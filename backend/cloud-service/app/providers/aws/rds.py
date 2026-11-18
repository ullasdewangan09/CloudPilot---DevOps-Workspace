"""
AWS RDS Database Resource Discovery Module — Phase 3B.3 Architecture.

Discovers Relational Database Service (RDS) instances across AWS regions using
paginated API calls and transforms them into normalized CloudResource dictionaries.

Responsibilities:
- Reuses AWSSessionManager for ephemeral cross-account AssumeRole STS access.
- Uses describe_db_instances paginator for reliable multi-page enumeration.
- Strictly excludes database passwords, master credentials, or secret storage tokens.
- Categorizes AWS errors (AccessDenied, Throttling, ExpiredToken, Network errors).
- Produces normalized resources aligned with ResourceType.RDS_INSTANCE.
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
from app.providers.aws.normalizer import ResourceNormalizer


class RDSDiscovery:
    """
    AWS RDS database instance discovery service.
    """

    def __init__(self, session_manager: AWSSessionManager, region: Optional[str] = None):
        self.session_manager = session_manager
        self.region = (region or session_manager.region or "us-east-1").strip()
        self._client = None

    @property
    def client(self):
        """Lazy client instantiation with automatic AssumeRole token refresh."""
        if self._client is None or self.session_manager.is_expired():
            self._client = self.session_manager.get_client("rds", region_name=self.region)
        return self._client

    def list_db_instances(self) -> List[Dict[str, Any]]:
        """
        Fetch all raw DB instance dictionaries in this region using describe_db_instances paginator.
        """
        instances = []
        try:
            if hasattr(self.client, "get_paginator") and self.client.can_paginate("describe_db_instances"):
                paginator = self.client.get_paginator("describe_db_instances")
                for page in paginator.paginate():
                    instances.extend(page.get("DBInstances", []))
            else:
                resp = self.client.describe_db_instances()
                instances.extend(resp.get("DBInstances", []))

            return instances

        except ClientError as exc:
            handle_aws_client_error(exc, "rds", "describe_db_instances", self.region)
        except (EndpointConnectionError, ConnectTimeoutError, ReadTimeoutError) as exc:
            raise AWSConnectionError(
                f"Network error connecting to AWS RDS endpoint in {self.region}."
            ) from exc
        except BotoCoreError as exc:
            raise AWSDiscoveryError(
                f"BotoCore error during RDS discovery in {self.region}: {str(exc)}",
                service="rds",
                region=self.region
            ) from exc

    def discover(self) -> List[Dict[str, Any]]:
        """
        Discover and normalize all RDS instances in this region.
        Returns a list of normalized resource dictionaries.
        """
        raw_instances = self.list_db_instances()
        normalized_list = []
        for inst in raw_instances:
            normalized = ResourceNormalizer.normalize_rds_instance(inst, self.region)
            normalized_list.append(normalized)
        return normalized_list

