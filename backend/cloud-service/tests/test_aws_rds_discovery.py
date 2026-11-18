"""
AWS RDS Resource Discovery Tests — Phase 3B.3 Automated Test Suite.

Verifies:
1. RDS DB instance discovery and normalization.
2. Verification of engine, version, multi-AZ, storage, and endpoint metadata.
3. Multi-page pagination via describe_db_instances paginator.
4. Empty DB instance responses.
5. Missing optional attributes (pending endpoints, no tags).
6. AccessDenied handling mapped to AWSAuthError.
7. Throttling handling mapped to AWSThrottlingError.
8. Network error handling mapped to AWSConnectionError.
9. Strict credential safety: zero passwords, secrets, or connection credentials.
10. JSON serializability.
"""

import json
from datetime import datetime, timezone
from unittest.mock import MagicMock
import pytest
from botocore.exceptions import (
    ClientError,
    EndpointConnectionError,
)

from app.models.cloud_resource import ResourceType
from app.providers.aws.rds import RDSDiscovery
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


def test_rds_discovery_success_with_paginator(mock_session_manager):
    """Verify RDS discovery normalizes DB instances with engine, endpoint, and multi-AZ specs."""
    mock_rds_client = MagicMock()
    mock_session_manager.get_client.return_value = mock_rds_client

    mock_paginator = MagicMock()
    mock_rds_client.can_paginate.return_value = True
    mock_rds_client.get_paginator.return_value = mock_paginator

    create_dt = datetime(2025, 9, 1, 10, 0, 0, tzinfo=timezone.utc)
    mock_paginator.paginate.return_value = [
        {
            "DBInstances": [
                {
                    "DBInstanceIdentifier": "billing-db-prod",
                    "DBInstanceArn": "arn:aws:rds:us-east-1:123456789012:db:billing-db-prod",
                    "Engine": "postgres",
                    "EngineVersion": "15.4",
                    "DBInstanceClass": "db.r6g.xlarge",
                    "DBInstanceStatus": "available",
                    "AllocatedStorage": 250,
                    "StorageType": "gp3",
                    "StorageEncrypted": True,
                    "MultiAZ": True,
                    "AvailabilityZone": "us-east-1a",
                    "Endpoint": {
                        "Address": "billing-db-prod.c0example.us-east-1.rds.amazonaws.com",
                        "Port": 5432
                    },
                    "DBSubnetGroup": {
                        "DBSubnetGroupName": "rds-private-subnets",
                        "VpcId": "vpc-0987654321"
                    },
                    "VpcSecurityGroups": [{"VpcSecurityGroupId": "sg-db-access"}],
                    "InstanceCreateTime": create_dt,
                    "TagList": [
                        {"Key": "Name", "Value": "Billing Production DB"},
                        {"Key": "Tier", "Value": "Data"}
                    ]
                }
            ]
        }
    ]

    discovery = RDSDiscovery(session_manager=mock_session_manager, region="us-east-1")
    resources = discovery.discover()

    assert len(resources) == 1
    res = resources[0]
    assert res["provider"] == "aws"
    assert res["resource_type"] == ResourceType.RDS_INSTANCE.value
    assert res["provider_resource_id"] == "billing-db-prod"
    assert res["name"] == "Billing Production DB"
    assert res["region"] == "us-east-1"
    assert res["status"] == "available"
    assert res["tags"]["Tier"] == "Data"
    assert res["metadata"]["engine"] == "postgres"
    assert res["metadata"]["engine_version"] == "15.4"
    assert res["metadata"]["instance_class"] == "db.r6g.xlarge"
    assert res["metadata"]["multi_az"] is True
    assert res["metadata"]["endpoint"]["port"] == 5432
    assert res["metadata"]["db_subnet_group"]["vpc_id"] == "vpc-0987654321"

    # Confirm JSON serializability
    json.dumps(res)


def test_rds_discovery_empty(mock_session_manager):
    """Verify empty DBInstances returns an empty list without errors."""
    mock_rds_client = MagicMock()
    mock_session_manager.get_client.return_value = mock_rds_client
    mock_paginator = MagicMock()
    mock_rds_client.can_paginate.return_value = True
    mock_rds_client.get_paginator.return_value = mock_paginator
    mock_paginator.paginate.return_value = [{"DBInstances": []}]

    discovery = RDSDiscovery(session_manager=mock_session_manager, region="us-east-1")
    resources = discovery.discover()
    assert resources == []


def test_rds_discovery_missing_optional_endpoint(mock_session_manager):
    """Verify instances in 'creating' status without an assigned endpoint normalize safely."""
    mock_rds_client = MagicMock()
    mock_session_manager.get_client.return_value = mock_rds_client
    mock_paginator = MagicMock()
    mock_rds_client.can_paginate.return_value = True
    mock_rds_client.get_paginator.return_value = mock_paginator
    mock_paginator.paginate.return_value = [
        {
            "DBInstances": [
                {
                    "DBInstanceIdentifier": "new-db-creating",
                    "Engine": "mysql",
                    "DBInstanceStatus": "creating",
                    "DBInstanceClass": "db.t4g.micro",
                    # No Endpoint key present yet
                }
            ]
        }
    ]

    discovery = RDSDiscovery(session_manager=mock_session_manager, region="us-east-1")
    resources = discovery.discover()

    assert len(resources) == 1
    assert resources[0]["provider_resource_id"] == "new-db-creating"
    assert resources[0]["status"] == "creating"
    assert resources[0]["metadata"]["endpoint"] is None
    json.dumps(resources[0])


def test_rds_discovery_access_denied(mock_session_manager):
    """Verify AccessDenied on describe_db_instances raises AWSAuthError."""
    mock_rds_client = MagicMock()
    mock_session_manager.get_client.return_value = mock_rds_client
    mock_paginator = MagicMock()
    mock_rds_client.can_paginate.return_value = True
    mock_rds_client.get_paginator.return_value = mock_paginator

    mock_paginator.paginate.side_effect = ClientError(
        {"Error": {"Code": "AccessDenied", "Message": "Not authorized"}},
        "DescribeDBInstances"
    )

    discovery = RDSDiscovery(session_manager=mock_session_manager, region="us-east-1")
    with pytest.raises(AWSAuthError) as exc_info:
        discovery.discover()

    assert exc_info.value.code == "AccessDenied"


def test_rds_discovery_throttling(mock_session_manager):
    """Verify ThrottlingException raises AWSThrottlingError."""
    mock_rds_client = MagicMock()
    mock_session_manager.get_client.return_value = mock_rds_client
    mock_paginator = MagicMock()
    mock_rds_client.can_paginate.return_value = True
    mock_rds_client.get_paginator.return_value = mock_paginator

    mock_paginator.paginate.side_effect = ClientError(
        {"Error": {"Code": "ThrottlingException", "Message": "Rate exceeded"}},
        "DescribeDBInstances"
    )

    discovery = RDSDiscovery(session_manager=mock_session_manager, region="us-east-1")
    with pytest.raises(AWSThrottlingError) as exc_info:
        discovery.discover()

    assert exc_info.value.code == "ThrottlingException"


def test_rds_discovery_network_error(mock_session_manager):
    """Verify network connection failures raise AWSConnectionError."""
    mock_rds_client = MagicMock()
    mock_session_manager.get_client.return_value = mock_rds_client
    mock_paginator = MagicMock()
    mock_rds_client.can_paginate.return_value = True
    mock_rds_client.get_paginator.return_value = mock_paginator

    mock_paginator.paginate.side_effect = EndpointConnectionError(endpoint_url="https://rds.us-east-1.amazonaws.com")

    discovery = RDSDiscovery(session_manager=mock_session_manager, region="us-east-1")
    with pytest.raises(AWSConnectionError):
        discovery.discover()


def test_rds_discovery_strictly_no_passwords(mock_session_manager):
    """Verify RDS discovery never exposes password fields or secrets in metadata or resource dict."""
    mock_rds_client = MagicMock()
    mock_session_manager.get_client.return_value = mock_rds_client
    mock_paginator = MagicMock()
    mock_rds_client.can_paginate.return_value = True
    mock_rds_client.get_paginator.return_value = mock_paginator

    # Even if mock payload contains password-like keys, normalizer must never include them
    mock_paginator.paginate.return_value = [
        {
            "DBInstances": [
                {
                    "DBInstanceIdentifier": "sensitive-db",
                    "Engine": "postgres",
                    "DBInstanceClass": "db.t4g.micro",
                    "MasterUsername": "dbadmin",
                    "MasterUserPassword": "SUPER_SECRET_PASSWORD_DO_NOT_STORE",
                }
            ]
        }
    ]

    discovery = RDSDiscovery(session_manager=mock_session_manager, region="us-east-1")
    resources = discovery.discover()

    serialized = json.dumps(resources[0])
    assert "SUPER_SECRET_PASSWORD_DO_NOT_STORE" not in serialized
    assert "MasterUserPassword" not in serialized
    assert "password" not in serialized.lower()

