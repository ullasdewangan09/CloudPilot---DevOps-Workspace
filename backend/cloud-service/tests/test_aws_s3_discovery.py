"""
AWS S3 Resource Discovery Tests — Phase 3B.3 Automated Test Suite.

Verifies:
1. Global bucket enumeration via list_buckets.
2. Canonical region resolution and normalization (None -> us-east-1, EU -> eu-west-1).
3. Optional metadata degradation (NoSuchTagSet, ServerSideEncryptionConfigurationNotFoundError, AccessDenied on sub-resources).
4. Root AccessDenied on list_buckets mapped to AWSAuthError.
5. Network error mapping to AWSConnectionError.
6. Zero data/object content access (GetObject never called).
7. Normalized resource_type matches ResourceType.S3_BUCKET.
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
from app.providers.aws.s3 import S3Discovery
from app.providers.aws.session import (
    AWSAuthError,
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


def test_s3_discovery_success_full(mock_session_manager):
    """Verify S3 discovery lists buckets globally and queries versioning, encryption, and tags."""
    mock_s3_client = MagicMock()
    mock_session_manager.get_client.return_value = mock_s3_client

    created_dt = datetime(2025, 4, 1, 12, 0, 0, tzinfo=timezone.utc)
    mock_s3_client.list_buckets.return_value = {
        "Buckets": [
            {"Name": "prod-app-assets", "CreationDate": created_dt}
        ]
    }
    mock_s3_client.get_bucket_location.return_value = {"LocationConstraint": "us-west-2"}
    mock_s3_client.get_bucket_versioning.return_value = {"Status": "Enabled"}
    mock_s3_client.get_bucket_encryption.return_value = {
        "ServerSideEncryptionConfiguration": {
            "Rules": [{"ApplyServerSideEncryptionByDefault": {"SSEAlgorithm": "AES256"}}]
        }
    }
    mock_s3_client.get_bucket_tagging.return_value = {
        "TagSet": [{"Key": "Environment", "Value": "Production"}]
    }

    discovery = S3Discovery(session_manager=mock_session_manager)
    resources = discovery.discover()

    assert len(resources) == 1
    res = resources[0]
    assert res["provider"] == "aws"
    assert res["resource_type"] == ResourceType.S3_BUCKET.value
    assert res["provider_resource_id"] == "prod-app-assets"
    assert res["region"] == "us-west-2"
    assert res["status"] == "available"
    assert res["tags"]["Environment"] == "Production"
    assert res["metadata"]["versioning_status"] == "Enabled"
    assert res["metadata"]["encryption"]["sse_algorithm"] == "AES256"
    assert res["metadata"]["arn"] == "arn:aws:s3:::prod-app-assets"

    # Confirm JSON serializability
    json.dumps(res)


def test_s3_region_normalization_rules(mock_session_manager):
    """Verify standard AWS S3 region quirks are normalized accurately."""
    mock_s3_client = MagicMock()
    mock_session_manager.get_client.return_value = mock_s3_client
    discovery = S3Discovery(session_manager=mock_session_manager)

    # 1. None -> 'us-east-1'
    mock_s3_client.get_bucket_location.return_value = {"LocationConstraint": None}
    assert discovery.get_bucket_region("bucket-none") == "us-east-1"

    # 2. Empty string -> 'us-east-1'
    mock_s3_client.get_bucket_location.return_value = {"LocationConstraint": ""}
    assert discovery.get_bucket_region("bucket-empty") == "us-east-1"

    # 3. 'EU' -> 'eu-west-1'
    mock_s3_client.get_bucket_location.return_value = {"LocationConstraint": "EU"}
    assert discovery.get_bucket_region("bucket-eu") == "eu-west-1"

    # 4. Standard explicit region
    mock_s3_client.get_bucket_location.return_value = {"LocationConstraint": "ap-south-1"}
    assert discovery.get_bucket_region("bucket-mumbai") == "ap-south-1"


def test_s3_optional_sub_resource_degradation(mock_session_manager):
    """Verify missing tags or encryption does NOT fail discovery of the bucket."""
    mock_s3_client = MagicMock()
    mock_session_manager.get_client.return_value = mock_s3_client

    mock_s3_client.list_buckets.return_value = {
        "Buckets": [{"Name": "unconfigured-bucket"}]
    }
    mock_s3_client.get_bucket_location.return_value = {"LocationConstraint": None}

    # Simulate NoSuchTagSet on tagging
    mock_s3_client.get_bucket_tagging.side_effect = ClientError(
        {"Error": {"Code": "NoSuchTagSet", "Message": "The TagSet does not exist"}},
        "GetBucketTagging"
    )

    # Simulate ServerSideEncryptionConfigurationNotFoundError on encryption
    mock_s3_client.get_bucket_encryption.side_effect = ClientError(
        {"Error": {"Code": "ServerSideEncryptionConfigurationNotFoundError", "Message": "Not found"}},
        "GetBucketEncryption"
    )

    # Simulate AccessDenied on versioning
    mock_s3_client.get_bucket_versioning.side_effect = ClientError(
        {"Error": {"Code": "AccessDenied", "Message": "Access Denied"}},
        "GetBucketVersioning"
    )

    discovery = S3Discovery(session_manager=mock_session_manager)
    resources = discovery.discover()

    assert len(resources) == 1
    res = resources[0]
    assert res["provider_resource_id"] == "unconfigured-bucket"
    assert res["region"] == "us-east-1"
    assert res["tags"] == {}
    assert res["metadata"]["encryption"]["status"] == "Disabled"
    assert res["metadata"]["versioning_status"] == "Unknown"
    json.dumps(res)


def test_s3_discovery_empty_buckets(mock_session_manager):
    """Verify empty account returns empty list cleanly."""
    mock_s3_client = MagicMock()
    mock_session_manager.get_client.return_value = mock_s3_client
    mock_s3_client.list_buckets.return_value = {"Buckets": []}

    discovery = S3Discovery(session_manager=mock_session_manager)
    resources = discovery.discover()
    assert resources == []


def test_s3_discovery_root_access_denied(mock_session_manager):
    """Verify AccessDenied on list_buckets raises AWSAuthError."""
    mock_s3_client = MagicMock()
    mock_session_manager.get_client.return_value = mock_s3_client
    mock_s3_client.list_buckets.side_effect = ClientError(
        {"Error": {"Code": "AccessDenied", "Message": "Access Denied"}},
        "ListBuckets"
    )

    discovery = S3Discovery(session_manager=mock_session_manager)
    with pytest.raises(AWSAuthError) as exc_info:
        discovery.discover()

    assert exc_info.value.code == "AccessDenied"


def test_s3_discovery_network_error(mock_session_manager):
    """Verify network connection failures raise AWSConnectionError."""
    mock_s3_client = MagicMock()
    mock_session_manager.get_client.return_value = mock_s3_client
    mock_s3_client.list_buckets.side_effect = EndpointConnectionError(endpoint_url="https://s3.amazonaws.com")

    discovery = S3Discovery(session_manager=mock_session_manager)
    with pytest.raises(AWSConnectionError):
        discovery.discover()


def test_s3_discovery_never_calls_get_object(mock_session_manager):
    """Verify S3 discovery strictly inspects bucket metadata and never retrieves object contents."""
    mock_s3_client = MagicMock()
    mock_session_manager.get_client.return_value = mock_s3_client

    mock_s3_client.list_buckets.return_value = {
        "Buckets": [{"Name": "private-vault"}]
    }
    mock_s3_client.get_bucket_location.return_value = {"LocationConstraint": None}
    mock_s3_client.get_bucket_versioning.return_value = {"Status": "Suspended"}
    mock_s3_client.get_bucket_encryption.return_value = {}
    mock_s3_client.get_bucket_tagging.return_value = {"TagSet": []}

    discovery = S3Discovery(session_manager=mock_session_manager)
    discovery.discover()

    # GetObject and ListObjects must NEVER be called
    assert not hasattr(mock_s3_client, "get_object") or mock_s3_client.get_object.call_count == 0
    assert not hasattr(mock_s3_client, "list_objects") or mock_s3_client.list_objects.call_count == 0
    assert not hasattr(mock_s3_client, "list_objects_v2") or mock_s3_client.list_objects_v2.call_count == 0

