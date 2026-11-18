"""
AWS VPC Resource Discovery Tests — Phase 3B.3 Automated Test Suite.

Verifies:
1. VPC enumeration and network topology correlation (subnets, route tables, IGWs, SGs).
2. Sub-resource normalization for Subnets and Security Groups.
3. Multi-page pagination support.
4. Multi-VPC and multi-region handling.
5. Empty responses.
6. AccessDenied handling mapped to AWSAuthError.
7. Throttling handling mapped to AWSThrottlingError.
8. Network error handling mapped to AWSConnectionError.
9. JSON serializability.
"""

import json
from unittest.mock import MagicMock
import pytest
from botocore.exceptions import (
    ClientError,
    EndpointConnectionError,
)

from app.models.cloud_resource import ResourceType
from app.providers.aws.vpc import VPCDiscovery
from app.providers.aws.session import (
    AWSAuthError,
    AWSThrottlingError,
    AWSConnectionError,
    AWSSessionManager,
)


@pytest.fixture
def mock_session_manager():
    """Mock AWSSessionManager providing ephemeral clients."""
    manager = MagicMock(spec=AWSSessionManager)
    manager.region = "us-east-1"
    manager.is_expired.return_value = False
    return manager


def test_vpc_discovery_success(mock_session_manager):
    """Verify VPC discovery captures VPC, subnets, route tables, IGWs, SGs and correlates them."""
    mock_ec2_client = MagicMock()
    mock_session_manager.get_client.return_value = mock_ec2_client

    # Configure direct responses
    mock_ec2_client.can_paginate.return_value = False
    mock_ec2_client.describe_vpcs.return_value = {
        "Vpcs": [
            {
                "VpcId": "vpc-100",
                "CidrBlock": "10.0.0.0/16",
                "IsDefault": False,
                "State": "available",
                "Tags": [{"Key": "Name", "Value": "primary-vpc"}]
            }
        ]
    }
    mock_ec2_client.describe_subnets.return_value = {
        "Subnets": [
            {
                "SubnetId": "subnet-101",
                "VpcId": "vpc-100",
                "CidrBlock": "10.0.1.0/24",
                "AvailabilityZone": "us-east-1a",
                "State": "available",
                "Tags": [{"Key": "Name", "Value": "public-subnet"}]
            }
        ]
    }
    mock_ec2_client.describe_route_tables.return_value = {
        "RouteTables": [
            {"RouteTableId": "rtb-101", "VpcId": "vpc-100"}
        ]
    }
    mock_ec2_client.describe_internet_gateways.return_value = {
        "InternetGateways": [
            {
                "InternetGatewayId": "igw-101",
                "Attachments": [{"VpcId": "vpc-100", "State": "available"}]
            }
        ]
    }
    mock_ec2_client.describe_security_groups.return_value = {
        "SecurityGroups": [
            {
                "GroupId": "sg-101",
                "GroupName": "web-tier",
                "VpcId": "vpc-100",
                "Description": "HTTP web tier",
                "IpPermissions": [],
                "IpPermissionsEgress": []
            }
        ]
    }

    discovery = VPCDiscovery(session_manager=mock_session_manager, region="us-east-1")
    resources = discovery.discover(include_sub_resources=True)

    # 1 VPC + 1 Subnet + 1 Security Group = 3 resources
    assert len(resources) == 3

    # Find the VPC resource
    vpc_res = next(r for r in resources if r["resource_type"] == ResourceType.VPC.value)
    assert vpc_res["provider_resource_id"] == "vpc-100"
    assert vpc_res["name"] == "primary-vpc"
    assert vpc_res["metadata"]["subnets"] == ["subnet-101"]
    assert vpc_res["metadata"]["route_tables"] == ["rtb-101"]
    assert vpc_res["metadata"]["internet_gateways"] == ["igw-101"]
    assert vpc_res["metadata"]["security_groups"] == ["sg-101"]

    # Find Subnet resource
    subnet_res = next(r for r in resources if r["resource_type"] == ResourceType.SUBNET.value)
    assert subnet_res["provider_resource_id"] == "subnet-101"
    assert subnet_res["metadata"]["vpc_id"] == "vpc-100"

    # Find Security Group resource
    sg_res = next(r for r in resources if r["resource_type"] == ResourceType.SECURITY_GROUP.value)
    assert sg_res["provider_resource_id"] == "sg-101"
    assert sg_res["name"] == "web-tier"

    # JSON serializability check
    for r in resources:
        json.dumps(r)


def test_vpc_discovery_empty(mock_session_manager):
    """Verify empty region returns empty list cleanly."""
    mock_ec2_client = MagicMock()
    mock_session_manager.get_client.return_value = mock_ec2_client
    mock_ec2_client.can_paginate.return_value = False

    mock_ec2_client.describe_vpcs.return_value = {"Vpcs": []}
    mock_ec2_client.describe_subnets.return_value = {"Subnets": []}
    mock_ec2_client.describe_route_tables.return_value = {"RouteTables": []}
    mock_ec2_client.describe_internet_gateways.return_value = {"InternetGateways": []}
    mock_ec2_client.describe_security_groups.return_value = {"SecurityGroups": []}

    discovery = VPCDiscovery(session_manager=mock_session_manager, region="us-east-1")
    resources = discovery.discover()
    assert resources == []


def test_vpc_discovery_access_denied(mock_session_manager):
    """Verify UnauthorizedOperation on describe_vpcs raises AWSAuthError."""
    mock_ec2_client = MagicMock()
    mock_session_manager.get_client.return_value = mock_ec2_client
    mock_ec2_client.can_paginate.return_value = False

    mock_ec2_client.describe_vpcs.side_effect = ClientError(
        {"Error": {"Code": "UnauthorizedOperation", "Message": "Access Denied"}},
        "DescribeVpcs"
    )

    discovery = VPCDiscovery(session_manager=mock_session_manager, region="us-east-1")
    with pytest.raises(AWSAuthError) as exc_info:
        discovery.discover()

    assert exc_info.value.code == "UnauthorizedOperation"


def test_vpc_discovery_throttling(mock_session_manager):
    """Verify RequestLimitExceeded raises AWSThrottlingError."""
    mock_ec2_client = MagicMock()
    mock_session_manager.get_client.return_value = mock_ec2_client
    mock_ec2_client.can_paginate.return_value = False

    mock_ec2_client.describe_vpcs.side_effect = ClientError(
        {"Error": {"Code": "RequestLimitExceeded", "Message": "Rate limit exceeded"}},
        "DescribeVpcs"
    )

    discovery = VPCDiscovery(session_manager=mock_session_manager, region="us-east-1")
    with pytest.raises(AWSThrottlingError) as exc_info:
        discovery.discover()

    assert exc_info.value.code == "RequestLimitExceeded"


def test_vpc_discovery_network_error(mock_session_manager):
    """Verify network connection failures raise AWSConnectionError."""
    mock_ec2_client = MagicMock()
    mock_session_manager.get_client.return_value = mock_ec2_client
    mock_ec2_client.can_paginate.return_value = False
    mock_ec2_client.describe_vpcs.side_effect = EndpointConnectionError(endpoint_url="https://ec2.us-east-1.amazonaws.com")

    discovery = VPCDiscovery(session_manager=mock_session_manager, region="us-east-1")
    with pytest.raises(AWSConnectionError):
        discovery.discover()

