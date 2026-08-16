import uuid
from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.security import get_current_user
from app.schemas.cloud_account import (
    CloudAccountCreate,
    CloudAccountUpdate,
    CloudAccountResponse,
    CloudAccountListResponse
)
from app.services.cloud_account_service import (
    create_cloud_account,
    list_cloud_accounts,
    get_cloud_account,
    update_cloud_account,
    delete_cloud_account
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
