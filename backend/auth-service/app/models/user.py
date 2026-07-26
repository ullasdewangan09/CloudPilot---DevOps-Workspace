from sqlalchemy import Column, Integer, String, DateTime
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func

from app.core.database import Base


class User(Base):
    __tablename__ = "users"

    id = Column(
        Integer,
        primary_key=True,
        index=True
    )

    username = Column(
        String(100),
        unique=True,
        nullable=False
    )

    email = Column(
        String(255),
        unique=True,
        nullable=False
    )

    password_hash = Column(
        String,
        nullable=True
    )

    role = Column(
        String(50),
        default="developer",
        nullable=False
    )

    provider = Column(
        String(50),
        default="local",
        nullable=False
    )

    provider_id = Column(
        String(255),
        nullable=True
    )

    avatar_url = Column(
        String(500),
        nullable=True
    )

    created_at = Column(
        DateTime(timezone=True),
        server_default=func.now()
    )

    workspaces = relationship(
        "Workspace",
        back_populates="owner",
        cascade="all, delete-orphan"
    )

    workspace_memberships = relationship(
        "WorkspaceMember",
        back_populates="user",
        cascade="all, delete-orphan"
    )

    workspace_audit_logs = relationship(
        "WorkspaceAuditLog",
        back_populates="user",
        cascade="all, delete-orphan"
    )

