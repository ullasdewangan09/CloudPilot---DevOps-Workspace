import uuid
from typing import Optional, List
from fastapi import APIRouter, Depends, Query, BackgroundTasks, status
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.security import get_current_user
from app.schemas.cloud_account import (
    CloudAccountCreate,
    CloudAccountUpdate,
    CloudAccountResponse,
    CloudAccountListResponse,
)
from app.schemas.cloud_resource import (
    CloudAccountVerifyResponse,
    SyncTriggerRequest,
    SyncTriggerResponse,
    SyncStatusResponse,
    CloudSyncJobResponse,
    CloudSyncJobListResponse,
    CloudResourceResponse,
    CloudResourceListResponse,
    ResourceSummaryResponse,
)
from app.services.cloud_account_service import (
    create_cloud_account,
    list_cloud_accounts,
    get_cloud_account,
    update_cloud_account,
    delete_cloud_account,
    verify_cloud_account_connection,
    trigger_account_sync,
    get_sync_status,
    list_sync_jobs,
    get_sync_job,
    list_resources,
    get_resource,
    get_resource_summary,
)

router = APIRouter()


@router.post(
    "",
    response_model=CloudAccountResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Connect a cloud account to a workspace"
)
def create_account(
    data: CloudAccountCreate,
    current_user=Depends(get_current_user),
    db: Session = Depends(get_db)
):
    return create_cloud_account(db, current_user_id=current_user.id, data=data)


@router.get(
    "",
    response_model=CloudAccountListResponse,
    summary="List active cloud accounts in a workspace"
)
def list_accounts(
    workspace_id: uuid.UUID = Query(..., description="Workspace UUID to list accounts for"),
    page: int = Query(default=1, ge=1),
    limit: int = Query(default=10, ge=1, le=100),
    current_user=Depends(get_current_user),
    db: Session = Depends(get_db)
):
    items, total = list_cloud_accounts(
        db,
        current_user_id=current_user.id,
        workspace_id=workspace_id,
        page=page,
        limit=limit
    )
    return CloudAccountListResponse(
        items=items,
        total=total,
        page=page,
        limit=limit
    )


@router.get(
    "/{account_id}",
    response_model=CloudAccountResponse,
    summary="Get a specific cloud account"
)
def get_account(
    account_id: uuid.UUID,
    current_user=Depends(get_current_user),
    db: Session = Depends(get_db)
):
    return get_cloud_account(db, current_user_id=current_user.id, account_id=account_id)


@router.put(
    "/{account_id}",
    response_model=CloudAccountResponse,
    summary="Update a cloud account"
)
def update_account(
    account_id: uuid.UUID,
    data: CloudAccountUpdate,
    current_user=Depends(get_current_user),
    db: Session = Depends(get_db)
):
    return update_cloud_account(
        db,
        current_user_id=current_user.id,
        account_id=account_id,
        data=data
    )


@router.delete(
    "/{account_id}",
    status_code=status.HTTP_200_OK,
    summary="Soft-delete a cloud account"
)
def delete_account(
    account_id: uuid.UUID,
    current_user=Depends(get_current_user),
    db: Session = Depends(get_db)
):
    delete_cloud_account(db, current_user_id=current_user.id, account_id=account_id)
    return {"message": "Cloud account deactivated successfully"}


# ==============================================================================
# PHASE 3B.5: DISCOVERY, SYNCHRONIZATION & RESOURCE REST ENDPOINTS
# ==============================================================================

# ── 1. AWS Connection Verification Endpoint ──────────────────────────────────

@router.post(
    "/{account_id}/verify",
    response_model=CloudAccountVerifyResponse,
    status_code=status.HTTP_200_OK,
    summary="Verify AWS cross-account AssumeRole connection and CallerIdentity"
)
def verify_account(
    account_id: uuid.UUID,
    current_user=Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """
    Validates AWS cross-account STS AssumeRole credentials and verifies the returned
    CallerIdentity matches the configured AWS account ID.
    Requires Owner or Admin role in the workspace.
    """
    return verify_cloud_account_connection(db, current_user.id, account_id)


# ── 2. Resource Synchronization Endpoint ─────────────────────────────────────

@router.post(
    "/{account_id}/sync",
    response_model=SyncTriggerResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Trigger asynchronous resource synchronization for a cloud account"
)
def sync_account_resources(
    account_id: uuid.UUID,
    background_tasks: BackgroundTasks,
    payload: Optional[SyncTriggerRequest] = None,
    current_user=Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """
    Creates a CloudSyncJob and launches asynchronous resource synchronization in the background.
    Supports sync_type: 'full', 'ec2', 's3', 'vpc', 'rds'.
    Requires Owner or Admin role in the workspace.
    Returns HTTP 202 Accepted when successfully queued.

    Execution Limitation:
    Background execution uses in-process background task threads. If the application process
    terminates unexpectedly, in-flight jobs are recovered on subsequent runs via Zombie Job Recovery.
    """
    sync_type = payload.sync_type if payload else "full"
    return trigger_account_sync(
        db=db,
        current_user_id=current_user.id,
        account_id=account_id,
        sync_type=sync_type,
        background_tasks=background_tasks
    )


# ── 3. Sync Status and Job Tracking Endpoints ────────────────────────────────

@router.get(
    "/{account_id}/sync/status",
    response_model=SyncStatusResponse,
    status_code=status.HTTP_200_OK,
    summary="Get latest synchronization status for a cloud account"
)
def get_account_sync_status(
    account_id: uuid.UUID,
    current_user=Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """
    Returns latest synchronization state and currently running job (if any).
    Accessible to all workspace members (Owner, Admin, Developer, Viewer).
    """
    return get_sync_status(db, current_user.id, account_id)


@router.get(
    "/{account_id}/sync/jobs",
    response_model=CloudSyncJobListResponse,
    status_code=status.HTTP_200_OK,
    summary="List synchronization jobs for a cloud account"
)
def list_account_sync_jobs(
    account_id: uuid.UUID,
    page: int = Query(default=1, ge=1, description="Page number (1-based)"),
    limit: int = Query(default=10, ge=1, le=100, description="Items per page"),
    status: Optional[str] = Query(default=None, description="Filter by job status (pending, running, completed, partial_failure, failed)"),
    current_user=Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """
    Paginated list of historical synchronization jobs for this cloud account.
    Accessible to all workspace members.
    """
    items, total = list_sync_jobs(
        db=db,
        current_user_id=current_user.id,
        account_id=account_id,
        page=page,
        limit=limit,
        status_filter=status
    )
    return CloudSyncJobListResponse(items=items, total=total, page=page, limit=limit)


@router.get(
    "/{account_id}/sync/jobs/{job_id}",
    response_model=CloudSyncJobResponse,
    status_code=status.HTTP_200_OK,
    summary="Get details of a specific synchronization job"
)
def get_account_sync_job_detail(
    account_id: uuid.UUID,
    job_id: uuid.UUID,
    current_user=Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """
    Detailed breakdown, resource counters, and sanitized errors for a specific sync job.
    Accessible to all workspace members.
    """
    return get_sync_job(db, current_user.id, account_id, job_id)


# ── 4. Resource Endpoints ────────────────────────────────────────────────────

@router.get(
    "/{account_id}/resources/summary",
    response_model=ResourceSummaryResponse,
    status_code=status.HTTP_200_OK,
    summary="Get aggregate resource summary and statistics for a cloud account"
)
def get_account_resources_summary(
    account_id: uuid.UUID,
    include_deleted: bool = Query(default=False, description="Include soft-deleted resources in counts"),
    bypass_cache: bool = Query(default=False, description="Bypass Redis cache and force fresh database lookup"),
    current_user=Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """
    Returns database-aggregated counts of active resources, breakdowns by service (EC2, S3, VPC, RDS),
    regions, and operational statuses. Uses Redis caching with PostgreSQL fallback.
    Accessible to all workspace members.
    """
    return get_resource_summary(
        db,
        current_user.id,
        account_id,
        include_deleted=include_deleted,
        bypass_cache=bypass_cache
    )


@router.get(
    "/{account_id}/resources",
    response_model=CloudResourceListResponse,
    status_code=status.HTTP_200_OK,
    summary="List discovered cloud resources with filtering and pagination"
)
def list_account_resources(
    account_id: uuid.UUID,
    page: int = Query(default=1, ge=1, description="Page number"),
    limit: int = Query(default=20, ge=1, le=100, description="Items per page"),
    resource_type: Optional[str] = Query(default=None, description="Filter by resource_type (e.g. ec2_instance, s3_bucket)"),
    region: Optional[str] = Query(default=None, description="Filter by AWS region"),
    status: Optional[str] = Query(default=None, description="Filter by operational status"),
    search: Optional[str] = Query(default=None, description="Search resource name or provider ID"),
    include_deleted: bool = Query(default=False, description="Include soft-deleted resources"),
    bypass_cache: bool = Query(default=False, description="Bypass Redis cache and force fresh database lookup"),
    current_user=Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """
    Paginated and filtered listing of infrastructure resources discovered for this cloud account.
    Excludes soft-deleted resources by default. Uses Redis caching with PostgreSQL fallback.
    Accessible to all workspace members.
    """
    items, total = list_resources(
        db=db,
        current_user_id=current_user.id,
        account_id=account_id,
        page=page,
        limit=limit,
        resource_type=resource_type,
        region=region,
        status_filter=status,
        search=search,
        include_deleted=include_deleted,
        bypass_cache=bypass_cache
    )
    return CloudResourceListResponse(items=items, total=total, page=page, limit=limit)


@router.get(
    "/{account_id}/resources/{resource_id}",
    response_model=CloudResourceResponse,
    status_code=status.HTTP_200_OK,
    summary="Get details of a specific discovered cloud resource"
)
def get_account_resource_detail(
    account_id: uuid.UUID,
    resource_id: uuid.UUID,
    include_deleted: bool = Query(default=False, description="Include soft-deleted resources"),
    current_user=Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """
    Retrieve full details, tags, and sanitized metadata for a single discovered resource.
    Accessible to all workspace members.
    """
    return get_resource(db, current_user.id, account_id, resource_id, include_deleted=include_deleted)

