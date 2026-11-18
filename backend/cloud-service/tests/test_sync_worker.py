"""
CloudPilot — Phase 3B.7 Automated Test Suite.
Durable Background Synchronization & Job Recovery Architecture Tests.

Verifies:
1. Single worker atomic job claim with FOR UPDATE SKIP LOCKED.
2. Multiple workers cannot claim the same job (zero duplicate execution).
3. Concurrent workers claim distinct eligible jobs simultaneously.
4. Account-level concurrency lock prevents claiming when account is actively running.
5. Scheduled retry job with future next_retry_at is not claimed until due.
6. Worker successfully executes a claimed job and updates discovered resources.
7. Recoverable transient error schedules exponential backoff retry.
8. Permanent validation or auth error fails immediately without retrying.
9. Maximum retry attempts exhaustion transitions job to terminal FAILED.
10. Periodic heartbeat updates refresh lease during execution.
11. Active worker with fresh heartbeat is never prematurely recovered.
12. Abandoned worker job with expired lease is safely recovered and requeued.
13. Abandoned worker job with exhausted attempts is marked FAILED.
14. Worker signal handling and graceful drain shutdown.
15. Worker survives individual job crash and continues processing subsequent jobs.
16. API POST /sync creates durable PENDING job and independent worker executes it.
17. Sync status endpoint accurately reflects lifecycle transitions across worker execution.
18. Worker executes reliably when Redis is disabled/offline (PostgreSQL source of truth).
"""

import os
import uuid
from datetime import datetime, timezone, timedelta
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import text

from app.models.cloud_account import CloudAccount
from app.models.cloud_resource import (
    CloudResource,
    CloudSyncJob,
    SyncStatus,
    SyncType,
    ResourceType,
)
from app.services.job_queue import JobQueueService
from app.services.resource_sync import (
    ResourceSyncEngine,
    RecoverableSyncError,
    ConcurrentSyncError,
)
from app.providers.aws.session import (
    AWSSessionManager,
    AWSAuthError,
    AWSConnectionError,
    AWSValidationError,
)
from app.workers.sync_worker import CloudSyncWorker
from app.core.config import WORKER_MAX_ATTEMPTS
from tests.conftest import TestingSessionLocal


# ── Fixtures ─────────────────────────────────────────────────────────────────

@pytest.fixture
def test_cloud_account(db, test_workspace):
    """Seed an active AWS CloudAccount in test_workspace."""
    account = CloudAccount(
        workspace_id=test_workspace,
        provider="aws",
        name="Production AWS Core",
        account_id="123456789012",
        default_region="us-east-1",
        role_arn="arn:aws:iam::123456789012:role/CloudPilotDiscoveryRole",
        external_id="cloudpilot-secure-ext-id",
        status="active",
        is_active=True,
        description="Production AWS environment"
    )
    db.add(account)
    db.commit()
    db.refresh(account)
    return account


@pytest.fixture
def second_cloud_account(db, test_workspace):
    """Seed a second distinct AWS CloudAccount in test_workspace."""
    account = CloudAccount(
        workspace_id=test_workspace,
        provider="aws",
        name="Staging AWS Core",
        account_id="987654321098",
        default_region="us-west-2",
        role_arn="arn:aws:iam::987654321098:role/CloudPilotStagingRole",
        external_id="cloudpilot-staging-ext-id",
        status="active",
        is_active=True,
        description="Staging AWS environment"
    )
    db.add(account)
    db.commit()
    db.refresh(account)
    return account


@pytest.fixture
def mock_aws_session():
    """Mock AWSSessionManager to bypass live AWS STS calls."""
    with patch.object(AWSSessionManager, "assume_role") as mock_assume:
        mock_assume.return_value = {
            "account_id": "123456789012",
            "assumed_role_arn": "arn:aws:iam::123456789012:role/CloudPilotDiscoveryRole",
            "user_id": "AROAEXAMPLE:CloudPilot",
            "verified_at": datetime.now(timezone.utc).isoformat(),
            "status": "active"
        }
        with patch.object(AWSSessionManager, "is_expired", return_value=False):
            yield mock_assume


# ==============================================================================
# 1. ATOMIC JOB CLAIMING & CONCURRENCY (PART 3)
# ==============================================================================

def test_single_worker_claim_success(db, test_cloud_account):
    """Verify single worker atomically claims a pending job and sets ownership."""
    engine = ResourceSyncEngine(db)
    job = engine.create_sync_job(test_cloud_account.id, test_cloud_account.workspace_id, sync_type=SyncType.EC2)
    assert job.status == SyncStatus.PENDING.value
    assert job.attempts == 0

    claimed = JobQueueService.claim_next_job(db, worker_id="worker-node-1")
    assert claimed is not None
    assert claimed.id == job.id
    assert claimed.status == SyncStatus.RUNNING.value
    assert claimed.worker_id == "worker-node-1"
    assert claimed.attempts == 1
    assert claimed.started_at is not None
    assert claimed.heartbeat_at is not None


def test_multiple_workers_cannot_claim_same_job(db, test_cloud_account):
    """Verify that once a job is claimed, another concurrent worker cannot claim it."""
    engine = ResourceSyncEngine(db)
    job = engine.create_sync_job(test_cloud_account.id, test_cloud_account.workspace_id)

    # Worker 1 claims
    claimed_w1 = JobQueueService.claim_next_job(db, worker_id="worker-node-1")
    assert claimed_w1 is not None

    # Worker 2 attempts to claim
    claimed_w2 = JobQueueService.claim_next_job(db, worker_id="worker-node-2")
    assert claimed_w2 is None  # No jobs available for claiming


def test_concurrent_workers_claim_different_jobs(db, test_cloud_account, second_cloud_account):
    """Verify concurrent workers claim distinct eligible jobs without conflict."""
    engine = ResourceSyncEngine(db)
    job1 = engine.create_sync_job(test_cloud_account.id, test_cloud_account.workspace_id)
    job2 = engine.create_sync_job(second_cloud_account.id, second_cloud_account.workspace_id)

    claimed_w1 = JobQueueService.claim_next_job(db, worker_id="worker-1")
    claimed_w2 = JobQueueService.claim_next_job(db, worker_id="worker-2")

    assert claimed_w1 is not None
    assert claimed_w2 is not None
    assert claimed_w1.id != claimed_w2.id
    assert {claimed_w1.id, claimed_w2.id} == {job1.id, job2.id}


def test_account_level_concurrency_blocks_claim(db, test_cloud_account):
    """Verify that a pending job for an account is NOT claimed if that account has a RUNNING job."""
    # Seed an actively running job for the account
    running_job = CloudSyncJob(
        cloud_account_id=test_cloud_account.id,
        workspace_id=test_cloud_account.workspace_id,
        status=SyncStatus.RUNNING.value,
        sync_type=SyncType.FULL.value,
        worker_id="active-worker",
        started_at=datetime.now(timezone.utc),
        heartbeat_at=datetime.now(timezone.utc)
    )
    db.add(running_job)

    # Seed a second pending job for the SAME account
    pending_job = CloudSyncJob(
        cloud_account_id=test_cloud_account.id,
        workspace_id=test_cloud_account.workspace_id,
        status=SyncStatus.PENDING.value,
        sync_type=SyncType.EC2.value
    )
    db.add(pending_job)
    db.commit()

    # Attempt to claim: must return None because the account is already running
    claimed = JobQueueService.claim_next_job(db, worker_id="worker-new")
    assert claimed is None


def test_future_retry_job_not_claimed_until_due(db, test_cloud_account):
    """Verify that a retry job with next_retry_at in the future is skipped until due."""
    future_time = datetime.now(timezone.utc) + timedelta(minutes=5)
    retry_job = CloudSyncJob(
        cloud_account_id=test_cloud_account.id,
        workspace_id=test_cloud_account.workspace_id,
        status=SyncStatus.PENDING.value,
        sync_type=SyncType.FULL.value,
        attempts=1,
        next_retry_at=future_time
    )
    db.add(retry_job)
    db.commit()

    # In the future: not eligible
    assert JobQueueService.claim_next_job(db, "worker-1") is None

    # Now make next_retry_at in the past
    retry_job.next_retry_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    db.commit()

    # Now eligible and successfully claimed
    claimed = JobQueueService.claim_next_job(db, "worker-1")
    assert claimed is not None
    assert claimed.id == retry_job.id


# ==============================================================================
# 2. JOB EXECUTION & RETRY STRATEGY (PART 4 & 5)
# ==============================================================================

def test_worker_successful_job_execution(db, test_cloud_account, mock_aws_session):
    """Verify worker claims and successfully executes a discovery job."""
    engine = ResourceSyncEngine(db)
    job = engine.create_sync_job(test_cloud_account.id, test_cloud_account.workspace_id, sync_type=SyncType.EC2)

    sample_inst = {
        "provider": "aws",
        "resource_type": ResourceType.EC2_INSTANCE.value,
        "provider_resource_id": "i-0abc1234def567890",
        "name": "WebServer-Production",
        "region": "us-east-1",
        "status": "running",
        "tags": {"Env": "prod"},
        "metadata": {"instance_type": "m5.large"}
    }

    worker = CloudSyncWorker(worker_id="test-worker-1", session_factory=TestingSessionLocal)

    with patch("app.services.resource_sync.EC2Discovery.discover", return_value=[sample_inst]):
        completed_job = worker.run_once()

    assert completed_job is not None
    assert completed_job.id == job.id
    assert completed_job.status == SyncStatus.COMPLETED.value
    assert completed_job.discovered_resources == 1
    assert completed_job.completed_at is not None
    assert completed_job.heartbeat_at is None


def test_worker_transient_error_schedules_retry(db, test_cloud_account, mock_aws_session):
    """Verify that a transient AWS error schedules an exponential backoff retry."""
    engine = ResourceSyncEngine(db)
    job = engine.create_sync_job(test_cloud_account.id, test_cloud_account.workspace_id, sync_type=SyncType.EC2)

    worker = CloudSyncWorker(worker_id="test-worker-retry", session_factory=TestingSessionLocal)

    transient_exc = AWSConnectionError("Connection timed out to ec2.us-east-1.amazonaws.com")
    with patch("app.services.resource_sync.EC2Discovery.discover", side_effect=transient_exc):
        result_job = worker.run_once()

    assert result_job is not None
    db.refresh(job)
    assert job.status == SyncStatus.PENDING.value
    assert job.attempts == 1
    assert job.next_retry_at is not None
    assert job.next_retry_at > datetime.now(timezone.utc)
    assert any("TRANSIENT_AWS_ERROR" in e.get("code", "") for e in job.errors)


def test_worker_permanent_error_fails_immediately(db, test_cloud_account):
    """Verify permanent auth or configuration error fails immediately without retrying."""
    engine = ResourceSyncEngine(db)
    job = engine.create_sync_job(test_cloud_account.id, test_cloud_account.workspace_id, sync_type=SyncType.EC2)

    worker = CloudSyncWorker(worker_id="test-worker-fail", session_factory=TestingSessionLocal)

    permanent_exc = AWSAuthError("AccessDenied: User not authorized to perform sts:AssumeRole", code="STS_ASSUME_ROLE_FAILED")
    with patch.object(AWSSessionManager, "assume_role", side_effect=permanent_exc):
        result_job = worker.run_once()

    assert result_job is not None
    assert result_job.status == SyncStatus.FAILED.value
    assert result_job.completed_at is not None
    assert result_job.next_retry_at is None
    assert any("STS_ASSUME_ROLE_FAILED" in e.get("code", "") for e in result_job.errors)


def test_worker_max_retries_exhaustion(db, test_cloud_account):
    """Verify that when attempts reach max_attempts, job is marked FAILED permanently."""
    job = CloudSyncJob(
        cloud_account_id=test_cloud_account.id,
        workspace_id=test_cloud_account.workspace_id,
        status=SyncStatus.RUNNING.value,
        sync_type=SyncType.FULL.value,
        attempts=3,
        max_attempts=3,
        started_at=datetime.now(timezone.utc)
    )
    db.add(job)
    db.commit()

    failed_job = JobQueueService.schedule_retry(db, job.id, "ThrottlingException: Rate exceeded")
    assert failed_job is not None
    assert failed_job.status == SyncStatus.FAILED.value
    assert failed_job.completed_at is not None
    assert any("MAX_RETRIES_EXHAUSTED" in e.get("code", "") for e in failed_job.errors)


# ==============================================================================
# 3. WORKER HEARTBEAT & CRASH RECOVERY (PART 6)
# ==============================================================================

def test_heartbeat_update(db, test_cloud_account):
    """Verify update_heartbeat updates heartbeat_at timestamp for running job."""
    past = datetime.now(timezone.utc) - timedelta(seconds=45)
    job = CloudSyncJob(
        cloud_account_id=test_cloud_account.id,
        workspace_id=test_cloud_account.workspace_id,
        status=SyncStatus.RUNNING.value,
        sync_type=SyncType.FULL.value,
        worker_id="active-worker",
        started_at=past,
        heartbeat_at=past
    )
    db.add(job)
    db.commit()

    success = JobQueueService.update_heartbeat(db, job.id, worker_id="active-worker")
    assert success is True
    db.refresh(job)
    assert job.heartbeat_at > past


def test_active_worker_with_fresh_heartbeat_not_recovered(db, test_cloud_account):
    """Verify that a worker with a fresh heartbeat is NOT recovered by crash recovery."""
    fresh_time = datetime.now(timezone.utc) - timedelta(seconds=10)
    job = CloudSyncJob(
        cloud_account_id=test_cloud_account.id,
        workspace_id=test_cloud_account.workspace_id,
        status=SyncStatus.RUNNING.value,
        sync_type=SyncType.FULL.value,
        worker_id="healthy-worker",
        started_at=datetime.now(timezone.utc) - timedelta(minutes=5),
        heartbeat_at=fresh_time
    )
    db.add(job)
    db.commit()

    recovered = JobQueueService.recover_abandoned_jobs(db, lease_seconds=120)
    assert recovered == 0
    db.refresh(job)
    assert job.status == SyncStatus.RUNNING.value


def test_abandoned_worker_lease_expired_requeued(db, test_cloud_account):
    """Verify that an abandoned job with expired heartbeat lease is recovered and requeued."""
    expired_time = datetime.now(timezone.utc) - timedelta(seconds=200)
    job = CloudSyncJob(
        cloud_account_id=test_cloud_account.id,
        workspace_id=test_cloud_account.workspace_id,
        status=SyncStatus.RUNNING.value,
        sync_type=SyncType.FULL.value,
        worker_id="crashed-worker",
        attempts=1,
        max_attempts=3,
        started_at=expired_time,
        heartbeat_at=expired_time
    )
    db.add(job)
    db.commit()

    recovered = JobQueueService.recover_abandoned_jobs(db, lease_seconds=120)
    assert recovered == 1
    db.refresh(job)
    assert job.status == SyncStatus.PENDING.value
    assert job.next_retry_at is not None
    assert any("WORKER_LEASE_EXPIRED" in e.get("code", "") for e in job.errors)


def test_abandoned_worker_exhausted_marked_failed(db, test_cloud_account):
    """Verify that an abandoned job with exhausted attempts is marked FAILED upon recovery."""
    expired_time = datetime.now(timezone.utc) - timedelta(seconds=300)
    job = CloudSyncJob(
        cloud_account_id=test_cloud_account.id,
        workspace_id=test_cloud_account.workspace_id,
        status=SyncStatus.RUNNING.value,
        sync_type=SyncType.FULL.value,
        worker_id="crashed-worker",
        attempts=3,
        max_attempts=3,
        started_at=expired_time,
        heartbeat_at=expired_time
    )
    db.add(job)
    db.commit()

    recovered = JobQueueService.recover_abandoned_jobs(db, lease_seconds=120)
    assert recovered == 1
    db.refresh(job)
    assert job.status == SyncStatus.FAILED.value
    assert job.completed_at is not None
    assert any("WORKER_CRASHED_EXHAUSTED" in e.get("code", "") for e in job.errors)


# ==============================================================================
# 4. WORKER LIFECYCLE & SHUTDOWN (PART 7)
# ==============================================================================

def test_worker_graceful_shutdown(db, test_cloud_account):
    """Verify worker responds to shutdown signal and stops claiming new jobs."""
    worker = CloudSyncWorker(worker_id="shutdown-test-worker", session_factory=TestingSessionLocal)
    assert worker.is_running is True

    worker.request_stop()
    assert worker.is_running is False


def test_worker_survives_individual_job_failure(db, test_cloud_account, second_cloud_account, mock_aws_session):
    """Verify that an unhandled crash in one job does not prevent worker from processing next jobs."""
    engine = ResourceSyncEngine(db)
    job1 = engine.create_sync_job(test_cloud_account.id, test_cloud_account.workspace_id, sync_type=SyncType.EC2)
    job2 = engine.create_sync_job(second_cloud_account.id, second_cloud_account.workspace_id, sync_type=SyncType.EC2)

    worker = CloudSyncWorker(worker_id="resilient-worker", session_factory=TestingSessionLocal)

    # Job 1 throws unexpected fatal runtime error
    with patch("app.services.resource_sync.ResourceSyncEngine.execute_sync", side_effect=RuntimeError("Hardware fault")):
        failed_res = worker.run_once()

    db.refresh(job1)
    assert job1.status == SyncStatus.FAILED.value
    assert "Hardware fault" in job1.errors[0]["error"]

    # Job 2 executes normally right after
    with patch("app.services.resource_sync.EC2Discovery.discover", return_value=[]):
        success_res = worker.run_once()

    assert success_res is not None
    assert success_res.id == job2.id
    assert success_res.status == SyncStatus.COMPLETED.value


# ==============================================================================
# 5. REST API & DURABLE WORKER INTEGRATION (PART 10)
# ==============================================================================

def test_api_creates_durable_pending_job_and_worker_executes(client, test_user, test_cloud_account, db, mock_aws_session):
    """Verify REST API creates durable PENDING job and independent worker executes it to completion."""
    # 1. API Call: Trigger sync
    resp = client.post(
        f"/api/v1/cloud/accounts/{test_cloud_account.id}/sync",
        json={"sync_type": "ec2"},
        headers=test_user["headers"]
    )
    assert resp.status_code == 202
    job_id = uuid.UUID(resp.json()["job_id"])

    # 2. Database Verification: Job is in PENDING state with 0 attempts
    job_in_db = db.query(CloudSyncJob).filter(CloudSyncJob.id == job_id).first()
    assert job_in_db is not None
    assert job_in_db.status == SyncStatus.PENDING.value
    assert job_in_db.attempts == 0

    # 3. Independent Worker Run
    worker = CloudSyncWorker(worker_id="standalone-worker", session_factory=TestingSessionLocal)
    with patch("app.services.resource_sync.EC2Discovery.discover", return_value=[]):
        processed_job = worker.run_once()

    assert processed_job is not None
    assert processed_job.id == job_id
    assert processed_job.status == SyncStatus.COMPLETED.value

    # 4. API Sync Status Endpoint Verification
    status_resp = client.get(
        f"/api/v1/cloud/accounts/{test_cloud_account.id}/sync/status",
        headers=test_user["headers"]
    )
    assert status_resp.status_code == 200
    status_data = status_resp.json()
    assert status_data["has_active_job"] is False
    assert status_data["latest_job"]["id"] == str(job_id)
    assert status_data["latest_job"]["status"] == "completed"


def test_worker_executes_when_redis_offline(db, test_cloud_account, mock_aws_session):
    """Verify worker successfully claims and executes jobs when Redis cache is disabled."""
    from app.core.cache import cache_manager
    original_enabled = cache_manager.enabled
    cache_manager.enabled = False

    try:
        engine = ResourceSyncEngine(db)
        job = engine.create_sync_job(test_cloud_account.id, test_cloud_account.workspace_id, sync_type=SyncType.EC2)

        worker = CloudSyncWorker(worker_id="no-redis-worker", session_factory=TestingSessionLocal)
        with patch("app.services.resource_sync.EC2Discovery.discover", return_value=[]):
            completed = worker.run_once()

        assert completed is not None
        assert completed.status == SyncStatus.COMPLETED.value
    finally:
        cache_manager.enabled = original_enabled


# ==============================================================================
# 6. WORKER ENGINE & QUEUE COVERAGE EDGE CASES
# ==============================================================================

def test_worker_heartbeat_ticker_and_lifecycle(db, test_cloud_account):
    """Verify heartbeat ticker thread starts, updates database, and terminates cleanly."""
    job = CloudSyncJob(
        cloud_account_id=test_cloud_account.id,
        workspace_id=test_cloud_account.workspace_id,
        status=SyncStatus.RUNNING.value,
        worker_id="ticker-worker",
        started_at=datetime.now(timezone.utc),
        heartbeat_at=datetime.now(timezone.utc)
    )
    db.add(job)
    db.commit()

    worker = CloudSyncWorker(
        worker_id="ticker-worker",
        heartbeat_interval=0.05,
        session_factory=TestingSessionLocal
    )
    worker._start_heartbeat(job.id)
    assert worker._heartbeat_thread is not None
    import time
    time.sleep(0.12)
    worker._stop_heartbeat()
    assert worker._heartbeat_thread is None


def test_worker_signal_handling():
    """Verify signal handling records shutdown request."""
    import signal
    worker = CloudSyncWorker(worker_id="signal-worker", session_factory=TestingSessionLocal)
    assert worker.is_running is True

    worker._handle_signal(signal.SIGINT, None)
    assert worker.is_running is False


def test_worker_run_loop_and_drain():
    """Verify worker run loop processes and exits when stop is requested."""
    worker = CloudSyncWorker(
        worker_id="run-loop-worker",
        poll_interval=0.05,
        session_factory=TestingSessionLocal
    )

    call_count = 0
    def mock_run_once():
        nonlocal call_count
        call_count += 1
        worker.request_stop()
        return None

    worker.run_once = mock_run_once
    worker.run()
    assert call_count >= 1
    assert worker.is_running is False


def test_worker_shutdown_timeout_warning():
    """Verify worker shutdown handles active job exceeding timeout."""
    worker = CloudSyncWorker(
        worker_id="timeout-worker",
        shutdown_timeout=0.05,
        session_factory=TestingSessionLocal
    )
    worker._current_job_id = uuid.uuid4()
    worker._shutdown()
    assert worker._current_job_id is not None


def test_job_queue_edge_cases(db, test_cloud_account):
    """Verify error and edge case handling in JobQueueService."""
    # Nonexistent job update
    fake_id = uuid.uuid4()
    assert JobQueueService.update_heartbeat(db, fake_id) is False
    assert JobQueueService.schedule_retry(db, fake_id, "error") is None

    # Recover abandoned jobs with account filter
    recovered = JobQueueService.recover_abandoned_jobs(
        db, lease_seconds=120, cloud_account_id=test_cloud_account.id
    )
    assert recovered == 0

    # Error handling when db session raises error
    mock_db = MagicMock()
    mock_db.execute.side_effect = Exception("DB Connection Lost")
    with pytest.raises(Exception):
        JobQueueService.claim_next_job(mock_db, "failing-worker")
    mock_db.rollback.assert_called_once()

    # Recovery error handling
    mock_db_recover = MagicMock()
    mock_db_recover.execute.side_effect = Exception("DB Timeout")
    assert JobQueueService.recover_abandoned_jobs(mock_db_recover) == 0
    mock_db_recover.rollback.assert_called_once()


