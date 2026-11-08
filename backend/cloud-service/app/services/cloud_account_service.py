import uuid
from typing import Tuple, Optional, List

from fastapi import HTTPException, status
from sqlalchemy import Column, Integer, String, Boolean
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Session, declarative_base

from app.models.cloud_account import CloudAccount
from app.schemas.cloud_account import CloudAccountCreate, CloudAccountUpdate
from app.core.security import encrypt_credential, decrypt_credential
from app.providers.aws import AWSProvider

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
