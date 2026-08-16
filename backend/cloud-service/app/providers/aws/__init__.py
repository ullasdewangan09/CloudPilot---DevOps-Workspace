"""
AWS Provider Stub — Phase 3A Architecture Placeholder.

Real AWS API calls (boto3/STS/EC2/S3/VPC) are NOT implemented here.
Those belong to Phase 3B (AWS Resource Discovery).

This file establishes the provider structure for seamless Phase 3B integration.
"""
from typing import Any, Dict
from app.providers.base import BaseCloudProvider


class AWSProvider(BaseCloudProvider):
    """
    AWS Cloud Provider implementation stub.

    Phase 3B will add:
    - STS AssumeRole via boto3
    - EC2 instance discovery
    - S3 bucket discovery
    - VPC resource discovery
    - IAM validation
    """

    def __init__(self, role_arn: str = None, external_id: str = None, region: str = "us-east-1"):
        self.role_arn = role_arn
        self.external_id = external_id
        self.region = region

    def validate_credentials(self, **kwargs) -> bool:
        """
        Phase 3B: Will call STS GetCallerIdentity to validate credentials.
        Phase 3A: Always returns False (not yet implemented).
        """
        return False

    def get_account_info(self, **kwargs) -> Dict[str, Any]:
        """
        Phase 3B: Will call STS GetCallerIdentity for account metadata.
        Phase 3A: Returns empty dict (not yet implemented).
        """
        return {}
