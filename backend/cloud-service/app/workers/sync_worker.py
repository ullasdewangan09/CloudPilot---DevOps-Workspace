"""
CloudPilot — Durable Cloud Synchronization Worker (Phase 3B.7).

Responsibilities:
1. Operates as an independent, standalone process decoupled from FastAPI API servers.
2. Polls PostgreSQL for eligible pending jobs via atomic SELECT FOR UPDATE SKIP LOCKED.
3. Automatically recovers crashed/abandoned worker jobs via heartbeat lease expiration.
4. Executes AWS discovery using ResourceSyncEngine with isolated DB sessions.
5. Emits periodic heartbeats to maintain lease ownership during long-running discovery.
6. Implements exponential backoff retries for transient/recoverable errors.
7. Handles OS signals (SIGINT, SIGTERM) for graceful drain and shutdown.
"""

import logging
import os
import signal
import socket
import sys
import threading
import time
import uuid
from typing import Optional

from sqlalchemy.orm import Session

from app.core.config import (
    WORKER_POLL_INTERVAL,
    WORKER_HEARTBEAT_INTERVAL,
    WORKER_JOB_LEASE_SECONDS,
    WORKER_GRACEFUL_SHUTDOWN_TIMEOUT,
)
from app.core.database import SessionLocal, init_db
from app.models.cloud_resource import CloudSyncJob, SyncStatus
from app.services.job_queue import JobQueueService
from app.services.resource_sync import ResourceSyncEngine, RecoverableSyncError, SyncJobNotFoundError

logger = logging.getLogger("cloudpilot.sync_worker")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] [%(name)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)]
)


class CloudSyncWorker:
    """
    Independent worker daemon that claims and executes cloud synchronization jobs
    directly from PostgreSQL.
    """

    def __init__(
        self,
        worker_id: Optional[str] = None,
        poll_interval: float = WORKER_POLL_INTERVAL,
        heartbeat_interval: float = WORKER_HEARTBEAT_INTERVAL,
        lease_seconds: int = WORKER_JOB_LEASE_SECONDS,
        shutdown_timeout: float = WORKER_GRACEFUL_SHUTDOWN_TIMEOUT,
        session_factory=SessionLocal,
    ):
        self.worker_id = worker_id or f"worker-{socket.gethostname()}-{os.getpid()}-{uuid.uuid4().hex[:6]}"
        self.poll_interval = poll_interval
        self.heartbeat_interval = heartbeat_interval
        self.lease_seconds = lease_seconds
        self.shutdown_timeout = shutdown_timeout
        self.session_factory = session_factory

        self._stop_requested = threading.Event()
        self._current_job_id: Optional[uuid.UUID] = None
        self._current_job_lock = threading.Lock()
        self._heartbeat_thread: Optional[threading.Thread] = None
        self._heartbeat_stop = threading.Event()

        self._setup_signals()

    def _setup_signals(self) -> None:
        """Configures OS signal handlers for graceful shutdown."""
        try:
            signal.signal(signal.SIGINT, self._handle_signal)
            signal.signal(signal.SIGTERM, self._handle_signal)
        except (ValueError, AttributeError) as exc:
            logger.debug(f"Could not register signal handlers (likely running in non-main thread): {exc}")

    def _handle_signal(self, signum, frame) -> None:
        """Invoked when termination signal is received."""
        sig_name = signal.Signals(signum).name if hasattr(signal, "Signals") else str(signum)
        logger.info(f"Worker '{self.worker_id}' received {sig_name}; initiating graceful shutdown...")
        self.request_stop()

    def request_stop(self) -> None:
        """Requests worker to stop processing new jobs."""
        self._stop_requested.set()

    @property
    def is_running(self) -> bool:
        """Returns True if worker is active and not terminating."""
        return not self._stop_requested.is_set()

    def _heartbeat_ticker(self, job_id: uuid.UUID) -> None:
        """Background thread that pulses heartbeat timestamp while a job is running."""
        while not self._heartbeat_stop.is_set() and not self._stop_requested.is_set():
            self._heartbeat_stop.wait(self.heartbeat_interval)
            if self._heartbeat_stop.is_set():
                break

            try:
                db = self.session_factory()
                try:
                    updated = JobQueueService.update_heartbeat(
                        db=db,
                        job_id=job_id,
                        worker_id=self.worker_id
                    )
                    if updated:
                        logger.debug(f"Heartbeat pulsed for job {job_id} by {self.worker_id}")
                    else:
                        logger.debug(f"Heartbeat skipped (job {job_id} no longer running)")
                        break
                finally:
                    db.close()
            except Exception as exc:
                logger.warning(f"Error pulsing heartbeat for job {job_id}: {exc}")

    def _start_heartbeat(self, job_id: uuid.UUID) -> None:
        """Starts the background heartbeat ticker thread for the current job."""
        self._heartbeat_stop.clear()
        self._heartbeat_thread = threading.Thread(
            target=self._heartbeat_ticker,
            args=(job_id,),
            daemon=True,
            name=f"heartbeat-{job_id}"
        )
        self._heartbeat_thread.start()

    def _stop_heartbeat(self) -> None:
        """Stops the background heartbeat ticker thread."""
        self._heartbeat_stop.set()
        if self._heartbeat_thread and self._heartbeat_thread.is_alive():
            self._heartbeat_thread.join(timeout=2.0)
        self._heartbeat_thread = None

    def _execute_job(self, job_id: uuid.UUID) -> Optional[CloudSyncJob]:
        """
        Executes a claimed synchronization job with isolated database session and
        heartbeat tracking. Handles transient retries and terminal failures.
        """
        with self._current_job_lock:
            self._current_job_id = job_id

        self._start_heartbeat(job_id)
        db = self.session_factory()

        try:
            logger.info(f"Worker '{self.worker_id}' beginning execution of job {job_id}")
            engine = ResourceSyncEngine(db)
            result_job = engine.execute_sync(job_id=job_id, raise_on_recoverable=True)
            logger.info(
                f"Worker '{self.worker_id}' successfully completed job {job_id} "
                f"with status '{result_job.status}'"
            )
            return result_job

        except RecoverableSyncError as rec_exc:
            logger.warning(
                f"Recoverable error during job {job_id} on worker '{self.worker_id}': {rec_exc}. "
                "Scheduling exponential backoff retry."
            )
            try:
                retry_db = self.session_factory()
                try:
                    return JobQueueService.schedule_retry(
                        db=retry_db,
                        job_id=job_id,
                        error_message=str(rec_exc),
                        code="TRANSIENT_AWS_ERROR"
                    )
                finally:
                    retry_db.close()
            except Exception as retry_err:
                logger.exception(f"Failed to schedule retry for job {job_id}: {retry_err}")
                return None

        except Exception as unhandled_exc:
            logger.exception(f"Fatal error during execution of job {job_id}: {unhandled_exc}")
            try:
                fail_db = self.session_factory()
                try:
                    engine = ResourceSyncEngine(fail_db)
                    job_to_fail = fail_db.query(CloudSyncJob).filter(CloudSyncJob.id == job_id).first()
                    if job_to_fail and job_to_fail.status == SyncStatus.RUNNING.value:
                        return engine._fail_job(
                            job_to_fail,
                            f"Worker error: {str(unhandled_exc)}",
                            code="WORKER_EXECUTION_ERROR"
                        )
                finally:
                    fail_db.close()
            except Exception as fail_err:
                logger.exception(f"Failed to record failure for job {job_id}: {fail_err}")
            return None

        finally:
            self._stop_heartbeat()
            db.close()
            with self._current_job_lock:
                self._current_job_id = None

    def run_once(self) -> Optional[CloudSyncJob]:
        """
        Performs one execution cycle:
        1. Recovers abandoned jobs.
        2. Claims next eligible pending job.
        3. Executes the job.
        Returns the completed CloudSyncJob or None if no job was claimed.
        """
        db = self.session_factory()
        try:
            # 1. Crash recovery: recover dead/abandoned jobs
            recovered = JobQueueService.recover_abandoned_jobs(
                db=db,
                lease_seconds=self.lease_seconds
            )
            if recovered > 0:
                logger.info(f"Worker '{self.worker_id}' recovered {recovered} abandoned jobs.")

            # 2. Atomic claim: try to lock and claim next eligible pending job
            claimed_job = JobQueueService.claim_next_job(
                db=db,
                worker_id=self.worker_id
            )
        finally:
            db.close()

        if claimed_job:
            # 3. Execute claimed job in its own isolated lifecycle
            return self._execute_job(claimed_job.id)

        return None

    def run(self) -> None:
        """
        Main worker execution loop. Continuously polls and processes jobs
        until stop is requested via signal.
        """
        logger.info(
            f"CloudPilot Sync Worker '{self.worker_id}' started. "
            f"Poll interval: {self.poll_interval}s, Lease: {self.lease_seconds}s"
        )

        # Ensure database tables and migrations are initialized
        try:
            init_db()
        except Exception as exc:
            logger.warning(f"Worker database init warning (schema may already be ready): {exc}")

        while self.is_running:
            try:
                job = self.run_once()
                if not job and self.is_running:
                    # Sleep in small slices so shutdown signals break immediately
                    slices = max(1, int(self.poll_interval / 0.2))
                    for _ in range(slices):
                        if not self.is_running:
                            break
                        time.sleep(self.poll_interval / slices)
            except Exception as loop_exc:
                logger.exception(f"Unhandled error in worker loop for '{self.worker_id}': {loop_exc}")
                time.sleep(min(self.poll_interval, 2.0))

        self._shutdown()

    def _shutdown(self) -> None:
        """Executes graceful shutdown and waits for current job within grace period."""
        logger.info(f"Worker '{self.worker_id}' stopping. Waiting for current job to finish...")
        start_wait = time.time()

        while True:
            with self._current_job_lock:
                current_id = self._current_job_id

            if not current_id:
                logger.info(f"Worker '{self.worker_id}' cleanly drained and stopped.")
                break

            elapsed = time.time() - start_wait
            if elapsed >= self.shutdown_timeout:
                logger.warning(
                    f"Worker '{self.worker_id}' shutdown timeout ({self.shutdown_timeout}s) exceeded. "
                    f"Job {current_id} will be recovered by lease expiration."
                )
                break

            time.sleep(0.5)

        logger.info(f"Worker '{self.worker_id}' shutdown sequence complete.")


if __name__ == "__main__":
    worker = CloudSyncWorker()
    worker.run()

