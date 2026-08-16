from typing import Optional, List
from uuid import UUID
from datetime import datetime
from pydantic import BaseModel, field_validator, model_validator
import re

SUPPORTED_PROVIDERS = {"aws", "azure", "gcp"}
SUPPORTED_STATUSES = {"active", "inactive", "pending", "error"}

AWS_REGIONS = {
    "us-east-1", "us-east-2", "us-west-1", "us-west-2",
    "ap-south-1", "ap-northeast-1", "ap-northeast-2", "ap-northeast-3",
    "ap-southeast-1", "ap-southeast-2",
    "ca-central-1",
    "eu-central-1", "eu-west-1", "eu-west-2", "eu-west-3", "eu-north-1",
    "sa-east-1",
    "me-south-1", "af-south-1"
}


class CloudAccountCreate(BaseModel):
    name: str
    provider: str
    account_id: Optional[str] = None
    default_region: Optional[str] = None
    role_arn: Optional[str] = None
    external_id: Optional[str] = None
    description: Optional[str] = None
    workspace_id: UUID

    @field_validator("name")
    @classmethod
    def name_must_not_be_empty(cls, v):
        if not v or not v.strip():
            raise ValueError("name must not be empty")
        return v.strip()

    @field_validator("provider")
    @classmethod
    def provider_must_be_valid(cls, v):
        if v.lower() not in SUPPORTED_PROVIDERS:
            raise ValueError(
                f"provider must be one of: {', '.join(sorted(SUPPORTED_PROVIDERS))}"
            )
        return v.lower()

    @field_validator("default_region")
    @classmethod
    def region_must_be_valid_format(cls, v):
        if v is None:
            return v
        # General region format: lowercase letters, digits, hyphens (e.g. us-east-1)
        if not re.match(r'^[a-z][a-z0-9-]{1,30}$', v):
            raise ValueError(
                "default_region must be a valid cloud region identifier (e.g. us-east-1)"
            )
        return v


class CloudAccountUpdate(BaseModel):
    name: Optional[str] = None
    account_id: Optional[str] = None
    default_region: Optional[str] = None
    role_arn: Optional[str] = None
    external_id: Optional[str] = None
    description: Optional[str] = None
    status: Optional[str] = None

    @field_validator("name")
    @classmethod
    def name_must_not_be_empty(cls, v):
        if v is not None and not v.strip():
            raise ValueError("name must not be empty")
        return v.strip() if v else v

    @field_validator("status")
    @classmethod
    def status_must_be_valid(cls, v):
        if v is not None and v.lower() not in SUPPORTED_STATUSES:
            raise ValueError(
                f"status must be one of: {', '.join(sorted(SUPPORTED_STATUSES))}"
            )
        return v.lower() if v else v

    @field_validator("default_region")
    @classmethod
    def region_must_be_valid_format(cls, v):
        if v is None:
            return v
        if not re.match(r'^[a-z][a-z0-9-]{1,30}$', v):
            raise ValueError(
                "default_region must be a valid cloud region identifier (e.g. us-east-1)"
            )
        return v


class CloudAccountResponse(BaseModel):
    id: UUID
    workspace_id: UUID
    provider: str
    name: str
    account_id: Optional[str] = None
    default_region: Optional[str] = None
    # role_arn and external_id intentionally NOT exposed in API responses (sensitive)
    description: Optional[str] = None
    status: str
    is_active: bool
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class CloudAccountListResponse(BaseModel):
    items: List[CloudAccountResponse]
    total: int
    page: int
    limit: int
