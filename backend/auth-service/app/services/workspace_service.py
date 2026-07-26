import uuid
from sqlalchemy import or_, func
from sqlalchemy.orm import Session
from fastapi import HTTPException, status

from app.models.workspace import Workspace, WorkspaceMember, WorkspaceAuditLog
from app.models.user import User
from app.schemas.workspace import WorkspaceCreate, WorkspaceUpdate


def log_audit(
    db: Session,
    workspace_id: uuid.UUID,
    user_id: int,
    action: str,
    details: str | None = None
):
    db_log = WorkspaceAuditLog(
        workspace_id=workspace_id,
        user_id=user_id,
        action=action,
        details=details
    )
    db.add(db_log)


def create_workspace(
    db: Session,
    owner_id: int,
    workspace: WorkspaceCreate
):
    # 1. Create workspace
    db_workspace = Workspace(
        name=workspace.name,
        description=workspace.description,
        environment=workspace.environment,
        icon=workspace.icon,
        color=workspace.color,
        owner_id=owner_id,
        is_active=True
    )
    db.add(db_workspace)
    db.commit()
    db.refresh(db_workspace)

    # 2. Add owner membership
    owner_member = WorkspaceMember(
        workspace_id=db_workspace.id,
        user_id=owner_id,
        role="owner"
    )
    db.add(owner_member)

    # 3. Log audit event
    log_audit(
        db,
        workspace_id=db_workspace.id,
        user_id=owner_id,
        action="WORKSPACE_CREATED",
        details=f"Workspace '{workspace.name}' created by owner."
    )
    db.commit()
    db.refresh(db_workspace)

    return db_workspace


def list_workspaces(
    db: Session,
    user_id: int,
    page: int,
    limit: int
):
    # Page must be at least 1
    if page < 1:
        page = 1

    offset = (page - 1) * limit

    # Select workspaces where user is owner OR a member
    query = db.query(Workspace).filter(
        Workspace.is_active == True,
        or_(
            Workspace.owner_id == user_id,
            Workspace.id.in_(
                db.query(WorkspaceMember.workspace_id)
                .filter(WorkspaceMember.user_id == user_id)
            )
        )
    )

    total = query.count()
    items = (
        query
        .order_by(Workspace.created_at.desc())
        .offset(offset)
        .limit(limit)
        .all()
    )

    return items, total


def get_workspace(
    db: Session,
    workspace_id: uuid.UUID
):
    return (
        db.query(Workspace)
        .filter(
            Workspace.id == workspace_id,
            Workspace.is_active == True
        )
        .first()
    )


def update_workspace(
    db: Session,
    workspace: Workspace,
    user_id: int,
    data: WorkspaceUpdate
):
    update_data = data.model_dump(exclude_unset=True)

    for field, value in update_data.items():
        setattr(workspace, field, value)

    log_audit(
        db,
        workspace_id=workspace.id,
        user_id=user_id,
        action="WORKSPACE_UPDATED",
        details=f"Workspace fields updated: {', '.join(update_data.keys())}"
    )

    db.commit()
    db.refresh(workspace)
    return workspace


def delete_workspace(
    db: Session,
    workspace: Workspace,
    user_id: int
):
    # Soft delete: set is_active=False
    workspace.is_active = False
    
    log_audit(
        db,
        workspace_id=workspace.id,
        user_id=user_id,
        action="WORKSPACE_DELETED",
        details="Workspace soft-deleted."
    )
    
    db.commit()


def search_workspaces(
    db: Session,
    user_id: int,
    q: str
):
    search_query = f"%{q}%"
    return (
        db.query(Workspace)
        .filter(
            Workspace.is_active == True,
            or_(
                Workspace.owner_id == user_id,
                Workspace.id.in_(
                    db.query(WorkspaceMember.workspace_id)
                    .filter(WorkspaceMember.user_id == user_id)
                )
            ),
            or_(
                Workspace.name.ilike(search_query),
                Workspace.description.ilike(search_query)
            )
        )
        .order_by(Workspace.created_at.desc())
        .all()
    )


def get_workspace_stats(
    db: Session,
    user_id: int
):
    # Fetch active workspaces user owns or belongs to
    user_workspaces_subquery = (
        db.query(Workspace.id)
        .filter(
            Workspace.is_active == True,
            or_(
                Workspace.owner_id == user_id,
                Workspace.id.in_(
                    db.query(WorkspaceMember.workspace_id)
                    .filter(WorkspaceMember.user_id == user_id)
                )
            )
        )
        .subquery()
    )

    # Aggregate counts by environment
    results = (
        db.query(Workspace.environment, func.count(Workspace.id))
        .filter(Workspace.id.in_(user_workspaces_subquery))
        .group_by(Workspace.environment)
        .all()
    )

    stats = {
        "total_workspaces": 0,
        "development": 0,
        "testing": 0,
        "production": 0,
        "staging": 0,
        "sandbox": 0
    }

    total = 0
    for env, count in results:
        if env in stats:
            stats[env] = count
            total += count

    stats["total_workspaces"] = total
    return stats


# --- Workspace Membership Services ---

def is_workspace_member(
    db: Session,
    workspace_id: uuid.UUID,
    user_id: int
) -> str | None:
    """Returns the member role if the user belongs to the workspace, else None."""
    member = (
        db.query(WorkspaceMember)
        .filter(
            WorkspaceMember.workspace_id == workspace_id,
            WorkspaceMember.user_id == user_id
        )
        .first()
    )
    if member:
        return member.role
    return None


def add_workspace_member(
    db: Session,
    workspace_id: uuid.UUID,
    user_id: int,
    role: str,
    executor_id: int
):
    # Check if user exists
    user_exists = db.query(User).filter(User.id == user_id).first()
    if not user_exists:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User to add not found."
        )

    # Check if user is already a member
    existing_role = is_workspace_member(db, workspace_id, user_id)
    if existing_role:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"User is already a member of this workspace with role: '{existing_role}'."
        )

    db_member = WorkspaceMember(
        workspace_id=workspace_id,
        user_id=user_id,
        role=role
    )
    db.add(db_member)

    log_audit(
        db,
        workspace_id=workspace_id,
        user_id=executor_id,
        action="WORKSPACE_MEMBER_ADDED",
        details=f"User ID {user_id} added as member with role '{role}'."
    )
    db.commit()
    return db_member


def list_workspace_members(
    db: Session,
    workspace_id: uuid.UUID
):
    # Returns members joined with User details for username/email display
    return (
        db.query(WorkspaceMember)
        .filter(WorkspaceMember.workspace_id == workspace_id)
        .order_by(WorkspaceMember.created_at.asc())
        .all()
    )


def remove_workspace_member(
    db: Session,
    workspace_id: uuid.UUID,
    user_id: int,
    executor_id: int
):
    member = (
        db.query(WorkspaceMember)
        .filter(
            WorkspaceMember.workspace_id == workspace_id,
            WorkspaceMember.user_id == user_id
        )
        .first()
    )

    if not member:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Member not found in this workspace."
        )

    if member.role == "owner":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="The owner cannot be removed from the workspace. Delete the workspace instead."
        )

    db.delete(member)

    log_audit(
        db,
        workspace_id=workspace_id,
        user_id=executor_id,
        action="WORKSPACE_MEMBER_REMOVED",
        details=f"User ID {user_id} removed from the workspace."
    )
    db.commit()
