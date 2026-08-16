import uuid
from sqlalchemy import (
    Column, DateTime, ForeignKey, Integer, String, Text, Boolean, Enum
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.sql import func
import enum

from app.core.database import Base


class CloudProvider(str, enum.Enum):
    AWS = "aws"
    AZURE = "azure"
    GCP = "gcp"


class AccountStatus(str, enum.Enum):
    ACTIVE = "active"
    INACTIVE = "inactive"
    PENDING = "pending"
    ERROR = "error"


class _WorkspaceRef(Base):
    """Read-only reference to shared workspaces table for ORM foreign key resolution."""
    __tablename__ = "workspaces"
    id = Column(UUID(as_uuid=True), primary_key=True)


class CloudAccount(Base):
    __tablename__ = "cloud_accounts"

    id = Column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
        index=True
    )

    # Workspace this cloud account belongs to (FK references workspaces.id)
    workspace_id = Column(
        UUID(as_uuid=True),
        ForeignKey("workspaces.id", ondelete="CASCADE"),
        nullable=False,
        index=True
    )

    # Cloud provider — supports AWS now, extensible to Azure/GCP
    provider = Column(
        String(30),
        nullable=False,
        index=True
    )

    # Human-readable name for this account in CloudPilot
    name = Column(
        String(100),
        nullable=False
    )

    # Provider-specific account/project/subscription identifier
    account_id = Column(
        String(255),
        nullable=True
    )

    # Default region for this cloud account
    default_region = Column(
        String(100),
        nullable=True
    )

    # AWS-specific: IAM Role ARN for cross-account access (Phase 3B+)
    role_arn = Column(
        String(500),
        nullable=True
    )

    # AWS-specific: External ID for STS AssumeRole security (Phase 3B+)
    external_id = Column(
        String(255),
        nullable=True
    )

    # Account operational status
    status = Column(
        String(30),
        nullable=False,
        default=AccountStatus.PENDING
    )

    # Soft delete flag — inactive accounts are hidden from standard listing
    is_active = Column(
        Boolean,
        default=True,
        nullable=False,
        index=True
    )

    # Notes and description
    description = Column(
        Text,
        nullable=True
    )

    # Timestamps
    created_at = Column(
        DateTime(timezone=True),
        server_default=func.now()
    )

    updated_at = Column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now()
    )
