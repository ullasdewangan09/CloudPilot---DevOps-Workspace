import logging
import uuid
from typing import Tuple, Optional, List, Any

from fastapi import BackgroundTasks, HTTPException, status
from sqlalchemy import Column, Integer, String, Boolean, or_, func
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Session, declarative_base

from app.models.cloud_account import CloudAccount
from app.models.cloud_resource import (
    CloudResource,
    CloudSyncJob,
    SyncStatus,
    SyncType,
    ResourceType,
)
from app.schemas.cloud_account import CloudAccountCreate, CloudAccountUpdate
from app.schemas.cloud_resource import (
    CloudAccountVerifyResponse,
    SyncTriggerResponse,
    SyncStatusResponse,
    ResourceSummaryResponse,
    CloudResourceResponse,
)
from app.core.security import encrypt_credential, decrypt_credential
from app.core.database import SessionLocal
from app.core.cache import cache_manager
from app.core.config import CACHE_TTL_SUMMARY, CACHE_TTL_LIST, CACHE_TTL_SYNC_STATUS
from app.providers.aws import AWSProvider
from app.providers.aws.session import AWSSessionManager, AWSSessionError
from app.services.resource_sync import ResourceSyncEngine, AsyncSyncRunner, ConcurrentSyncError

logger = logging.getLogger(__name__)

# ── RBAC configuration ──────────────────────────────────────────────────────
WRITE_ROLES = {"owner", "admin"}
READ_ROLES = {"owner", "admin", "developer", "viewer"}

# Read-only reference to shared workspace_members table
_Base = declarative_base()


class _WorkspaceMemberRef(_Base):
    """Read-only reference to the shared workspace_members table."""
    __tablename__ = "workspace_members"
    id = Column(Integer, primary_key=True)
    workspace_id = Column(PG_UUID(as_uuid=True), nullable=False)
    user_id = Column(Integer, nullable=False)
    role = Column(String, nullable=False)


class _WorkspaceRef(_Base):
    """Read-only reference to the shared workspaces table."""
    __tablename__ = "workspaces"
    id = Column(PG_UUID(as_uuid=True), primary_key=True)
    owner_id = Column(Integer, nullable=False)
    is_active = Column(Boolean, nullable=False, default=True)


# ── Workspace authorization helpers ─────────────────────────────────────────

def _get_workspace(db: Session, workspace_id: uuid.UUID) -> _WorkspaceRef:
    ws = db.query(_WorkspaceRef).filter(
        _WorkspaceRef.id == workspace_id,
        _WorkspaceRef.is_active == True
    ).first()
    if not ws:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Workspace not found or is inactive"
        )
    return ws


def _get_member_role(db: Session, workspace_id: uuid.UUID, user_id: int) -> Optional[str]:
    """Return user's role in workspace, or None if not a member."""
    member = db.query(_WorkspaceMemberRef).filter(
        _WorkspaceMemberRef.workspace_id == workspace_id,
        _WorkspaceMemberRef.user_id == user_id
    ).first()
    return member.role if member else None


def _require_workspace_access(
    db: Session,
    workspace_id: uuid.UUID,
    user_id: int,
    require_write: bool = False
) -> str:
    """
    Ensure user has access to the workspace.
    Returns the user's role.
    Raises 403 if unauthorized or lacks write permission.
    """
    ws = _get_workspace(db, workspace_id)

    # Owner of the workspace always has access
    if ws.owner_id == user_id:
        return "owner"

    role = _get_member_role(db, workspace_id, user_id)
    if role is None or role not in READ_ROLES:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You do not have access to this workspace"
        )

    if require_write and role not in WRITE_ROLES:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Your workspace role '{role}' does not permit this operation. "
                   f"Required roles: {', '.join(sorted(WRITE_ROLES))}"
        )

    return role


# ── CRUD Service Functions ───────────────────────────────────────────────────

def create_cloud_account(
    db: Session,
    current_user_id: int,
    data: CloudAccountCreate
) -> CloudAccount:
    _require_workspace_access(db, data.workspace_id, current_user_id, require_write=True)

    # Initial status and provider credential validation
    account_status = "pending"
    if data.provider.lower() == "aws":
        provider = AWSProvider(
            role_arn=data.role_arn,
            external_id=data.external_id,
            region=data.default_region or "us-east-1"
        )
        if data.role_arn:
            if provider.validate_credentials():
                account_status = "active"
            else:
                account_status = "error"

    # Encrypt sensitive credentials using Fernet before persisting to PostgreSQL
    encrypted_role_arn = encrypt_credential(data.role_arn)
    encrypted_external_id = encrypt_credential(data.external_id)

    account = CloudAccount(
        workspace_id=data.workspace_id,
        provider=data.provider.lower(),
        name=data.name,
        account_id=data.account_id,
        default_region=data.default_region,
        role_arn=encrypted_role_arn,
        external_id=encrypted_external_id,
        description=data.description,
        status=account_status,
        is_active=True
    )
    db.add(account)
    db.commit()
    db.refresh(account)
    return account


def list_cloud_accounts(
    db: Session,
    current_user_id: int,
    workspace_id: uuid.UUID,
    page: int = 1,
    limit: int = 10
) -> Tuple[List[CloudAccount], int]:
    _require_workspace_access(db, workspace_id, current_user_id, require_write=False)

    if page < 1:
        page = 1
    offset = (page - 1) * limit

    query = db.query(CloudAccount).filter(
        CloudAccount.workspace_id == workspace_id,
        CloudAccount.is_active == True
    )
    total = query.count()
    items = (
        query
        .order_by(CloudAccount.created_at.desc())
        .offset(offset)
        .limit(limit)
        .all()
    )
    return items, total


def get_cloud_account(
    db: Session,
    current_user_id: int,
    account_id: uuid.UUID
) -> CloudAccount:
    account = db.query(CloudAccount).filter(
        CloudAccount.id == account_id,
        CloudAccount.is_active == True
    ).first()

    if not account:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Cloud account not found"
        )

    _require_workspace_access(db, account.workspace_id, current_user_id, require_write=False)
    return account


def update_cloud_account(
    db: Session,
    current_user_id: int,
    account_id: uuid.UUID,
    data: CloudAccountUpdate
) -> CloudAccount:
    account = db.query(CloudAccount).filter(
        CloudAccount.id == account_id,
        CloudAccount.is_active == True
    ).first()

    if not account:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Cloud account not found"
        )

    _require_workspace_access(db, account.workspace_id, current_user_id, require_write=True)

    update_data = data.model_dump(exclude_unset=True)

    # Protect against accidental overwrite of encrypted credentials with empty or masked values
    if "role_arn" in update_data:
        val = update_data["role_arn"]
        if val and not val.startswith("***") and val.strip():
            update_data["role_arn"] = encrypt_credential(val)
        else:
            del update_data["role_arn"]

    if "external_id" in update_data:
        val = update_data["external_id"]
        if val and not val.startswith("***") and val.strip():
            update_data["external_id"] = encrypt_credential(val)
        else:
            del update_data["external_id"]

    for field, value in update_data.items():
        setattr(account, field, value)

    db.commit()
    db.refresh(account)

    # Invalidate cache on account update (scoped to workspace and account)
    try:
        cache_manager.invalidate_account_cache(account.workspace_id, account.id)
    except Exception as exc:
        logger.warning(f"Cache invalidation failed after account update: {exc}")

    return account


def delete_cloud_account(
    db: Session,
    current_user_id: int,
    account_id: uuid.UUID
) -> None:
    """Soft delete — sets is_active=False to preserve audit history."""
    account = db.query(CloudAccount).filter(
        CloudAccount.id == account_id,
        CloudAccount.is_active == True
    ).first()

    if not account:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Cloud account not found"
        )

    _require_workspace_access(db, account.workspace_id, current_user_id, require_write=True)

    account.is_active = False
    db.commit()

    # Invalidate cache for deactivated account
    try:
        cache_manager.invalidate_account_cache(account.workspace_id, account.id)
    except Exception as exc:
        logger.warning(f"Cache invalidation failed after account deactivation: {exc}")


# ==============================================================================
# PHASE 3B.5: DISCOVERY & SYNCHRONIZATION SERVICE FUNCTIONS
# ==============================================================================

# ── 1. AWS Connection Verification ──────────────────────────────────────────

def verify_cloud_account_connection(
    db: Session,
    current_user_id: int,
    account_id: uuid.UUID
) -> CloudAccountVerifyResponse:
    """
    Validates AWS cross-account AssumeRole connectivity and identity verification.
    Requires Owner or Admin role in the workspace.
    """
    account = get_cloud_account(db, current_user_id, account_id)
    _require_workspace_access(db, account.workspace_id, current_user_id, require_write=True)

    if not account.is_active:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Cannot verify an inactive cloud account"
        )

    if account.provider.lower() != "aws":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Provider '{account.provider}' does not support AWS verification"
        )

    try:
        session_mgr = AWSSessionManager.from_cloud_account(account)
        identity = session_mgr.assume_role()

        # Update account operational status to active on verified connection
        if account.status in ("pending", "error"):
            account.status = "active"
            db.commit()
            db.refresh(account)

        return CloudAccountVerifyResponse(
            status="verified",
            provider="aws",
            account_id=str(identity.get("account_id", "")),
            assumed_role_arn=str(identity.get("assumed_role_arn", "")),
            user_id=identity.get("user_id"),
            verified_at=identity.get("verified_at")
        )
    except AWSSessionError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"AWS connection verification failed: {exc.message}"
        )
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"AWS connection verification failed: {str(exc)}"
        )


# ── 2. Synchronization Execution ─────────────────────────────────────────────

def trigger_account_sync(
    db: Session,
    current_user_id: int,
    account_id: uuid.UUID,
    sync_type: str,
    background_tasks: BackgroundTasks,
    regions: Optional[List[str]] = None
) -> SyncTriggerResponse:
    """
    Creates and enqueues a CloudSyncJob for execution.
    Requires Owner or Admin role in the workspace.
    Returns HTTP 202 Accepted contract.
    """
    account = get_cloud_account(db, current_user_id, account_id)
    _require_workspace_access(db, account.workspace_id, current_user_id, require_write=True)

    if not account.is_active:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Cannot synchronize an inactive cloud account"
        )

    st_val = sync_type.lower().strip()
    valid_sync_types = {e.value for e in SyncType}
    if st_val not in valid_sync_types:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Invalid sync_type '{sync_type}'. Must be one of: {', '.join(sorted(valid_sync_types))}"
        )

    engine = ResourceSyncEngine(db)

    # Clean up any zombie jobs older than timeout limit
    engine.recover_zombie_jobs(account.id)

    # Check for active running synchronization to prevent state corruption
    active_job = db.query(CloudSyncJob).filter(
        CloudSyncJob.cloud_account_id == account.id,
        CloudSyncJob.status == SyncStatus.RUNNING.value
    ).first()

    if active_job:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"A synchronization job ({active_job.id}) is already running for this cloud account."
        )

    # Create the CloudSyncJob in PENDING state
    job = engine.create_sync_job(
        cloud_account_id=account.id,
        workspace_id=account.workspace_id,
        sync_type=st_val,
        triggered_by_user_id=current_user_id
    )

    # Dispatch to background task runner using isolated session factory
    background_tasks.add_task(
        AsyncSyncRunner.run_sync_job,
        db_session_factory=SessionLocal,
        job_id=job.id,
        regions=regions
    )

    return SyncTriggerResponse(
        job_id=job.id,
        cloud_account_id=account.id,
        status=job.status,
        sync_type=job.sync_type,
        message="Synchronization job accepted and queued for background execution",
        created_at=job.created_at
    )


# ── 3. Sync Status and Job Tracking ──────────────────────────────────────────

def get_sync_status(
    db: Session,
    current_user_id: int,
    account_id: uuid.UUID,
    bypass_cache: bool = False
) -> SyncStatusResponse:
    """
    Returns the latest synchronization status for a cloud account with Redis caching.
    Readable by all workspace members (Owner, Admin, Developer, Viewer).
    """
    account = get_cloud_account(db, current_user_id, account_id)
    _require_workspace_access(db, account.workspace_id, current_user_id, require_write=False)

    gen = cache_manager.get_generation(account.workspace_id, account.id)
    cache_key = cache_manager.build_sync_status_key(account.workspace_id, account.id, generation=gen)

    # 1. Check cache (unless explicitly bypassed)
    if not bypass_cache:
        cached_data = cache_manager.get(cache_key)
        if cached_data and isinstance(cached_data, dict):
            try:
                return SyncStatusResponse.model_validate(cached_data)
            except Exception as exc:
                logger.warning(f"Malformed sync status cache for key '{cache_key}': {exc}.")

    # 2. Query PostgreSQL (Source of Truth)
    engine = ResourceSyncEngine(db)
    engine.recover_zombie_jobs(account.id)

    active_job = db.query(CloudSyncJob).filter(
        CloudSyncJob.cloud_account_id == account.id,
        CloudSyncJob.status == SyncStatus.RUNNING.value
    ).first()

    latest_job = db.query(CloudSyncJob).filter(
        CloudSyncJob.cloud_account_id == account.id
    ).order_by(CloudSyncJob.started_at.desc()).first()

    response = SyncStatusResponse(
        cloud_account_id=account.id,
        has_active_job=bool(active_job),
        active_job=active_job,
        latest_job=latest_job
    )

    # 3. Populate Redis Cache only if generation has not changed during read
    current_gen = cache_manager.get_generation(account.workspace_id, account.id)
    if current_gen == gen:
        try:
            cache_manager.set(cache_key, response.model_dump(), ttl=CACHE_TTL_SYNC_STATUS)
        except Exception as exc:
            logger.warning(f"Failed to cache sync status for key '{cache_key}': {exc}")

    return response


def list_sync_jobs(
    db: Session,
    current_user_id: int,
    account_id: uuid.UUID,
    page: int = 1,
    limit: int = 10,
    status_filter: Optional[str] = None
) -> Tuple[List[CloudSyncJob], int]:
    """
    Paginated list of synchronization jobs for a cloud account.
    Readable by all workspace members.
    """
    account = get_cloud_account(db, current_user_id, account_id)
    _require_workspace_access(db, account.workspace_id, current_user_id, require_write=False)

    query = db.query(CloudSyncJob).filter(
        CloudSyncJob.cloud_account_id == account.id
    )

    if status_filter:
        query = query.filter(CloudSyncJob.status == status_filter.lower().strip())

    total = query.count()
    offset = (page - 1) * limit
    items = query.order_by(CloudSyncJob.started_at.desc()).offset(offset).limit(limit).all()
    return items, total


def get_sync_job(
    db: Session,
    current_user_id: int,
    account_id: uuid.UUID,
    job_id: uuid.UUID
) -> CloudSyncJob:
    """
    Retrieve specific sync job details by UUID.
    Readable by all workspace members.
    """
    account = get_cloud_account(db, current_user_id, account_id)
    _require_workspace_access(db, account.workspace_id, current_user_id, require_write=False)

    job = db.query(CloudSyncJob).filter(
        CloudSyncJob.id == job_id,
        CloudSyncJob.cloud_account_id == account.id
    ).first()

    if not job:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Sync job not found"
        )
    return job


# ── 4. Resource Queries & Aggregations ───────────────────────────────────────

def list_resources(
    db: Session,
    current_user_id: int,
    account_id: uuid.UUID,
    page: int = 1,
    limit: int = 20,
    resource_type: Optional[str] = None,
    region: Optional[str] = None,
    status_filter: Optional[str] = None,
    search: Optional[str] = None,
    include_deleted: bool = False,
    bypass_cache: bool = False
) -> Tuple[List[Any], int]:
    """
    Database-side filtered and paginated resource listing with Redis caching.
    Readable by all workspace members.
    """
    account = get_cloud_account(db, current_user_id, account_id)
    _require_workspace_access(db, account.workspace_id, current_user_id, require_write=False)

    # Normalize query filters for canonical cache key generation
    norm_resource_type = resource_type.lower().strip() if resource_type and resource_type.strip() else None
    norm_region = region.lower().strip() if region and region.strip() else None
    norm_status = status_filter.lower().strip() if status_filter and status_filter.strip() else None
    norm_search = search.strip() if search and search.strip() else None

    cache_params = {
        "page": page,
        "limit": limit,
        "resource_type": norm_resource_type,
        "region": norm_region,
        "status": norm_status,
        "search": norm_search,
        "include_deleted": bool(include_deleted),
    }

    gen = cache_manager.get_generation(account.workspace_id, account.id)
    cache_key = cache_manager.build_list_key(account.workspace_id, account.id, cache_params, generation=gen)

    # 1. Check cache (unless explicitly bypassed)
    if not bypass_cache:
        cached_data = cache_manager.get(cache_key)
        if cached_data and isinstance(cached_data, dict) and "items" in cached_data and "total" in cached_data:
            try:
                cached_items = [CloudResourceResponse.model_validate(item) for item in cached_data["items"]]
                return cached_items, int(cached_data["total"])
            except Exception as exc:
                logger.warning(f"Malformed list cache for key '{cache_key}': {exc}. Refetching from database.")

    # 2. Query PostgreSQL (Source of Truth)
    query = db.query(CloudResource).filter(
        CloudResource.cloud_account_id == account.id
    )

    if not include_deleted:
        query = query.filter(CloudResource.is_deleted == False)

    if norm_resource_type:
        query = query.filter(CloudResource.resource_type == norm_resource_type)

    if norm_region:
        query = query.filter(CloudResource.region == norm_region)

    if norm_status:
        query = query.filter(CloudResource.status == norm_status)

    if norm_search:
        term = f"%{norm_search}%"
        query = query.filter(
            or_(
                CloudResource.name.ilike(term),
                CloudResource.provider_resource_id.ilike(term)
            )
        )

    total = query.count()
    offset = (page - 1) * limit
    items = query.order_by(CloudResource.created_at.desc()).offset(offset).limit(limit).all()

    # 3. Populate Redis Cache only if generation has not changed during database read
    current_gen = cache_manager.get_generation(account.workspace_id, account.id)
    if current_gen == gen:
        try:
            serialized_items = [CloudResourceResponse.model_validate(item).model_dump() for item in items]
            cache_manager.set(
                cache_key,
                {"items": serialized_items, "total": total},
                ttl=CACHE_TTL_LIST
            )
        except Exception as exc:
            logger.warning(f"Failed to cache resource listing for key '{cache_key}': {exc}")
    else:
        logger.info(
            f"Skipping stale list cache population for account {account.id}: "
            f"generation changed from {gen} to {current_gen} during DB read."
        )

    return items, total


def get_resource(
    db: Session,
    current_user_id: int,
    account_id: uuid.UUID,
    resource_id: uuid.UUID,
    include_deleted: bool = False
) -> CloudResource:
    """
    Retrieve specific cloud resource by UUID.
    Readable by all workspace members.
    """
    account = get_cloud_account(db, current_user_id, account_id)
    _require_workspace_access(db, account.workspace_id, current_user_id, require_write=False)

    query = db.query(CloudResource).filter(
        CloudResource.id == resource_id,
        CloudResource.cloud_account_id == account.id
    )

    if not include_deleted:
        query = query.filter(CloudResource.is_deleted == False)

    res = query.first()
    if not res:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Resource not found"
        )
    return res


def get_resource_summary(
    db: Session,
    current_user_id: int,
    account_id: uuid.UUID,
    include_deleted: bool = False,
    bypass_cache: bool = False
) -> ResourceSummaryResponse:
    """
    Calculates database-side aggregate resource statistics for an account with Redis caching.
    Readable by all workspace members.
    """
    account = get_cloud_account(db, current_user_id, account_id)
    _require_workspace_access(db, account.workspace_id, current_user_id, require_write=False)

    gen = cache_manager.get_generation(account.workspace_id, account.id)
    cache_key = cache_manager.build_summary_key(
        account.workspace_id, account.id, include_deleted=include_deleted, generation=gen
    )

    # 1. Check cache (unless explicitly bypassed)
    if not bypass_cache:
        cached_data = cache_manager.get(cache_key)
        if cached_data and isinstance(cached_data, dict):
            try:
                return ResourceSummaryResponse.model_validate(cached_data)
            except Exception as exc:
                logger.warning(f"Malformed summary cache for key '{cache_key}': {exc}. Refetching from database.")

    # 2. Query PostgreSQL (Source of Truth)
    filters = [CloudResource.cloud_account_id == account.id]
    if not include_deleted:
        filters.append(CloudResource.is_deleted == False)

    total_active = db.query(func.count(CloudResource.id)).filter(*filters).scalar() or 0

    ec2_count = db.query(func.count(CloudResource.id)).filter(
        *filters, CloudResource.resource_type == ResourceType.EC2_INSTANCE.value
    ).scalar() or 0

    s3_count = db.query(func.count(CloudResource.id)).filter(
        *filters, CloudResource.resource_type == ResourceType.S3_BUCKET.value
    ).scalar() or 0

    vpc_types = [
        ResourceType.VPC.value,
        ResourceType.SUBNET.value,
        ResourceType.SECURITY_GROUP.value,
        ResourceType.ROUTE_TABLE.value,
        ResourceType.INTERNET_GATEWAY.value,
        ResourceType.NAT_GATEWAY.value,
    ]
    vpc_count = db.query(func.count(CloudResource.id)).filter(
        *filters, CloudResource.resource_type.in_(vpc_types)
    ).scalar() or 0

    rds_types = [
        ResourceType.RDS_INSTANCE.value,
        ResourceType.RDS_CLUSTER.value,
    ]
    rds_count = db.query(func.count(CloudResource.id)).filter(
        *filters, CloudResource.resource_type.in_(rds_types)
    ).scalar() or 0

    # Aggregate by region
    region_rows = db.query(
        CloudResource.region, func.count(CloudResource.id)
    ).filter(*filters).group_by(CloudResource.region).all()
    by_region = {row[0]: row[1] for row in region_rows if row[0]}

    # Aggregate by operational status
    status_rows = db.query(
        CloudResource.status, func.count(CloudResource.id)
    ).filter(*filters).group_by(CloudResource.status).all()
    by_status = {row[0]: row[1] for row in status_rows if row[0]}

    summary = ResourceSummaryResponse(
        cloud_account_id=account.id,
        total_active_resources=total_active,
        ec2_instances=ec2_count,
        s3_buckets=s3_count,
        vpc_resources=vpc_count,
        rds_instances=rds_count,
        by_region=by_region,
        by_status=by_status
    )

    # 3. Populate Redis Cache only if generation has not changed during database read
    current_gen = cache_manager.get_generation(account.workspace_id, account.id)
    if current_gen == gen:
        try:
            cache_manager.set(cache_key, summary.model_dump(), ttl=CACHE_TTL_SUMMARY)
        except Exception as exc:
            logger.warning(f"Failed to cache resource summary for key '{cache_key}': {exc}")
    else:
        logger.info(
            f"Skipping stale summary cache population for account {account.id}: "
            f"generation changed from {gen} to {current_gen} during DB read."
        )

    return summary

