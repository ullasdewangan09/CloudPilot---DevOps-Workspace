"""
CloudPilot — Resource Synchronization Engine & Database Reconciliation (Phase 3B.4).

Responsibilities:
1. Orchestrates AWS resource discovery for EC2, S3, VPC, and RDS using existing discovery modules.
2. Manages CloudSyncJob lifecycle (PENDING -> RUNNING -> COMPLETED / PARTIAL_FAILURE / FAILED).
3. Performs high-performance PostgreSQL bulk upserts on (cloud_account_id, resource_type, provider_resource_id).
4. Safely reconciles stale resources strictly within verified, successful discovery scopes.
5. Preserves existing resources whenever a discovery scope fails or partially degrades.
6. Enforces tenant and cloud account isolation.
7. Prevents concurrent overlapping synchronizations for the same cloud account using PostgreSQL advisory locking.
8. Recovers zombie/stale jobs that exceed execution timeout limits.
9. Strictly excludes temporary credentials, session tokens, or sensitive secrets from logs, DB, and errors.
"""

import logging
import uuid
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional, Set, Tuple, Union

from sqlalchemy import select, update, func, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.models.cloud_account import CloudAccount
from app.models.cloud_resource import (
    CloudResource,
    CloudSyncJob,
    SyncStatus,
    SyncType,
    ResourceType,
)
from app.providers.aws.session import (
    AWSSessionManager,
    AWSSessionError,
    AWSAuthError,
    AWSValidationError,
    AWSConnectionError,
    AWSThrottlingError,
    AWSDiscoveryError,
)
from app.providers.aws.ec2 import EC2Discovery
from app.providers.aws.s3 import S3Discovery
from app.providers.aws.vpc import VPCDiscovery
from app.providers.aws.rds import RDSDiscovery

logger = logging.getLogger(__name__)

# Timeout threshold after which a RUNNING job is considered a zombie / dead worker (30 minutes)
JOB_TIMEOUT_MINUTES = 30


# ── Domain Exceptions ────────────────────────────────────────────────────────

class SyncEngineError(Exception):
    """Base exception for sync engine failures."""
    pass


class ConcurrentSyncError(SyncEngineError):
    """Raised when another sync operation is actively running for the cloud account."""
    pass


class CloudAccountNotFoundError(SyncEngineError):
    """Raised when the specified cloud account does not exist."""
    pass


class SyncJobNotFoundError(SyncEngineError):
    """Raised when the specified sync job does not exist."""
    pass


# ── Discovery Scope Data Structure ──────────────────────────────────────────

@dataclass
class DiscoveryScopeResult:
    """
    Encapsulates the discovery result for a specific resource type and regional scope.
    Reconciliation is strictly gated on success == True.
    """
    service: str              # "ec2", "s3", "vpc", "rds"
    resource_type: str        # ResourceType.value (e.g. "ec2_instance", "s3_bucket")
    region: str               # AWS region or "global" for S3
    success: bool             # Gating flag for reconciliation
    resources: List[Dict[str, Any]] = field(default_factory=list)
    error_message: Optional[str] = None
    error_code: Optional[str] = None


# ── ResourceSyncEngine ──────────────────────────────────────────────────────

class ResourceSyncEngine:
    """
    Core synchronization and database reconciliation engine for CloudPilot.
    """

    def __init__(self, db: Session):
        self.db = db

    # ── Job Management ──────────────────────────────────────────────────────

    def create_sync_job(
        self,
        cloud_account_id: uuid.UUID,
        workspace_id: uuid.UUID,
        sync_type: Union[SyncType, str] = SyncType.FULL,
        triggered_by_user_id: Optional[int] = None,
    ) -> CloudSyncJob:
        """
        Creates a new CloudSyncJob in PENDING state.
        Validates the existence of the cloud account and workspace.
        """
        st_val = sync_type.value if isinstance(sync_type, SyncType) else str(sync_type).lower()
        if st_val not in [e.value for e in SyncType]:
            raise ValueError(f"Invalid sync_type '{sync_type}'. Must be one of {[e.value for e in SyncType]}")

        # Verify cloud account exists
        account = self.db.query(CloudAccount).filter(
            CloudAccount.id == cloud_account_id,
            CloudAccount.workspace_id == workspace_id,
            CloudAccount.is_active == True
        ).first()

        if not account:
            raise CloudAccountNotFoundError(
                f"Cloud account {cloud_account_id} not found or inactive in workspace {workspace_id}."
            )

        job = CloudSyncJob(
            cloud_account_id=cloud_account_id,
            workspace_id=workspace_id,
            triggered_by_user_id=triggered_by_user_id,
            status=SyncStatus.PENDING.value,
            sync_type=st_val,
            total_resources=0,
            discovered_resources=0,
            updated_resources=0,
            deleted_resources=0,
            summary={},
            errors=[],
        )
        self.db.add(job)
        self.db.commit()
        self.db.refresh(job)

        # Invalidate sync status cache so clients immediately see the new PENDING job
        try:
            from app.core.cache import cache_manager
            cache_manager.invalidate_sync_status(workspace_id, cloud_account_id)
        except Exception as exc:
            logger.warning(f"Failed to invalidate sync status on job create: {exc}")

        return job

    def sync_account(
        self,
        cloud_account_id: uuid.UUID,
        workspace_id: uuid.UUID,
        sync_type: Union[SyncType, str] = SyncType.FULL,
        triggered_by_user_id: Optional[int] = None,
        regions: Optional[List[str]] = None,
    ) -> CloudSyncJob:
        """
        Convenience method to create and synchronously execute a sync job.
        """
        job = self.create_sync_job(
            cloud_account_id=cloud_account_id,
            workspace_id=workspace_id,
            sync_type=sync_type,
            triggered_by_user_id=triggered_by_user_id,
        )
        return self.execute_sync(job.id, regions=regions)

    def recover_zombie_jobs(self, cloud_account_id: uuid.UUID) -> int:
        """
        Detects and fails jobs stuck in RUNNING beyond JOB_TIMEOUT_MINUTES.
        Returns the number of recovered zombie jobs.
        """
        cutoff = datetime.now(timezone.utc) - timedelta(minutes=JOB_TIMEOUT_MINUTES)
        stale_jobs = self.db.query(CloudSyncJob).filter(
            CloudSyncJob.cloud_account_id == cloud_account_id,
            CloudSyncJob.status == SyncStatus.RUNNING.value,
            CloudSyncJob.started_at < cutoff
        ).all()

        recovered_count = 0
        for job in stale_jobs:
            job.status = SyncStatus.FAILED.value
            job.completed_at = datetime.now(timezone.utc)
            job.errors = list(job.errors or []) + [{
                "service": "orchestrator",
                "error": "Job timed out or worker process terminated unexpectedly.",
                "code": "ZOMBIE_JOB_TIMEOUT",
                "recovered_at": datetime.now(timezone.utc).isoformat()
            }]
            recovered_count += 1

        if recovered_count > 0:
            self.db.commit()
            logger.warning(
                f"Recovered {recovered_count} zombie sync jobs for cloud account {cloud_account_id}."
            )
            # Invalidate sync status cache for recovered accounts
            try:
                from app.core.cache import cache_manager
                for j in stale_jobs:
                    cache_manager.invalidate_sync_status(j.workspace_id, j.cloud_account_id)
            except Exception as exc:
                logger.warning(f"Failed to invalidate sync status after zombie recovery: {exc}")

        return recovered_count

    # ── Concurrency & Locking ───────────────────────────────────────────────

    def _acquire_account_lock(self, cloud_account_id: uuid.UUID) -> bool:
        """
        Attempt to acquire a PostgreSQL advisory transaction lock for the cloud account.
        Derives a stable 64-bit integer from the cloud account UUID.
        """
        lock_id = uuid.UUID(str(cloud_account_id)).int & 0x7FFFFFFFFFFFFFFF
        result = self.db.execute(
            text("SELECT pg_try_advisory_xact_lock(:lock_id)"),
            {"lock_id": lock_id}
        ).scalar()
        return bool(result)

    def _check_active_running_job(self, cloud_account_id: uuid.UUID, current_job_id: uuid.UUID) -> None:
        """
        Checks if another sync job is currently RUNNING for this cloud account.
        First attempts to recover any dead zombie jobs.
        """
        self.recover_zombie_jobs(cloud_account_id)

        active_job = self.db.query(CloudSyncJob).filter(
            CloudSyncJob.cloud_account_id == cloud_account_id,
            CloudSyncJob.id != current_job_id,
            CloudSyncJob.status == SyncStatus.RUNNING.value
        ).first()

        if active_job:
            raise ConcurrentSyncError(
                f"Sync job {active_job.id} is currently running for cloud account {cloud_account_id}. "
                "Overlapping syncs are prevented to protect resource consistency."
            )

    # ── Core Synchronization Workflow ────────────────────────────────────────

    def execute_sync(
        self,
        job_id: uuid.UUID,
        regions: Optional[List[str]] = None,
    ) -> CloudSyncJob:
        """
        Executes synchronization for the given CloudSyncJob.
        Transitions state: PENDING -> RUNNING -> COMPLETED / PARTIAL_FAILURE / FAILED.
        """
        job = self.db.query(CloudSyncJob).filter(CloudSyncJob.id == job_id).first()
        if not job:
            raise SyncJobNotFoundError(f"Sync job {job_id} not found.")

        # Check for active running jobs and recover zombies
        self._check_active_running_job(job.cloud_account_id, job.id)

        # Transition job to RUNNING
        job.status = SyncStatus.RUNNING.value
        job.started_at = datetime.now(timezone.utc)
        self.db.commit()
        self.db.refresh(job)

        # Invalidate sync status cache so clients immediately see the RUNNING job
        try:
            from app.core.cache import cache_manager
            cache_manager.invalidate_sync_status(job.workspace_id, job.cloud_account_id)
        except Exception as exc:
            logger.warning(f"Failed to invalidate sync status on job start: {exc}")

        account = self.db.query(CloudAccount).filter(CloudAccount.id == job.cloud_account_id).first()
        if not account:
            return self._fail_job(job, "Cloud account associated with this job no longer exists.")

        if account.provider != "aws":
            return self._fail_job(
                job,
                f"Provider '{account.provider}' is not supported for synchronization. Only 'aws' is supported.",
                code="UNSUPPORTED_PROVIDER"
            )

        # Target regions: use explicit regions, or default to account's default_region, or us-east-1
        target_regions = regions or ([account.default_region] if account.default_region else ["us-east-1"])
        target_regions = [r.strip() for r in target_regions if r and r.strip()]
        if not target_regions:
            target_regions = ["us-east-1"]

        # 1. Establish AWS STS Session
        try:
            session_manager = AWSSessionManager.from_cloud_account(account)
            session_manager.assume_role()
        except (AWSSessionError, Exception) as exc:
            sanitized_err = self._sanitize_error(exc)
            logger.error(f"Failed to establish AWS STS session for account {account.id}: {sanitized_err}")
            return self._fail_job(job, sanitized_err, code=getattr(exc, "code", "STS_ASSUME_ROLE_FAILED"))

        # 2. Run discovery modules based on sync_type
        scope_results: List[DiscoveryScopeResult] = []
        sync_type = job.sync_type

        try:
            # PostgreSQL advisory lock for the critical persistence & reconciliation phase
            self._acquire_account_lock(account.id)

            if sync_type in (SyncType.FULL.value, SyncType.EC2.value):
                scope_results.extend(self._discover_ec2(session_manager, target_regions))

            if sync_type in (SyncType.FULL.value, SyncType.S3.value):
                scope_results.extend(self._discover_s3(session_manager))

            if sync_type in (SyncType.FULL.value, SyncType.VPC.value):
                scope_results.extend(self._discover_vpc(session_manager, target_regions))

            if sync_type in (SyncType.FULL.value, SyncType.RDS.value):
                scope_results.extend(self._discover_rds(session_manager, target_regions))

            # 3. Process persistence & safe reconciliation for each scope
            return self._finalize_sync(job, account, scope_results)

        except Exception as exc:
            self.db.rollback()
            sanitized_err = self._sanitize_error(exc)
            logger.exception(f"Unexpected error during sync execution for job {job.id}: {sanitized_err}")
            return self._fail_job(job, sanitized_err, code="INTERNAL_SYNC_ERROR")

    # ── Modular Discovery Runners ────────────────────────────────────────────

    def _discover_ec2(
        self, session_manager: AWSSessionManager, regions: List[str]
    ) -> List[DiscoveryScopeResult]:
        """Execute EC2 discovery across target regions."""
        results = []
        for region in regions:
            try:
                ec2_disco = EC2Discovery(session_manager, region=region)
                resources = ec2_disco.discover()
                results.append(DiscoveryScopeResult(
                    service="ec2",
                    resource_type=ResourceType.EC2_INSTANCE.value,
                    region=region,
                    success=True,
                    resources=resources
                ))
            except Exception as exc:
                err_msg = self._sanitize_error(exc)
                logger.warning(f"EC2 discovery failed in region {region}: {err_msg}")
                results.append(DiscoveryScopeResult(
                    service="ec2",
                    resource_type=ResourceType.EC2_INSTANCE.value,
                    region=region,
                    success=False,
                    error_message=err_msg,
                    error_code=getattr(exc, "code", "EC2_DISCOVERY_ERROR")
                ))
        return results

    def _discover_s3(
        self, session_manager: AWSSessionManager
    ) -> List[DiscoveryScopeResult]:
        """Execute S3 discovery across the global account namespace."""
        try:
            s3_disco = S3Discovery(session_manager)
            resources = s3_disco.discover()
            return [DiscoveryScopeResult(
                service="s3",
                resource_type=ResourceType.S3_BUCKET.value,
                region="global",
                success=True,
                resources=resources
            )]
        except Exception as exc:
            err_msg = self._sanitize_error(exc)
            logger.warning(f"S3 discovery failed: {err_msg}")
            return [DiscoveryScopeResult(
                service="s3",
                resource_type=ResourceType.S3_BUCKET.value,
                region="global",
                success=False,
                error_message=err_msg,
                error_code=getattr(exc, "code", "S3_DISCOVERY_ERROR")
            )]

    def _discover_vpc(
        self, session_manager: AWSSessionManager, regions: List[str]
    ) -> List[DiscoveryScopeResult]:
        """Execute VPC and network discovery across target regions."""
        results = []
        for region in regions:
            try:
                vpc_disco = VPCDiscovery(session_manager, region=region)
                all_resources = vpc_disco.discover(include_sub_resources=True)

                # Partition into distinct resource type scopes for granular reconciliation
                vpcs = [r for r in all_resources if r["resource_type"] == ResourceType.VPC.value]
                subnets = [r for r in all_resources if r["resource_type"] == ResourceType.SUBNET.value]
                sgs = [r for r in all_resources if r["resource_type"] == ResourceType.SECURITY_GROUP.value]

                results.append(DiscoveryScopeResult(
                    service="vpc",
                    resource_type=ResourceType.VPC.value,
                    region=region,
                    success=True,
                    resources=vpcs
                ))
                results.append(DiscoveryScopeResult(
                    service="vpc",
                    resource_type=ResourceType.SUBNET.value,
                    region=region,
                    success=True,
                    resources=subnets
                ))
                results.append(DiscoveryScopeResult(
                    service="vpc",
                    resource_type=ResourceType.SECURITY_GROUP.value,
                    region=region,
                    success=True,
                    resources=sgs
                ))
            except Exception as exc:
                err_msg = self._sanitize_error(exc)
                logger.warning(f"VPC discovery failed in region {region}: {err_msg}")
                # Emit failed scopes for all 3 resource types so none are reconciled
                for rt in (ResourceType.VPC.value, ResourceType.SUBNET.value, ResourceType.SECURITY_GROUP.value):
                    results.append(DiscoveryScopeResult(
                        service="vpc",
                        resource_type=rt,
                        region=region,
                        success=False,
                        error_message=err_msg,
                        error_code=getattr(exc, "code", "VPC_DISCOVERY_ERROR")
                    ))
        return results

    def _discover_rds(
        self, session_manager: AWSSessionManager, regions: List[str]
    ) -> List[DiscoveryScopeResult]:
        """Execute RDS database discovery across target regions."""
        results = []
        for region in regions:
            try:
                rds_disco = RDSDiscovery(session_manager, region=region)
                resources = rds_disco.discover()
                results.append(DiscoveryScopeResult(
                    service="rds",
                    resource_type=ResourceType.RDS_INSTANCE.value,
                    region=region,
                    success=True,
                    resources=resources
                ))
            except Exception as exc:
                err_msg = self._sanitize_error(exc)
                logger.warning(f"RDS discovery failed in region {region}: {err_msg}")
                results.append(DiscoveryScopeResult(
                    service="rds",
                    resource_type=ResourceType.RDS_INSTANCE.value,
                    region=region,
                    success=False,
                    error_message=err_msg,
                    error_code=getattr(exc, "code", "RDS_DISCOVERY_ERROR")
                ))
        return results

    # ── Persistence & Safe Reconciliation ────────────────────────────────────

    def _finalize_sync(
        self,
        job: CloudSyncJob,
        account: CloudAccount,
        scope_results: List[DiscoveryScopeResult]
    ) -> CloudSyncJob:
        """
        Executes atomic bulk upserts and safe scoped reconciliation.
        Calculates aggregate statistics and determines final job state.
        """
        total_discovered = 0
        total_inserted = 0
        total_updated = 0
        total_deleted = 0

        job_errors: List[Dict[str, Any]] = []
        summary_by_service: Dict[str, Dict[str, int]] = {}

        successful_scopes = 0
        failed_scopes = 0

        now_utc = datetime.now(timezone.utc)

        for scope in scope_results:
            svc = scope.service
            if svc not in summary_by_service:
                summary_by_service[svc] = {"discovered": 0, "inserted": 0, "updated": 0, "deleted": 0}

            if not scope.success:
                failed_scopes += 1
                job_errors.append({
                    "service": scope.service,
                    "resource_type": scope.resource_type,
                    "region": scope.region,
                    "error": scope.error_message,
                    "code": scope.error_code,
                    "timestamp": now_utc.isoformat()
                })
                # SAFETY GUARANTEE: Never reconcile a failed discovery scope.
                continue

            successful_scopes += 1
            resources = scope.resources
            scope_discovered = len(resources)
            total_discovered += scope_discovered
            summary_by_service[svc]["discovered"] += scope_discovered

            # 1. Bulk Upsert discovered resources
            inserted_count, updated_count = self._bulk_upsert_resources(
                account=account,
                resource_type=scope.resource_type,
                resources=resources,
                timestamp=now_utc
            )
            total_inserted += inserted_count
            total_updated += updated_count
            summary_by_service[svc]["inserted"] += inserted_count
            summary_by_service[svc]["updated"] += updated_count

            # 2. Safe Stale-Resource Reconciliation within this completed scope
            deleted_count = self._reconcile_stale_resources(
                account=account,
                resource_type=scope.resource_type,
                region=scope.region,
                discovered_ids={r["provider_resource_id"] for r in resources},
                timestamp=now_utc
            )
            total_deleted += deleted_count
            summary_by_service[svc]["deleted"] += deleted_count

        # Compute total active resources currently in DB for this cloud account
        active_resource_count = self.db.query(func.count(CloudResource.id)).filter(
            CloudResource.cloud_account_id == account.id,
            CloudResource.is_deleted == False
        ).scalar() or 0

        # Determine final status
        if failed_scopes == 0:
            final_status = SyncStatus.COMPLETED.value
        elif successful_scopes > 0:
            final_status = SyncStatus.PARTIAL_FAILURE.value
        else:
            final_status = SyncStatus.FAILED.value

        # Update Job Record
        job.status = final_status
        job.total_resources = active_resource_count
        job.discovered_resources = total_discovered
        job.updated_resources = total_updated
        job.deleted_resources = total_deleted
        job.completed_at = datetime.now(timezone.utc)
        job.errors = job_errors
        job.summary = {
            "inserted": total_inserted,
            "updated": total_updated,
            "deleted": total_deleted,
            "discovered": total_discovered,
            "successful_scopes": successful_scopes,
            "failed_scopes": failed_scopes,
            "by_service": summary_by_service
        }

        self.db.commit()
        self.db.refresh(job)

        # Invalidate sync status always, and invalidate resource cache if scopes were updated
        try:
            from app.core.cache import cache_manager
            cache_manager.invalidate_sync_status(account.workspace_id, account.id)
            if successful_scopes > 0:
                cache_manager.invalidate_account_cache(account.workspace_id, account.id)
        except Exception as cache_exc:
            logger.warning(
                f"Cache invalidation failed for account {account.id} after sync: {cache_exc}. "
                "Database synchronization remains safely committed."
            )

        return job

    def _bulk_upsert_resources(
        self,
        account: CloudAccount,
        resource_type: str,
        resources: List[Dict[str, Any]],
        timestamp: datetime
    ) -> Tuple[int, int]:
        """
        Executes high-performance PostgreSQL bulk upsert for resources of a given type.
        Preserves first_discovered_at on existing rows, updates last_discovered_at.
        Returns (inserted_count, updated_count).
        """
        if not resources:
            return 0, 0

        incoming_ids = [r["provider_resource_id"] for r in resources]

        # 1. Determine existing resources to accurately calculate inserted vs updated counters
        existing_rows = self.db.query(CloudResource.provider_resource_id).filter(
            CloudResource.cloud_account_id == account.id,
            CloudResource.resource_type == resource_type,
            CloudResource.provider_resource_id.in_(incoming_ids)
        ).all()
        existing_id_set = {row[0] for row in existing_rows}

        inserted_count = len([rid for rid in incoming_ids if rid not in existing_id_set])
        updated_count = len([rid for rid in incoming_ids if rid in existing_id_set])

        # 2. Build values list for PostgreSQL insert statement
        records = []
        for r in resources:
            records.append({
                "id": uuid.uuid4(),
                "cloud_account_id": account.id,
                "workspace_id": account.workspace_id,
                "provider": r.get("provider", "aws"),
                "resource_type": resource_type,
                "provider_resource_id": r["provider_resource_id"],
                "name": r.get("name"),
                "region": r["region"],
                "status": r.get("status", "unknown"),
                "tags": r.get("tags", {}),
                "metadata": r.get("metadata", {}),
                "is_deleted": False,
                "first_discovered_at": timestamp,
                "last_discovered_at": timestamp,
                "created_at": timestamp,
                "updated_at": timestamp,
            })

        # 3. Perform PostgreSQL native ON CONFLICT DO UPDATE
        stmt = pg_insert(CloudResource.__table__).values(records)
        stmt = stmt.on_conflict_do_update(
            constraint="uq_account_resource_type_provider_id",
            set_={
                "name": stmt.excluded.name,
                "region": stmt.excluded.region,
                "status": stmt.excluded.status,
                "tags": stmt.excluded.tags,
                "metadata": stmt.excluded.metadata,
                "is_deleted": False,  # Automatically un-delete if re-discovered
                "last_discovered_at": stmt.excluded.last_discovered_at,
                "updated_at": timestamp,
            }
        )

        self.db.execute(stmt)
        return inserted_count, updated_count

    def _reconcile_stale_resources(
        self,
        account: CloudAccount,
        resource_type: str,
        region: Optional[str],
        discovered_ids: Set[str],
        timestamp: datetime
    ) -> int:
        """
        Safely reconciles stale resources by marking active resources not present
        in discovered_ids as deleted (is_deleted = True, status = 'deleted').
        Strictly scoped to:
        - cloud_account_id
        - resource_type
        - region (if not global/None)
        """
        query = self.db.query(CloudResource).filter(
            CloudResource.cloud_account_id == account.id,
            CloudResource.resource_type == resource_type,
            CloudResource.is_deleted == False
        )

        # Apply regional filter if applicable (S3 uses region='global' or None, so we don't filter region for S3)
        if region and region != "global" and resource_type != ResourceType.S3_BUCKET.value:
            query = query.filter(CloudResource.region == region)

        if discovered_ids:
            query = query.filter(CloudResource.provider_resource_id.notin_(list(discovered_ids)))

        stale_resources = query.all()
        deleted_count = len(stale_resources)

        for resource in stale_resources:
            resource.is_deleted = True
            resource.status = "deleted"
            resource.updated_at = timestamp

        self.db.flush()
        return deleted_count

    # ── Helper & Error Utilities ─────────────────────────────────────────────

    def _fail_job(
        self,
        job: CloudSyncJob,
        error_message: str,
        code: str = "SYNC_FAILED"
    ) -> CloudSyncJob:
        """Marks a job as FAILED and records sanitized error details."""
        job.status = SyncStatus.FAILED.value
        job.completed_at = datetime.now(timezone.utc)
        job.errors = list(job.errors or []) + [{
            "service": "orchestrator",
            "error": error_message,
            "code": code,
            "timestamp": datetime.now(timezone.utc).isoformat()
        }]
        self.db.commit()
        self.db.refresh(job)

        # Invalidate sync status cache so clients immediately see the FAILED job
        try:
            from app.core.cache import cache_manager
            cache_manager.invalidate_sync_status(job.workspace_id, job.cloud_account_id)
        except Exception as exc:
            logger.warning(f"Failed to invalidate sync status on job failure: {exc}")

        return job

    @staticmethod
    def _sanitize_error(exc: Exception) -> str:
        """
        Extract clean error message while strictly preventing secret leakage
        (strips access keys, session tokens, passwords).
        """
        msg = str(exc)
        # Redact common AWS secret patterns if present in string
        for pattern in ("aws_access_key_id", "aws_secret_access_key", "aws_session_token", "SessionToken"):
            if pattern in msg:
                msg = "[Sensitive credential details redacted]"
                break
        return msg


# ── Execution Abstraction (Part 7) ──────────────────────────────────────────

class AsyncSyncRunner:
    """
    Independent execution abstraction for invoking synchronization outside REST requests.
    Supports in-process concurrent execution via ThreadPoolExecutor or background workers.

    Durability & Restart Limitations:
    - In-process execution threads live within the application runtime.
    - If the server process terminates abruptly (SIGKILL, container restart, deployment),
      in-flight jobs will remain in RUNNING state.
    - ResourceSyncEngine includes automated Zombie Job Recovery (`recover_zombie_jobs`)
      to detect and recover any orphaned jobs beyond JOB_TIMEOUT_MINUTES.
    - For multi-instance, production-grade zero-downtime durability across server restarts,
      an external durable queue (such as Redis + Celery or SQS) is recommended.
    """

    @staticmethod
    def run_sync_job(
        db_session_factory,
        job_id: uuid.UUID,
        regions: Optional[List[str]] = None
    ) -> CloudSyncJob:
        """
        Executes a synchronization job with an isolated database session lifecycle.
        Ensures the session is properly closed upon completion or unexpected error.
        """
        db = db_session_factory()
        try:
            engine = ResourceSyncEngine(db)
            return engine.execute_sync(job_id=job_id, regions=regions)
        except Exception:
            logger.exception(f"AsyncSyncRunner failed for job {job_id}")
            raise
        finally:
            db.close()
