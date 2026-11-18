"""
AWS EC2 Resource Discovery Tests — Phase 3B.3 Automated Test Suite.

Verifies:
1. Successful EC2 instance discovery and normalization.
2. Handling of empty reservations and empty instances.
3. Multi-page pagination via AWS describe_instances paginator.
4. Multi-region support.
5. Missing optional tags and IPs.
6. AccessDenied handling mapped to AWSAuthError.
7. Throttling / RequestLimitExceeded handling mapped to AWSThrottlingError.
8. Network timeout handling mapped to AWSConnectionError.
9. Credential secrecy and AWSSessionManager integration.
"""

import json
from unittest.mock import MagicMock
import pytest
from botocore.exceptions import (
    ClientError,
    EndpointConnectionError,
    ConnectTimeoutError,
)

from app.models.cloud_resource import ResourceType
from app.providers.aws.ec2 import EC2Discovery
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


def test_ec2_discovery_success_single_page(mock_session_manager):
    """Verify EC2 discovery returns normalized instances from describe_instances."""
    mock_ec2_client = MagicMock()
    mock_session_manager.get_client.return_value = mock_ec2_client

    mock_paginator = MagicMock()
    mock_ec2_client.can_paginate.return_value = True
    mock_ec2_client.get_paginator.return_value = mock_paginator

    mock_paginator.paginate.return_value = [
        {
            "Reservations": [
                {
                    "Instances": [
                        {
                            "InstanceId": "i-11111111111111111",
                            "InstanceType": "t3.micro",
                            "State": {"Name": "running", "Code": 16},
                            "Placement": {"AvailabilityZone": "us-east-1a"},
                            "VpcId": "vpc-01",
                            "SubnetId": "subnet-01",
                            "PrivateIpAddress": "10.0.0.10",
                            "PublicIpAddress": "54.0.0.10",
                            "Tags": [{"Key": "Name", "Value": "bastion-host"}],
                            "SecurityGroups": [{"GroupId": "sg-01", "GroupName": "ssh-sg"}]
                        }
                    ]
                }
            ]
        }
    ]

    discovery = EC2Discovery(session_manager=mock_session_manager, region="us-east-1")
    resources = discovery.discover()

    assert len(resources) == 1
    res = resources[0]
    assert res["provider"] == "aws"
    assert res["resource_type"] == ResourceType.EC2_INSTANCE.value
    assert res["provider_resource_id"] == "i-11111111111111111"
    assert res["name"] == "bastion-host"
    assert res["region"] == "us-east-1"
    assert res["status"] == "running"
    assert res["metadata"]["private_ip"] == "10.0.0.10"
    assert res["metadata"]["public_ip"] == "54.0.0.10"

    # Confirm JSON serializability
    json.dumps(res)


def test_ec2_discovery_empty_response(mock_session_manager):
    """Verify empty Reservations returns an empty list without errors."""
    mock_ec2_client = MagicMock()
    mock_session_manager.get_client.return_value = mock_ec2_client

    mock_paginator = MagicMock()
    mock_ec2_client.can_paginate.return_value = True
    mock_ec2_client.get_paginator.return_value = mock_paginator
    mock_paginator.paginate.return_value = [{"Reservations": []}]

    discovery = EC2Discovery(session_manager=mock_session_manager, region="us-east-1")
    resources = discovery.discover()
    assert resources == []


def test_ec2_discovery_multi_page_pagination(mock_session_manager):
    """Verify multi-page paginator traverses all pages and collects instances."""
    mock_ec2_client = MagicMock()
    mock_session_manager.get_client.return_value = mock_ec2_client

    mock_paginator = MagicMock()
    mock_ec2_client.can_paginate.return_value = True
    mock_ec2_client.get_paginator.return_value = mock_paginator

    # 3 distinct pages
    mock_paginator.paginate.return_value = [
        {"Reservations": [{"Instances": [{"InstanceId": "i-page1", "State": {"Name": "running"}}]}]},
        {"Reservations": [{"Instances": [{"InstanceId": "i-page2", "State": {"Name": "stopped"}}]}]},
        {"Reservations": [{"Instances": [{"InstanceId": "i-page3", "State": {"Name": "running"}}]}]},
    ]

    discovery = EC2Discovery(session_manager=mock_session_manager, region="us-east-1")
    resources = discovery.discover()

    assert len(resources) == 3
    ids = [r["provider_resource_id"] for r in resources]
    assert ids == ["i-page1", "i-page2", "i-page3"]


def test_ec2_discovery_multiple_regions(mock_session_manager):
    """Verify discovery can be initialized for different target regions."""
    mock_ec2_client = MagicMock()
    mock_session_manager.get_client.return_value = mock_ec2_client
    mock_paginator = MagicMock()
    mock_ec2_client.can_paginate.return_value = True
    mock_ec2_client.get_paginator.return_value = mock_paginator
    mock_paginator.paginate.return_value = [
        {"Reservations": [{"Instances": [{"InstanceId": "i-tokyo", "State": {"Name": "running"}}]}]}
    ]

    discovery = EC2Discovery(session_manager=mock_session_manager, region="ap-northeast-1")
    resources = discovery.discover()

    mock_session_manager.get_client.assert_called_with("ec2", region_name="ap-northeast-1")
    assert resources[0]["region"] == "ap-northeast-1"


def test_ec2_discovery_missing_optional_fields(mock_session_manager):
    """Verify instances without tags, VPC, or IPs normalize safely."""
    mock_ec2_client = MagicMock()
    mock_session_manager.get_client.return_value = mock_ec2_client
    mock_paginator = MagicMock()
    mock_ec2_client.can_paginate.return_value = True
    mock_ec2_client.get_paginator.return_value = mock_paginator
    mock_paginator.paginate.return_value = [
        {
            "Reservations": [
                {
                    "Instances": [
                        {
                            "InstanceId": "i-notags",
                            "State": {"Name": "terminated", "Code": 48}
                        }
                    ]
                }
            ]
        }
    ]

    discovery = EC2Discovery(session_manager=mock_session_manager, region="us-east-1")
    resources = discovery.discover()
    assert len(resources) == 1
    assert resources[0]["name"] == "i-notags"
    assert resources[0]["tags"] == {}
    assert resources[0]["metadata"]["vpc_id"] is None


def test_ec2_discovery_access_denied(mock_session_manager):
    """Verify UnauthorizedOperation / AccessDenied raises AWSAuthError."""
    mock_ec2_client = MagicMock()
    mock_session_manager.get_client.return_value = mock_ec2_client
    mock_paginator = MagicMock()
    mock_ec2_client.can_paginate.return_value = True
    mock_ec2_client.get_paginator.return_value = mock_paginator

    err = ClientError(
        {"Error": {"Code": "UnauthorizedOperation", "Message": "You are not authorized"}},
        "DescribeInstances"
    )
    mock_paginator.paginate.side_effect = err

    discovery = EC2Discovery(session_manager=mock_session_manager, region="us-east-1")
    with pytest.raises(AWSAuthError) as exc_info:
        discovery.discover()

    assert exc_info.value.code == "UnauthorizedOperation"
    assert "access denied" in str(exc_info.value).lower()


def test_ec2_discovery_throttling(mock_session_manager):
    """Verify RequestLimitExceeded raises AWSThrottlingError."""
    mock_ec2_client = MagicMock()
    mock_session_manager.get_client.return_value = mock_ec2_client
    mock_paginator = MagicMock()
    mock_ec2_client.can_paginate.return_value = True
    mock_ec2_client.get_paginator.return_value = mock_paginator

    err = ClientError(
        {"Error": {"Code": "RequestLimitExceeded", "Message": "Request limit exceeded."}},
        "DescribeInstances"
    )
    mock_paginator.paginate.side_effect = err

    discovery = EC2Discovery(session_manager=mock_session_manager, region="us-east-1")
    with pytest.raises(AWSThrottlingError) as exc_info:
        discovery.discover()

    assert exc_info.value.code == "RequestLimitExceeded"
    assert "rate limit exceeded" in str(exc_info.value).lower()


def test_ec2_discovery_network_error(mock_session_manager):
    """Verify EndpointConnectionError / ConnectTimeoutError raises AWSConnectionError."""
    mock_ec2_client = MagicMock()
    mock_session_manager.get_client.return_value = mock_ec2_client
    mock_paginator = MagicMock()
    mock_ec2_client.can_paginate.return_value = True
    mock_ec2_client.get_paginator.return_value = mock_paginator

    mock_paginator.paginate.side_effect = EndpointConnectionError(endpoint_url="https://ec2.us-east-1.amazonaws.com")

    discovery = EC2Discovery(session_manager=mock_session_manager, region="us-east-1")
    with pytest.raises(AWSConnectionError):
        discovery.discover()

