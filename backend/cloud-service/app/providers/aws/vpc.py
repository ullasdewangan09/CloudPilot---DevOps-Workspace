"""
AWS VPC & Network Topology Discovery Module — Phase 3B.3 Architecture.

Discovers Virtual Private Clouds (VPCs), subnets, route tables, internet gateways,
and security groups across AWS regions using paginated API calls.

Responsibilities:
- Reuses AWSSessionManager for ephemeral cross-account AssumeRole STS access.
- Uses paginators (describe_vpcs, describe_subnets, describe_route_tables, describe_internet_gateways, describe_security_groups).
- Normalizes network relationships (correlating subnets, route tables, and gateways to their parent VPC).
- Produces normalized resources for VPCs, Subnets, and Security Groups aligned with ResourceType enum.
"""

from typing import Any, Dict, List, Optional
from collections import defaultdict
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


class VPCDiscovery:
    """
    AWS VPC and virtual network discovery service.
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

    def _paginate(self, operation: str, result_key: str) -> List[Dict[str, Any]]:
        """Helper to paginate or directly invoke EC2 network APIs safely."""
        items = []
        try:
            if hasattr(self.client, "get_paginator") and self.client.can_paginate(operation):
                paginator = self.client.get_paginator(operation)
                for page in paginator.paginate():
                    items.extend(page.get(result_key, []))
            else:
                method = getattr(self.client, operation)
                resp = method()
                items.extend(resp.get(result_key, []))
            return items
        except ClientError as exc:
            handle_aws_client_error(exc, "ec2", operation, self.region)
        except (EndpointConnectionError, ConnectTimeoutError, ReadTimeoutError) as exc:
            raise AWSConnectionError(
                f"Network error connecting to AWS EC2 network service ({operation}) in {self.region}."
            ) from exc
        except BotoCoreError as exc:
            raise AWSDiscoveryError(
                f"BotoCore error in {operation} ({self.region}): {str(exc)}",
                service="ec2",
                region=self.region
            ) from exc

    def list_vpcs(self) -> List[Dict[str, Any]]:
        """Fetch raw VPC descriptions."""
        return self._paginate("describe_vpcs", "Vpcs")

    def list_subnets(self) -> List[Dict[str, Any]]:
        """Fetch raw Subnet descriptions."""
        return self._paginate("describe_subnets", "Subnets")

    def list_route_tables(self) -> List[Dict[str, Any]]:
        """Fetch raw Route Table descriptions."""
        return self._paginate("describe_route_tables", "RouteTables")

    def list_internet_gateways(self) -> List[Dict[str, Any]]:
        """Fetch raw Internet Gateway descriptions."""
        return self._paginate("describe_internet_gateways", "InternetGateways")

    def list_security_groups(self) -> List[Dict[str, Any]]:
        """Fetch raw Security Group descriptions."""
        return self._paginate("describe_security_groups", "SecurityGroups")

    def discover(self, include_sub_resources: bool = True) -> List[Dict[str, Any]]:
        """
        Discovers all VPCs and correlates related network components.
        If include_sub_resources=True, also returns normalized resources for subnets and security groups.
        """
        raw_vpcs = self.list_vpcs()
        raw_subnets = self.list_subnets()
        raw_route_tables = self.list_route_tables()
        raw_igws = self.list_internet_gateways()
        raw_sgs = self.list_security_groups()

        # Build correlation indices by vpc_id
        vpc_subnets = defaultdict(list)
        for s in raw_subnets:
            vid = s.get("VpcId")
            if vid:
                vpc_subnets[vid].append(s.get("SubnetId"))

        vpc_routes = defaultdict(list)
        for rt in raw_route_tables:
            vid = rt.get("VpcId")
            if vid:
                vpc_routes[vid].append(rt.get("RouteTableId"))

        vpc_igws = defaultdict(list)
        for igw in raw_igws:
            for attachment in igw.get("Attachments", []):
                vid = attachment.get("VpcId")
                if vid:
                    vpc_igws[vid].append(igw.get("InternetGatewayId"))

        vpc_sgs = defaultdict(list)
        for sg in raw_sgs:
            vid = sg.get("VpcId")
            if vid:
                vpc_sgs[vid].append(sg.get("GroupId"))

        normalized_resources = []

        # 1. Normalize VPCs with correlated references
        for vpc in raw_vpcs:
            vid = vpc.get("VpcId")
            normalized_vpc = ResourceNormalizer.normalize_vpc(
                raw=vpc,
                region=self.region,
                subnets=vpc_subnets.get(vid, []),
                route_tables=vpc_routes.get(vid, []),
                internet_gateways=vpc_igws.get(vid, []),
                security_groups=vpc_sgs.get(vid, []),
            )
            normalized_resources.append(normalized_vpc)

        # 2. Normalize child subnets & security groups if requested
        if include_sub_resources:
            for s in raw_subnets:
                normalized_resources.append(
                    ResourceNormalizer.normalize_subnet(s, self.region)
                )
            for sg in raw_sgs:
                normalized_resources.append(
                    ResourceNormalizer.normalize_security_group(sg, self.region)
                )

        return normalized_resources

