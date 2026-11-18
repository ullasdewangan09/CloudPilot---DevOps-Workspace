"""
AWS EC2 Resource Discovery Module — Phase 3B.3 Architecture.

Discovers EC2 virtual machine instances across AWS regions using paginated API calls
and transforms them into normalized CloudResource dictionaries.

Responsibilities:
- Reuses AWSSessionManager for ephemeral cross-account AssumeRole STS access.
- Utilizes boto3 describe_instances paginator for reliable multi-page fetching.
- Safely handles empty reservations, terminating instances, and missing optional tags.
- Categorizes AWS errors (AccessDenied, Throttling, ExpiredToken, Network errors).
- Zero secret leakage.
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


class EC2Discovery:
    """
    EC2 instance discovery service.
    """

    def __init__(self, session_manager: AWSSessionManager, region: Optional[str] = None):
        self.session_manager = session_manager
        self.region = (region or session_manager.region or "us-east-1").strip()
        self._client = None

    @property
    def client(self):
        """Lazy client instantiation with automatic AssumeRole token refresh."""
        if self._client is None or self.session_manager.is_expired():
            self._client = self.session_manager.get_client("ec2", region_name=self.region)
        return self._client

    def list_instances(self) -> List[Dict[str, Any]]:
        """
        Fetch all raw EC2 instance dictionaries in this region using the describe_instances paginator.
        """
        instances = []
        try:
            # Check if paginator is available on client
            if hasattr(self.client, "get_paginator") and self.client.can_paginate("describe_instances"):
                paginator = self.client.get_paginator("describe_instances")
                for page in paginator.paginate():
                    for reservation in page.get("Reservations", []):
                        for inst in reservation.get("Instances", []):
                            instances.append(inst)
            else:
                # Direct call fallback for non-paginated mock setups
                response = self.client.describe_instances()
                for reservation in response.get("Reservations", []):
                    for inst in reservation.get("Instances", []):
                        instances.append(inst)

            return instances

        except ClientError as exc:
            handle_aws_client_error(exc, "ec2", "describe_instances", self.region)
        except (EndpointConnectionError, ConnectTimeoutError, ReadTimeoutError) as exc:
            raise AWSConnectionError(
                f"Network error connecting to AWS EC2 endpoint in {self.region}."
            ) from exc
        except BotoCoreError as exc:
            raise AWSDiscoveryError(
                f"BotoCore error during EC2 discovery in {self.region}: {str(exc)}",
                service="ec2",
                region=self.region
            ) from exc

    def discover(self) -> List[Dict[str, Any]]:
        """
        Discover and normalize all EC2 instances in this region.
        Returns a list of normalized resource dictionaries.
        """
        raw_instances = self.list_instances()
        normalized_list = []
        for inst in raw_instances:
            normalized = ResourceNormalizer.normalize_ec2_instance(inst, self.region)
            normalized_list.append(normalized)
        return normalized_list

