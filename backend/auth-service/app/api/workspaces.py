import uuid
from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.security import get_current_user
from app.models.user import User
from app.schemas.workspace import (
    WorkspaceCreate,
    WorkspaceListResponse,
    WorkspaceResponse,
    WorkspaceUpdate,
    WorkspaceMemberCreate,
    WorkspaceMemberResponse
)
from app.services.workspace_service import (
    create_workspace,
    delete_workspace,
    get_workspace,
    list_workspaces,
    update_workspace,
    search_workspaces,
    get_workspace_stats,
    is_workspace_member,
    add_workspace_member,
    list_workspace_members,
    remove_workspace_member
)

router = APIRouter()


@router.post(
    "",
    response_model=WorkspaceResponse,
    status_code=status.HTTP_201_CREATED
)
def create_workspace_route(
    workspace: WorkspaceCreate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    return create_workspace(
        db,
        owner_id=current_user.id,
        workspace=workspace
    )


@router.get(
    "",
    response_model=WorkspaceListResponse
)
def list_workspace_route(
    page: int = Query(default=1, ge=1),
    limit: int = Query(default=10, ge=1, le=100),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    items, total = list_workspaces(
        db,
        user_id=current_user.id,
        page=page,
        limit=limit
    )

    return {
        "items": items,
        "page": page,
        "limit": limit,
        "total": total
    }


@router.get(
    "/stats",
    status_code=status.HTTP_200_OK
)
def get_workspace_stats_route(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    return get_workspace_stats(db, user_id=current_user.id)


@router.get(
    "/search",
    response_model=list[WorkspaceResponse],
    status_code=status.HTTP_200_OK
)
def search_workspaces_route(
    q: str = Query(default="", min_length=1),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    return search_workspaces(db, user_id=current_user.id, q=q)


@router.get(
    "/{workspace_id}",
    response_model=WorkspaceResponse
)
def get_workspace_route(
    workspace_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    workspace = get_workspace(db, workspace_id)
    if not workspace:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Workspace not found or inactive."
        )

    # Authorization Check: User must be owner OR member
    role = is_workspace_member(db, workspace_id, current_user.id)
    if workspace.owner_id != current_user.id and not role:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You do not have permission to access this workspace."
        )

    return workspace


@router.put(
    "/{workspace_id}",
    response_model=WorkspaceResponse
)
def update_workspace_route(
    workspace_id: uuid.UUID,
    data: WorkspaceUpdate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    workspace = get_workspace(db, workspace_id)
    if not workspace:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Workspace not found or inactive."
        )

    # Authorization Check: Only owner can update
    if workspace.owner_id != current_user.id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only the workspace owner can modify workspace settings."
        )

    return update_workspace(db, workspace, user_id=current_user.id, data=data)


@router.delete(
    "/{workspace_id}",
    status_code=status.HTTP_200_OK
)
def delete_workspace_route(
    workspace_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    workspace = get_workspace(db, workspace_id)
    if not workspace:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Workspace not found or inactive."
        )

    # Authorization Check: Only owner can delete
    if workspace.owner_id != current_user.id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only the workspace owner can delete this workspace."
        )

    delete_workspace(db, workspace, user_id=current_user.id)
    return {"message": "Workspace deleted successfully"}


# --- Workspace Membership Routes ---

@router.post(
    "/{workspace_id}/members",
    response_model=WorkspaceMemberResponse,
    status_code=status.HTTP_201_CREATED
)
def add_workspace_member_route(
    workspace_id: uuid.UUID,
    member_data: WorkspaceMemberCreate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    workspace = get_workspace(db, workspace_id)
    if not workspace:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Workspace not found."
        )

    # Authorization Check: Only owner or admin can manage members
    role = is_workspace_member(db, workspace_id, current_user.id)
    if workspace.owner_id != current_user.id and role != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only workspace owners or admins can manage members."
        )

    return add_workspace_member(
        db,
        workspace_id=workspace_id,
        user_id=member_data.user_id,
        role=member_data.role,
        executor_id=current_user.id
    )


@router.get(
    "/{workspace_id}/members",
    response_model=list[WorkspaceMemberResponse]
)
def list_workspace_members_route(
    workspace_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    workspace = get_workspace(db, workspace_id)
    if not workspace:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Workspace not found."
        )

    # Authorization Check: User must be member/owner to view member list
    role = is_workspace_member(db, workspace_id, current_user.id)
    if workspace.owner_id != current_user.id and not role:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You do not have access to view this workspace's members."
        )

    return list_workspace_members(db, workspace_id)


@router.delete(
    "/{workspace_id}/members/{user_id}",
    status_code=status.HTTP_200_OK
)
def remove_workspace_member_route(
    workspace_id: uuid.UUID,
    user_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    workspace = get_workspace(db, workspace_id)
    if not workspace:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Workspace not found."
        )

    # Authorization Check: Only owner or admin can remove members
    role = is_workspace_member(db, workspace_id, current_user.id)
    if workspace.owner_id != current_user.id and role != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only workspace owners or admins can manage members."
        )

    remove_workspace_member(
        db,
        workspace_id=workspace_id,
        user_id=user_id,
        executor_id=current_user.id
    )
    return {"message": "Member removed successfully"}
