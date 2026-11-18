"""
CloudPilot — Durable PostgreSQL-Backed Job Queue (Phase 3B.7).

Responsibilities:
1. Atomic job claiming via PostgreSQL `SELECT FOR UPDATE SKIP LOCKED`.
2. Safe multi-worker concurrency with zero duplicate claims.
3. Account-level synchronization concurrency protection.
4. Heartbeat tracking and abandoned worker lease expiration recovery.
5. Exponential backoff retry scheduling for recoverable transient failures.
6. Safe transaction lifecycle with PostgreSQL as authoritative queue.
"""

import logging
import uuid
from datetime import datetime, timezone, timedelta
from typing import List, Optional, Tuple, Dict, Any

from sqlalchemy import select, or_, and_, text
from sqlalchemy.orm import Session

from app.models.cloud_resource import CloudSyncJob, SyncStatus
from app.core.config import (
    WORKER_MAX_ATTEMPTS,
    WORKER_JOB_LEASE_SECONDS,
    WORKER_INITIAL_RETRY_DELAY_SECONDS,
    WORKER_MAX_RETRY_DELAY_SECONDS,
    WORKER_BACKOFF_FACTOR,
)

logger = logging.getLogger(__name__)


class JobQueueService:
    """
    Service for durable PostgreSQL-backed job queue operations.
    Provides atomic claiming, retry management, and crash recovery.
    """

    @staticmethod
    def claim_next_job(
        db: Session,
        worker_id: str
    ) -> Optional[CloudSyncJob]:
        """
        Atomically claims the next eligible PENDING job using PostgreSQL SELECT FOR UPDATE SKIP LOCKED.

        Eligibility Criteria:
        1. status == 'pending'
        2. next_retry_at IS NULL or next_retry_at <= NOW() (retry backoff satisfied)
        3. No other job for the same cloud_account_id is currently 'running'
        4. Oldest jobs claimed first (FIFO by created_at)

        Updates the job to 'running' with worker_id, incremented attempts, and fresh heartbeat.
        Returns the claimed CloudSyncJob or None if queue is empty/ineligible.
        """
        now = datetime.now(timezone.utc)

        try:
            # Subquery: accounts that already have an active RUNNING job
            # Protects account-level consistency across multiple workers
            running_accounts = (
                select(CloudSyncJob.cloud_account_id)
                .where(CloudSyncJob.status == SyncStatus.RUNNING.value)
            )

            # Atomic claim query using row-level exclusive lock with skip locked
            claim_stmt = (
                select(CloudSyncJob.id)
                .where(
                    CloudSyncJob.status == SyncStatus.PENDING.value,
                    or_(
                        CloudSyncJob.next_retry_at.is_(None),
                        CloudSyncJob.next_retry_at <= now
                    ),
                    CloudSyncJob.cloud_account_id.not_in(running_accounts)
                )
                .order_by(CloudSyncJob.created_at.asc())
                .limit(1)
                .with_for_update(skip_locked=True)
            )

            job_id = db.execute(claim_stmt).scalar_one_or_none()
            if not job_id:
                db.rollback()
                return None

            # Fetch the locked row
            job = db.query(CloudSyncJob).filter(CloudSyncJob.id == job_id).first()
            if not job:
                db.rollback()
                return None

            # Transition state atomically
            job.status = SyncStatus.RUNNING.value
            job.worker_id = worker_id
            job.attempts = (job.attempts or 0) + 1
            job.started_at = now
            job.heartbeat_at = now
            job.updated_at = now

            db.commit()
            db.refresh(job)

            logger.info(
                f"Worker '{worker_id}' successfully claimed job {job.id} "
                f"(account={job.cloud_account_id}, sync_type={job.sync_type}, attempt={job.attempts})"
            )

            # Invalidate sync status cache so API clients immediately see RUNNING
            try:
                from app.core.cache import cache_manager
                cache_manager.invalidate_sync_status(job.workspace_id, job.cloud_account_id)
            except Exception as exc:
                logger.warning(f"Failed to invalidate sync status after job claim: {exc}")

            return job

        except Exception as exc:
            db.rollback()
            logger.exception(f"Error during atomic job claim for worker '{worker_id}': {exc}")
            raise

    @staticmethod
    def update_heartbeat(
        db: Session,
        job_id: uuid.UUID,
        worker_id: Optional[str] = None
    ) -> bool:
        """
        Refreshes heartbeat_at timestamp for an active running job.
        Returns True if heartbeat was updated, False if job is no longer running.
        """
        now = datetime.now(timezone.utc)
        try:
            query = db.query(CloudSyncJob).filter(
                CloudSyncJob.id == job_id,
                CloudSyncJob.status == SyncStatus.RUNNING.value
            )
            if worker_id:
                query = query.filter(CloudSyncJob.worker_id == worker_id)

            job = query.first()
            if not job:
                return False

            job.heartbeat_at = now
            job.updated_at = now
            db.commit()
            return True
        except Exception as exc:
            db.rollback()
            logger.warning(f"Failed to update heartbeat for job {job_id}: {exc}")
            return False

    @staticmethod
    def schedule_retry(
        db: Session,
        job_id: uuid.UUID,
        error_message: str,
        code: str = "RETRY_SCHEDULED"
    ) -> Optional[CloudSyncJob]:
        """
        Handles job failure with exponential backoff retry scheduling.
        If attempts < max_attempts: transitions back to PENDING with next_retry_at.
        If attempts >= max_attempts: transitions to terminal FAILED.
        """
        now = datetime.now(timezone.utc)
        try:
            job = db.query(CloudSyncJob).filter(CloudSyncJob.id == job_id).first()
            if not job:
                return None

            attempts = job.attempts or 1
            max_attempts = job.max_attempts or WORKER_MAX_ATTEMPTS

            if attempts < max_attempts:
                # Exponential backoff delay
                delay = min(
                    WORKER_INITIAL_RETRY_DELAY_SECONDS * (WORKER_BACKOFF_FACTOR ** (attempts - 1)),
                    WORKER_MAX_RETRY_DELAY_SECONDS
                )
                next_retry = now + timedelta(seconds=delay)
                job.status = SyncStatus.PENDING.value
                job.next_retry_at = next_retry
                job.heartbeat_at = None
                job.errors = list(job.errors or []) + [{
                    "service": "orchestrator",
                    "error": f"Attempt {attempts}/{max_attempts} failed: {error_message}. Retry scheduled in {int(delay)}s.",
                    "code": code,
                    "attempt": attempts,
                    "next_retry_at": next_retry.isoformat(),
                    "timestamp": now.isoformat()
                }]
                logger.warning(
                    f"Job {job.id} (account={job.cloud_account_id}) scheduled for retry at "
                    f"{next_retry.isoformat()} (attempt {attempts}/{max_attempts}, backoff={int(delay)}s)"
                )
            else:
                job.status = SyncStatus.FAILED.value
                job.completed_at = now
                job.heartbeat_at = None
                job.errors = list(job.errors or []) + [{
                    "service": "orchestrator",
                    "error": f"Maximum retry attempts ({max_attempts}) exhausted. Final error: {error_message}",
                    "code": "MAX_RETRIES_EXHAUSTED",
                    "attempt": attempts,
                    "timestamp": now.isoformat()
                }]
                logger.error(
                    f"Job {job.id} (account={job.cloud_account_id}) failed permanently: max attempts ({max_attempts}) exhausted"
                )

            job.updated_at = now
            db.commit()
            db.refresh(job)

            try:
                from app.core.cache import cache_manager
                cache_manager.invalidate_sync_status(job.workspace_id, job.cloud_account_id)
            except Exception as exc:
                logger.warning(f"Failed to invalidate sync status after retry schedule: {exc}")

            return job
        except Exception as exc:
            db.rollback()
            logger.exception(f"Failed to schedule retry for job {job_id}: {exc}")
            raise

    @staticmethod
    def recover_abandoned_jobs(
        db: Session,
        lease_seconds: int = WORKER_JOB_LEASE_SECONDS,
        cloud_account_id: Optional[uuid.UUID] = None
    ) -> int:
        """
        Detects and recovers jobs abandoned by crashed or terminated workers.
        Uses SELECT FOR UPDATE SKIP LOCKED to prevent concurrent recovery conflicts.

        Recovery Logic:
        - If attempts < max_attempts: Requeues to PENDING with backoff delay.
        - If attempts >= max_attempts: Marks as FAILED.
        """
        now = datetime.now(timezone.utc)
        cutoff = now - timedelta(seconds=lease_seconds)

        try:
            # Query for stuck RUNNING jobs whose lease has expired
            query = select(CloudSyncJob.id).where(
                CloudSyncJob.status == SyncStatus.RUNNING.value,
                or_(
                    and_(CloudSyncJob.heartbeat_at.is_not(None), CloudSyncJob.heartbeat_at < cutoff),
                    and_(CloudSyncJob.heartbeat_at.is_(None), CloudSyncJob.started_at < cutoff)
                )
            )

            if cloud_account_id:
                query = query.where(CloudSyncJob.cloud_account_id == cloud_account_id)

            # Prevent recovery race conditions
            locked_stmt = query.with_for_update(skip_locked=True)
            stale_ids = db.execute(locked_stmt).scalars().all()

            if not stale_ids:
                return 0

            stale_jobs = db.query(CloudSyncJob).filter(CloudSyncJob.id.in_(stale_ids)).all()
            recovered_count = 0

            for job in stale_jobs:
                attempts = job.attempts or 1
                max_attempts = job.max_attempts or WORKER_MAX_ATTEMPTS

                # Check if this was a legacy zombie (no heartbeat) or active heartbeat expiration
                err_code = "WORKER_LEASE_EXPIRED" if job.heartbeat_at is not None else "ZOMBIE_JOB_TIMEOUT"

                if attempts < max_attempts:
                    # Requeue job for retry
                    delay = min(
                        WORKER_INITIAL_RETRY_DELAY_SECONDS * (WORKER_BACKOFF_FACTOR ** (attempts - 1)),
                        WORKER_MAX_RETRY_DELAY_SECONDS
                    )
                    job.status = SyncStatus.PENDING.value
                    job.next_retry_at = now + timedelta(seconds=delay)
                    job.heartbeat_at = None
                    job.errors = list(job.errors or []) + [{
                        "service": "orchestrator",
                        "error": f"Worker lease expired (no heartbeat for > {lease_seconds}s). Requeued for attempt {attempts + 1}.",
                        "code": err_code,
                        "recovered_at": now.isoformat()
                    }]
                    logger.warning(
                        f"Requeued abandoned job {job.id} (worker='{job.worker_id}', account={job.cloud_account_id})"
                    )
                else:
                    job.status = SyncStatus.FAILED.value
                    job.completed_at = now
                    job.heartbeat_at = None
                    job.errors = list(job.errors or []) + [{
                        "service": "orchestrator",
                        "error": f"Worker crashed or lease expired. Maximum attempts ({max_attempts}) exhausted.",
                        "code": err_code if err_code == "ZOMBIE_JOB_TIMEOUT" else "WORKER_CRASHED_EXHAUSTED",
                        "recovered_at": now.isoformat()
                    }]
                    logger.error(
                        f"Failed abandoned job {job.id} (worker='{job.worker_id}'): attempts exhausted ({attempts}/{max_attempts})"
                    )

                job.updated_at = now
                recovered_count += 1

            db.commit()

            # Invalidate sync status cache for all affected accounts
            try:
                from app.core.cache import cache_manager
                for j in stale_jobs:
                    cache_manager.invalidate_sync_status(j.workspace_id, j.cloud_account_id)
            except Exception as exc:
                logger.warning(f"Failed to invalidate sync status after recovery: {exc}")

            return recovered_count

        except Exception as exc:
            db.rollback()
            logger.exception(f"Error recovering abandoned jobs: {exc}")
            return 0
