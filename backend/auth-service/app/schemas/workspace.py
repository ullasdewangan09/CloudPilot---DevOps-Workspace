import uuid
from datetime import datetime
from pydantic import BaseModel, ConfigDict, Field, field_validator

ALLOWED_ENVIRONMENTS = {"development", "testing", "staging", "production", "sandbox"}
ALLOWED_ROLES = {"owner", "admin", "developer", "viewer"}


class WorkspaceBase(BaseModel):
    name: str = Field(min_length=1, max_length=100, description="Workspace name")
    description: str | None = Field(default=None, max_length=1000)
    environment: str = Field(description="Allowed environments: development, testing, staging, production, sandbox")
    icon: str | None = Field(default=None, max_length=255)
    color: str | None = Field(default=None, max_length=50)

    @field_validator("environment")
    @classmethod
    def validate_environment(cls, v: str) -> str:
        v_lower = v.lower()
        if v_lower not in ALLOWED_ENVIRONMENTS:
            raise ValueError(
                f"Invalid environment: '{v}'. Must be one of {', '.join(sorted(ALLOWED_ENVIRONMENTS))}"
            )
        return v_lower


class WorkspaceCreate(WorkspaceBase):
    pass


class WorkspaceUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=100)
    description: str | None = Field(default=None, max_length=1000)
    environment: str | None = Field(default=None)
    icon: str | None = Field(default=None, max_length=255)
    color: str | None = Field(default=None, max_length=50)

    @field_validator("environment")
    @classmethod
    def validate_environment(cls, v: str | None) -> str | None:
        if v is None:
            return None
        v_lower = v.lower()
        if v_lower not in ALLOWED_ENVIRONMENTS:
            raise ValueError(
                f"Invalid environment: '{v}'. Must be one of {', '.join(sorted(ALLOWED_ENVIRONMENTS))}"
            )
        return v_lower


class WorkspaceResponse(WorkspaceBase):
    id: uuid.UUID
    owner_id: int
    is_active: bool
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


class WorkspaceListResponse(BaseModel):
    items: list[WorkspaceResponse]
    page: int
    limit: int
    total: int

    model_config = ConfigDict(from_attributes=True)


class WorkspaceMemberCreate(BaseModel):
    user_id: int
    role: str = Field(description="Allowed roles: admin, developer, viewer")

    @field_validator("role")
    @classmethod
    def validate_role(cls, v: str) -> str:
        v_lower = v.lower()
        # Members can be admin, developer, or viewer. "owner" is automatically assigned upon workspace creation.
        if v_lower not in {"admin", "developer", "viewer"}:
            raise ValueError(
                f"Invalid role: '{v}'. Must be one of admin, developer, or viewer."
            )
        return v_lower


class WorkspaceMemberResponse(BaseModel):
    id: int
    workspace_id: uuid.UUID
    user_id: int
    role: str
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)
