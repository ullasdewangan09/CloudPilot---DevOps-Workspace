"""
AWS S3 Bucket Resource Discovery Module — Phase 3B.3 Architecture.

Discovers S3 object storage buckets across an AWS account, resolves their canonical
regions, and queries metadata (versioning, encryption, tags) with graceful sub-permission degradation.

Responsibilities:
- Reuses AWSSessionManager for ephemeral cross-account AssumeRole STS access.
- Calls global s3.list_buckets() once per account.
- Resolves each bucket's canonical region via s3.get_bucket_location(), normalizing None/empty to 'us-east-1'.
- Safely handles optional sub-resource permissions (tags, encryption, versioning) without failing bucket discovery.
- Zero data/object access: strictly inspects bucket metadata, never calls GetObject or lists objects.
"""

from typing import Any, Dict, List, Optional
from botocore.exceptions import (
    ClientError,
    BotoCoreError,
    EndpointConnectionError,
    ConnectTimeoutError,
    ReadTimeoutError,
)

from app.providers.aws.session import (
    AWSSessionManager,
    AWSConnectionError,
    AWSDiscoveryError,
    handle_aws_client_error,
)
from app.providers.aws.normalizer import ResourceNormalizer, parse_aws_tags


class S3Discovery:
    """
    S3 bucket discovery service.
    """

    def __init__(self, session_manager: AWSSessionManager):
        self.session_manager = session_manager
        self._client = None

    @property
    def client(self):
        """Lazy client instantiation with automatic AssumeRole token refresh."""
        if self._client is None or self.session_manager.is_expired():
            self._client = self.session_manager.get_client("s3")
        return self._client

    def list_raw_buckets(self) -> List[Dict[str, Any]]:
        """
        Execute global bucket enumeration via list_buckets.
        Returns raw list of bucket dictionaries [{'Name': '...', 'CreationDate': ...}].
        """
        try:
            response = self.client.list_buckets()
            return response.get("Buckets", [])
        except ClientError as exc:
            handle_aws_client_error(exc, "s3", "list_buckets", region="global")
        except (EndpointConnectionError, ConnectTimeoutError, ReadTimeoutError) as exc:
            raise AWSConnectionError("Network error connecting to AWS S3 endpoint.") from exc
        except BotoCoreError as exc:
            raise AWSDiscoveryError(f"BotoCore error during S3 discovery: {str(exc)}", service="s3") from exc

    def get_bucket_region(self, bucket_name: str) -> str:
        """
        Resolve the canonical AWS region for a bucket via get_bucket_location.
        Normalizes AWS S3 quirks:
        - None or empty string -> 'us-east-1' (standard AWS default)
        - 'EU' -> 'eu-west-1' (legacy AWS constraint)
        """
        try:
            resp = self.client.get_bucket_location(Bucket=bucket_name)
            loc = resp.get("LocationConstraint")
            if not loc:
                return "us-east-1"
            if loc == "EU":
                return "eu-west-1"
            return str(loc)
        except ClientError as exc:
            error_code = exc.response.get("Error", {}).get("Code", "Unknown")
            # If AccessDenied on location or NoSuchBucket, fallback safely to default region
            if error_code in ("AccessDenied", "NoSuchBucket", "AllAccessDisabled"):
                return self.session_manager.region or "us-east-1"
            handle_aws_client_error(exc, "s3", f"get_bucket_location({bucket_name})")
        except (EndpointConnectionError, ConnectTimeoutError, ReadTimeoutError) as exc:
            raise AWSConnectionError(f"Network error resolving region for S3 bucket '{bucket_name}'.") from exc

    def get_bucket_versioning(self, bucket_name: str) -> Optional[str]:
        """
        Query bucket versioning status.
        Returns 'Enabled', 'Suspended', or 'Disabled'.
        Gracefully handles permission denials.
        """
        try:
            resp = self.client.get_bucket_versioning(Bucket=bucket_name)
            # Status can be 'Enabled', 'Suspended', or absent (Disabled/None)
            return resp.get("Status", "Disabled")
        except ClientError:
            return "Unknown"

    def get_bucket_encryption(self, bucket_name: str) -> Dict[str, Any]:
        """
        Query server-side encryption configuration.
        Gracefully handles ServerSideEncryptionConfigurationNotFoundError and AccessDenied.
        """
        try:
            resp = self.client.get_bucket_encryption(Bucket=bucket_name)
            rules = resp.get("ServerSideEncryptionConfiguration", {}).get("Rules", [])
            if rules:
                apply_rule = rules[0].get("ApplyServerSideEncryptionByDefault", {})
                return {
                    "status": "Enabled",
                    "sse_algorithm": apply_rule.get("SSEAlgorithm"),
                    "kms_master_key_id": apply_rule.get("KMSMasterKeyId"),
                }
            return {"status": "Enabled"}
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code")
            if code == "ServerSideEncryptionConfigurationNotFoundError":
                return {"status": "Disabled"}
            elif code in ("AccessDenied", "MethodNotAllowed"):
                return {"status": "Unknown", "error": "AccessDenied"}
            return {"status": "Unknown"}

    def get_bucket_tags(self, bucket_name: str) -> Dict[str, str]:
        """
        Query bucket tags.
        Gracefully handles NoSuchTagSet and AccessDenied without failing.
        """
        try:
            resp = self.client.get_bucket_tagging(Bucket=bucket_name)
            tag_set = resp.get("TagSet", [])
            return parse_aws_tags(tag_set)
        except ClientError as exc:
            # Common expected cases: bucket has no tags (NoSuchTagSet) or no tag read permission
            return {}

    def discover(self) -> List[Dict[str, Any]]:
        """
        Discovers all buckets across the account, resolves their regional endpoints,
        enriches with versioning, encryption, and tags, and returns normalized S3 resources.
        """
        raw_buckets = self.list_raw_buckets()
        normalized_list = []

        for b in raw_buckets:
            name = b.get("Name")
            if not name:
                continue

            creation_date = b.get("CreationDate")
            region = self.get_bucket_region(name)
            versioning = self.get_bucket_versioning(name)
            encryption = self.get_bucket_encryption(name)
            tags = self.get_bucket_tags(name)

            permissions_status = {
                "tags": bool(tags),
                "encryption": encryption.get("status") != "Unknown",
                "versioning": versioning != "Unknown",
            }

            normalized = ResourceNormalizer.normalize_s3_bucket(
                bucket_name=name,
                region=region,
                creation_date=creation_date,
                versioning_status=versioning,
                encryption=encryption,
                tags=tags,
                permissions_status=permissions_status,
            )
            normalized_list.append(normalized)

        return normalized_list

