"""
AWS Provider & Discovery Modules — Phase 3B.3 Architecture.

Supports:
- Provider initialization & credential sanitization.
- IAM Role ARN format validation (local syntax check).
- Ephemeral AWS STS AssumeRole session generation via AWSSessionManager.
- Live connection verification (caller identity and account match).
- Resource discovery for EC2, S3, VPC, and RDS.
- Resource normalization engine.
- Zero credential logging, zero temporary token persistence.
- Safe domain error handling without credential leakage.
"""
import re
from typing import Any, Dict, Optional
from app.providers.base import BaseCloudProvider
from app.providers.aws.session import (
    AWSSessionManager,
    AWSSessionError,
    AWSAuthError,
    AWSValidationError,
    AWSAccountMismatchError,
    AWSConnectionError,
    AWSThrottlingError,
    AWSDiscoveryError,
    handle_aws_client_error,
    AWS_ROLE_ARN_REGEX,
)
from app.providers.aws.normalizer import ResourceNormalizer, json_sanitize, parse_aws_tags
from app.providers.aws.ec2 import EC2Discovery
from app.providers.aws.s3 import S3Discovery
from app.providers.aws.vpc import VPCDiscovery
from app.providers.aws.rds import RDSDiscovery


class AWSProvider(BaseCloudProvider):
    """
    AWS Cloud Provider implementation.
    Validates IAM Role ARNs and handles STS AssumeRole credentials safely.
    """

    def __init__(
        self,
        role_arn: Optional[str] = None,
        external_id: Optional[str] = None,
        account_id: Optional[str] = None,
        region: str = "us-east-1"
    ):
        self.role_arn = role_arn.strip() if role_arn else None
        self.external_id = external_id.strip() if external_id else None
        self.account_id = account_id.strip() if account_id else None
        self.region = region.strip() if region else "us-east-1"

    def validate_role_arn_format(self) -> bool:
        """
        Verify that the IAM Role ARN conforms to AWS ARN standards (local syntax check).
        Does NOT verify live AWS connectivity or credential validity.
        """
        if not self.role_arn:
            return False
        return AWSSessionManager.validate_role_arn_format(self.role_arn)

    def get_session_manager(self, duration_seconds: int = 1800) -> AWSSessionManager:
        """
        Instantiate and return an AWSSessionManager configured with provider credentials.
        """
        return AWSSessionManager(
            role_arn=self.role_arn,
            external_id=self.external_id,
            account_id=self.account_id,
            region=self.region,
            duration_seconds=duration_seconds,
        )

    def verify_connection(self) -> Dict[str, Any]:
        """
        Execute actual AWS connection verification via STS AssumeRole and GetCallerIdentity.
        Returns sanitized identity metadata on success.
        Raises AWSSessionError or subclasses on authentication, permission, or connection failure.
        """
        session_manager = self.get_session_manager()
        return session_manager.assume_role()

    def validate_credentials(self, **kwargs) -> bool:
        """
        Validate AWS credentials against AWS STS.
        Performs local ARN syntax check first, then calls STS AssumeRole to verify live access.
        Returns True ONLY if AWS STS validates credentials and caller identity matches.
        Returns False on any failure (no false-positive fallback).
        """
        if not self.validate_role_arn_format():
            return False
        if not self.external_id:
            return False

        try:
            self.verify_connection()
            return True
        except (AWSSessionError, Exception):
            return False

    def get_account_info(self, **kwargs) -> Dict[str, Any]:
        """Retrieve account metadata safely without exposing credentials."""
        account_id = self.account_id
        if not account_id and self.role_arn and ":iam::" in self.role_arn:
            try:
                account_id = self.role_arn.split(":iam::")[1].split(":role/")[0]
            except Exception:
                account_id = None

        return {
            "provider": "aws",
            "region": self.region,
            "account_id": account_id,
            "role_configured": bool(self.role_arn),
            "external_id_configured": bool(self.external_id),
        }


__all__ = [
    "AWSProvider",
    "AWSSessionManager",
    "AWSSessionError",
    "AWSAuthError",
    "AWSValidationError",
    "AWSAccountMismatchError",
    "AWSConnectionError",
    "AWSThrottlingError",
    "AWSDiscoveryError",
    "handle_aws_client_error",
    "ResourceNormalizer",
    "json_sanitize",
    "parse_aws_tags",
    "EC2Discovery",
    "S3Discovery",
    "VPCDiscovery",
    "RDSDiscovery",
]
