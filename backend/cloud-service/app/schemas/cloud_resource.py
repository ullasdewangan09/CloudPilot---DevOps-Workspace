import uuid
from datetime import datetime
from typing import Optional, Dict, Any, List
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


# ── CloudResource Schemas ────────────────────────────────────────────────────

class CloudResourceBase(BaseModel):
    provider: str = "aws"
    resource_type: str
    provider_resource_id: str
    name: Optional[str] = None
    region: str
    status: str = "unknown"
    tags: Dict[str, Any] = Field(default_factory=dict)
    metadata: Dict[str, Any] = Field(default_factory=dict)


class CloudResourceCreate(CloudResourceBase):
    cloud_account_id: uuid.UUID
    workspace_id: uuid.UUID


class CloudResourceResponse(CloudResourceBase):
    id: uuid.UUID
    cloud_account_id: uuid.UUID
    workspace_id: uuid.UUID
    is_deleted: bool = False
    first_discovered_at: datetime
    last_discovered_at: datetime
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)

    @model_validator(mode="before")
    @classmethod
    def extract_orm_attributes(cls, data: Any) -> Any:
        """
        Safely extract resource_metadata and attributes from SQLAlchemy ORM instances,
        preventing attribute collision between SQLAlchemy Base.metadata and Column metadata.
        """
        if hasattr(data, "__dict__") or hasattr(data, "metadata_"):
            res_meta = getattr(data, "resource_metadata", None)
            if res_meta is None:
                res_meta = getattr(data, "metadata_", {})
            return {
                "id": getattr(data, "id", None),
                "cloud_account_id": getattr(data, "cloud_account_id", None),
                "workspace_id": getattr(data, "workspace_id", None),
                "provider": getattr(data, "provider", "aws"),
                "resource_type": getattr(data, "resource_type", ""),
                "provider_resource_id": getattr(data, "provider_resource_id", ""),
                "name": getattr(data, "name", None),
                "region": getattr(data, "region", ""),
                "status": getattr(data, "status", "unknown"),
                "tags": getattr(data, "tags", {}) or {},
                "metadata": res_meta or {},
                "is_deleted": bool(getattr(data, "is_deleted", False)),
                "first_discovered_at": getattr(data, "first_discovered_at", None),
                "last_discovered_at": getattr(data, "last_discovered_at", None),
                "created_at": getattr(data, "created_at", None),
                "updated_at": getattr(data, "updated_at", None),
            }
        return data


class CloudResourceListResponse(BaseModel):
    items: List[CloudResourceResponse]
    total: int
    page: int
    limit: int


class ResourceSummaryResponse(BaseModel):
    cloud_account_id: uuid.UUID
    total_active_resources: int
    ec2_instances: int
    s3_buckets: int
    vpc_resources: int
    rds_instances: int
    by_region: Dict[str, int] = Field(default_factory=dict)
    by_status: Dict[str, int] = Field(default_factory=dict)


# ── CloudSyncJob Schemas ─────────────────────────────────────────────────────

class CloudSyncJobBase(BaseModel):
    sync_type: str = "full"
    status: str = "pending"
    total_resources: int = 0
    discovered_resources: int = 0
    updated_resources: int = 0
    deleted_resources: int = 0
    summary: Dict[str, Any] = Field(default_factory=dict)
    errors: List[Dict[str, Any]] = Field(default_factory=list)


class CloudSyncJobResponse(CloudSyncJobBase):
    id: uuid.UUID
    cloud_account_id: uuid.UUID
    workspace_id: uuid.UUID
    triggered_by_user_id: Optional[int] = None
    started_at: datetime
    completed_at: Optional[datetime] = None
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


class CloudSyncJobListResponse(BaseModel):
    items: List[CloudSyncJobResponse]
    total: int
    page: int
    limit: int


class SyncStatusResponse(BaseModel):
    cloud_account_id: uuid.UUID
    has_active_job: bool
    active_job: Optional[CloudSyncJobResponse] = None
    latest_job: Optional[CloudSyncJobResponse] = None


class SyncTriggerRequest(BaseModel):
    sync_type: str = Field(
        default="full",
        description="Type of synchronization to perform: 'full', 'ec2', 's3', 'vpc', 'rds'"
    )

    @field_validator("sync_type")
    @classmethod
    def validate_sync_type(cls, v: str) -> str:
        valid_types = {"full", "ec2", "s3", "vpc", "rds"}
        val = v.lower().strip()
        if val not in valid_types:
            raise ValueError(f"Invalid sync_type '{v}'. Must be one of: {', '.join(sorted(valid_types))}")
        return val


class SyncTriggerResponse(BaseModel):
    job_id: uuid.UUID
    cloud_account_id: uuid.UUID
    status: str = "pending"
    sync_type: str = "full"
    message: str
    created_at: datetime


# ── Verification Schemas ─────────────────────────────────────────────────────

class CloudAccountVerifyResponse(BaseModel):
    status: str = "verified"
    provider: str = "aws"
    account_id: str
    assumed_role_arn: str
    user_id: Optional[str] = None
    verified_at: datetime
