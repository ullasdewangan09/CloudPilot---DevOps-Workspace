import enum
import uuid
from sqlalchemy import (
    Column,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Boolean,
    UniqueConstraint,
    Index,
)
from sqlalchemy.dialects.postgresql import UUID, JSONB
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func

from app.core.database import Base


class SyncStatus(str, enum.Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    PARTIAL_FAILURE = "partial_failure"
    FAILED = "failed"


class SyncType(str, enum.Enum):
    FULL = "full"
    EC2 = "ec2"
    S3 = "s3"
    VPC = "vpc"
    RDS = "rds"


class ResourceType(str, enum.Enum):
    EC2_INSTANCE = "ec2_instance"
    S3_BUCKET = "s3_bucket"
    VPC = "vpc"
    SUBNET = "subnet"
    SECURITY_GROUP = "security_group"
    INTERNET_GATEWAY = "internet_gateway"
    NAT_GATEWAY = "nat_gateway"
    ROUTE_TABLE = "route_table"
    RDS_INSTANCE = "rds_instance"
    RDS_CLUSTER = "rds_cluster"


class _UserRef(Base):
    """Read-only reference to shared users table for ORM foreign key resolution."""
    __tablename__ = "users"
    __table_args__ = {"extend_existing": True}
    id = Column(Integer, primary_key=True)


class CloudResource(Base):
    """
    Normalized Cloud Resource model.
    Stores normalized infrastructure attributes discovered from cloud providers.
    Uses compound unique constraint on (cloud_account_id, resource_type, provider_resource_id)
    to prevent cross-type identifier collisions while supporting idempotent upserts.
    """
    __tablename__ = "cloud_resources"
    __table_args__ = (
        UniqueConstraint(
            "cloud_account_id",
            "resource_type",
            "provider_resource_id",
            name="uq_account_resource_type_provider_id"
        ),
        Index("ix_cloud_resources_account_type", "cloud_account_id", "resource_type"),
        Index("ix_cloud_resources_last_discovered", "cloud_account_id", "resource_type", "last_discovered_at"),
        {"extend_existing": True}
    )

    id = Column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
        index=True
    )

    # Cloud account this resource belongs to (FK references cloud_accounts.id)
    cloud_account_id = Column(
        UUID(as_uuid=True),
        ForeignKey("cloud_accounts.id", ondelete="CASCADE"),
        nullable=False,
        index=True
    )

    # Workspace this resource belongs to (FK references workspaces.id for tenant isolation)
    workspace_id = Column(
        UUID(as_uuid=True),
        ForeignKey("workspaces.id", ondelete="CASCADE"),
        nullable=False,
        index=True
    )

    # Provider name (aws, azure, gcp)
    provider = Column(
        String(30),
        nullable=False,
        default="aws",
        index=True
    )

    # Normalized resource type (ec2_instance, s3_bucket, vpc, etc.)
    resource_type = Column(
        String(50),
        nullable=False,
        index=True
    )

    # Native provider identifier (e.g., i-0123456789abcdef0, my-bucket, vpc-12345)
    provider_resource_id = Column(
        String(255),
        nullable=False,
        index=True
    )

    # Human-readable name or Tag:Name
    name = Column(
        String(255),
        nullable=True,
        index=True
    )

    # Cloud region (e.g., us-east-1, global)
    region = Column(
        String(100),
        nullable=False,
        index=True
    )

    # Normalized operational status (running, stopped, available, terminated, etc.)
    status = Column(
        String(50),
        nullable=False,
        default="unknown",
        index=True
    )

    # Cloud resource tags (e.g. {"Environment": "prod", "Owner": "devops"})
    tags = Column(
        JSONB,
        nullable=False,
        default=dict,
        server_default="{}"
    )

    # Service-specific metadata (e.g. instance type, IPs, CIDR, versioning, storage size)
    metadata_ = Column(
        "metadata",
        JSONB,
        nullable=False,
        default=dict,
        server_default="{}"
    )

    # Soft delete / reconciliation flag
    is_deleted = Column(
        Boolean,
        nullable=False,
        default=False,
        index=True
    )

    # Discovery timestamp tracking
    first_discovered_at = Column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now()
    )

    last_discovered_at = Column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now()
    )

    created_at = Column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now()
    )

    updated_at = Column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now()
    )

    # Relationship to parent CloudAccount
    account = relationship("CloudAccount", back_populates="resources")

    @property
    def resource_metadata(self):
        return self.metadata_

    @resource_metadata.setter
    def resource_metadata(self, val):
        self.metadata_ = val


class CloudSyncJob(Base):
    """
    Cloud Sync Job tracking model.
    Records metadata, statistics, errors, and progress for cloud discovery operations.
    """
    __tablename__ = "cloud_sync_jobs"
    __table_args__ = (
        Index("ix_cloud_sync_jobs_account_started", "cloud_account_id", "started_at"),
        {"extend_existing": True}
    )

    id = Column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
        index=True
    )

    # Cloud account targeted by this synchronization job
    cloud_account_id = Column(
        UUID(as_uuid=True),
        ForeignKey("cloud_accounts.id", ondelete="CASCADE"),
        nullable=False,
        index=True
    )

    # Workspace this synchronization job belongs to
    workspace_id = Column(
        UUID(as_uuid=True),
        ForeignKey("workspaces.id", ondelete="CASCADE"),
        nullable=False,
        index=True
    )

    # User who triggered the job (None if triggered by background automation)
    triggered_by_user_id = Column(
        Integer,
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
        index=True
    )

    # Operational status: pending, running, completed, partial_failure, failed
    status = Column(
        String(30),
        nullable=False,
        default=SyncStatus.PENDING,
        index=True
    )

    # Synchronization scope: full, ec2, s3, vpc, rds
    sync_type = Column(
        String(30),
        nullable=False,
        default=SyncType.FULL,
        index=True
    )

    # Resource counts
    total_resources = Column(
        Integer,
        nullable=False,
        default=0
    )

    discovered_resources = Column(
        Integer,
        nullable=False,
        default=0
    )

    updated_resources = Column(
        Integer,
        nullable=False,
        default=0
    )

    deleted_resources = Column(
        Integer,
        nullable=False,
        default=0
    )

    # Breakdown summary: e.g. {"ec2": 10, "s3": 4, "vpc": 2, "rds": 1}
    summary = Column(
        JSONB,
        nullable=False,
        default=dict,
        server_default="{}"
    )

    # Error log: e.g. [{"service": "rds", "region": "ap-south-1", "error": "AccessDenied"}]
    errors = Column(
        JSONB,
        nullable=False,
        default=list,
        server_default="[]"
    )

    # Worker ownership and execution tracking (Phase 3B.7)
    worker_id = Column(
        String(100),
        nullable=True,
        index=True
    )

    attempts = Column(
        Integer,
        nullable=False,
        default=0,
        server_default="0"
    )

    max_attempts = Column(
        Integer,
        nullable=False,
        default=3,
        server_default="3"
    )

    heartbeat_at = Column(
        DateTime(timezone=True),
        nullable=True,
        index=True
    )

    next_retry_at = Column(
        DateTime(timezone=True),
        nullable=True,
        index=True
    )

    # Job lifecycle timestamps
    started_at = Column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now()
    )

    completed_at = Column(
        DateTime(timezone=True),
        nullable=True
    )

    created_at = Column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now()
    )

    updated_at = Column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now()
    )

    # Relationship to parent CloudAccount
    account = relationship("CloudAccount", back_populates="sync_jobs")

