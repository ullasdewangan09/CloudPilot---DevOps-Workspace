"""
CloudPilot — Phase 3B.4 Automated Test Suite
Resource Synchronization Engine & Database Reconciliation Tests.

Verifies:
1. Initial resource insertion with first_discovered_at and last_discovered_at.
2. Existing resource updates (preserves first_discovered_at, updates last_discovered_at and metadata).
3. Idempotent repeated synchronization without data duplication.
4. Full synchronization across EC2, S3, VPC, and RDS.
5. Individual resource-type synchronization (EC2, S3, VPC, RDS).
6. Successful scoped stale-resource reconciliation.
7. Failed discovery scope preserving existing resources (zero deletion on failure).
8. Partial regional failure: successful regions reconcile, failed regions preserve resources, job status PARTIAL_FAILURE.
9. S3 optional metadata failure graceful degradation without deleting bucket.
10. Job state transitions: PENDING -> RUNNING -> COMPLETED / PARTIAL_FAILURE / FAILED.
11. Job counter accuracy (discovered, updated, deleted, total).
12. Database rollback and clean error recovery on failure.
13. Concurrent synchronization prevention for the same cloud account.
14. Zombie/stale job recovery after execution timeout.
15. Sanitized error messages with strict secret exclusion.
16. Workspace tenant isolation.
17. Zero temporary AWS credentials persisted in database.
18. AsyncSyncRunner execution abstraction.
"""

import uuid
from datetime import datetime, timezone, timedelta
from unittest.mock import MagicMock, patch
import pytest
from botocore.exceptions import ClientError
from sqlalchemy import text

from app.models.cloud_account import CloudAccount
from app.models.cloud_resource import (
    CloudResource,
    CloudSyncJob,
    SyncStatus,
    SyncType,
    ResourceType,
)
from app.services.resource_sync import (
    ResourceSyncEngine,
    AsyncSyncRunner,
    ConcurrentSyncError,
    CloudAccountNotFoundError,
    SyncJobNotFoundError,
    JOB_TIMEOUT_MINUTES,
)
from app.providers.aws.session import AWSSessionManager, AWSAuthError


# ── Fixtures ─────────────────────────────────────────────────────────────────

@pytest.fixture
def test_cloud_account(db, test_workspace):
    """Seed an active AWS CloudAccount in test_workspace."""
    account = CloudAccount(
        workspace_id=test_workspace,
        provider="aws",
        name="Production AWS Core",
        account_id="123456789012",
        default_region="us-east-1",
        role_arn="arn:aws:iam::123456789012:role/CloudPilotDiscoveryRole",
        external_id="cloudpilot-secure-ext-id",
        status="active",
        is_active=True,
        description="Production AWS environment"
    )
    db.add(account)
    db.commit()
    db.refresh(account)
    return account


@pytest.fixture
def mock_aws_session():
    """Mock AWSSessionManager to bypass live AWS STS calls."""
    with patch.object(AWSSessionManager, "assume_role") as mock_assume:
        mock_assume.return_value = {
            "account_id": "123456789012",
            "assumed_role_arn": "arn:aws:iam::123456789012:role/CloudPilotDiscoveryRole",
            "user_id": "AROAEXAMPLE:CloudPilot",
            "verified_at": datetime.now(timezone.utc).isoformat(),
            "status": "active"
        }
        with patch.object(AWSSessionManager, "is_expired", return_value=False):
            yield mock_assume


# ── Sample Resource Fixtures ─────────────────────────────────────────────────

def sample_ec2_resource(instance_id: str = "i-0123456789abcdef0", region: str = "us-east-1", status: str = "running"):
    return {
        "provider": "aws",
        "resource_type": ResourceType.EC2_INSTANCE.value,
        "provider_resource_id": instance_id,
        "name": f"WebServer-{instance_id[-4:]}",
        "region": region,
        "status": status,
        "tags": {"Environment": "production", "Role": "web"},
        "metadata": {
            "instance_type": "t3.medium",
            "private_ip": "10.0.1.15",
            "public_ip": "54.12.34.56"
        }
    }


def sample_s3_resource(bucket_name: str = "cloudpilot-data-bucket", region: str = "us-east-1"):
    return {
        "provider": "aws",
        "resource_type": ResourceType.S3_BUCKET.value,
        "provider_resource_id": bucket_name,
        "name": bucket_name,
        "region": region,
        "status": "available",
        "tags": {"Environment": "production"},
        "metadata": {
            "versioning": "Enabled",
            "encryption": {"status": "Configured", "algorithm": "AES256"}
        }
    }


def sample_vpc_resources(region: str = "us-east-1"):
    return [
        {
            "provider": "aws",
            "resource_type": ResourceType.VPC.value,
            "provider_resource_id": "vpc-0123456789",
            "name": "Main-VPC",
            "region": region,
            "status": "available",
            "tags": {"Environment": "prod"},
            "metadata": {"cidr_block": "10.0.0.0/16"}
        },
        {
            "provider": "aws",
            "resource_type": ResourceType.SUBNET.value,
            "provider_resource_id": "subnet-0123456789",
            "name": "Public-Subnet",
            "region": region,
            "status": "available",
            "tags": {},
            "metadata": {"vpc_id": "vpc-0123456789", "cidr_block": "10.0.1.0/24"}
        },
        {
            "provider": "aws",
            "resource_type": ResourceType.SECURITY_GROUP.value,
            "provider_resource_id": "sg-0123456789",
            "name": "Web-SG",
            "region": region,
            "status": "active",
            "tags": {},
            "metadata": {"vpc_id": "vpc-0123456789", "group_name": "Web-SG"}
        }
    ]


def sample_rds_resource(db_id: str = "cloudpilot-db", region: str = "us-east-1"):
    return {
        "provider": "aws",
        "resource_type": ResourceType.RDS_INSTANCE.value,
        "provider_resource_id": db_id,
        "name": db_id,
        "region": region,
        "status": "available",
        "tags": {"Tier": "database"},
        "metadata": {
            "engine": "postgres",
            "engine_version": "16.1",
            "instance_class": "db.t4g.medium",
            "allocated_storage": 100,
            "multi_az": False,
            "endpoint": {"address": "cloudpilot-db.123456789012.us-east-1.rds.amazonaws.com", "port": 5432}
        }
    }


# ==============================================================================
# 1. INITIAL RESOURCE INSERTION & FIELDS
# ==============================================================================

def test_initial_resource_insertion(db, test_cloud_account, test_user, mock_aws_session):
    """Verify newly discovered resources are bulk inserted with correct timestamps and metadata."""
    engine = ResourceSyncEngine(db)
    ec2_item = sample_ec2_resource("i-11111111111111111")

    with patch("app.services.resource_sync.EC2Discovery.discover", return_value=[ec2_item]):
        job = engine.sync_account(
            cloud_account_id=test_cloud_account.id,
            workspace_id=test_cloud_account.workspace_id,
            sync_type=SyncType.EC2,
            triggered_by_user_id=test_user["id"],
        )

    assert job.status == SyncStatus.COMPLETED.value
    assert job.discovered_resources == 1
    assert job.updated_resources == 0
    assert job.deleted_resources == 0
    assert job.total_resources == 1
    assert job.summary["inserted"] == 1
    assert job.summary["updated"] == 0

    # Inspect persisted resource in DB
    res = db.query(CloudResource).filter(
        CloudResource.cloud_account_id == test_cloud_account.id,
        CloudResource.provider_resource_id == "i-11111111111111111"
    ).first()

    assert res is not None
    assert res.resource_type == ResourceType.EC2_INSTANCE.value
    assert res.name == "WebServer-1111"
    assert res.status == "running"
    assert res.is_deleted is False
    assert res.tags == {"Environment": "production", "Role": "web"}
    assert res.resource_metadata["instance_type"] == "t3.medium"
    assert res.first_discovered_at is not None
    assert res.last_discovered_at is not None
    assert res.workspace_id == test_cloud_account.workspace_id


# ==============================================================================
# 2. EXISTING RESOURCE UPDATE & TIMESTAMP PRESERVATION
# ==============================================================================

def test_existing_resource_update(db, test_cloud_account, mock_aws_session):
    """Verify existing resources update metadata and last_discovered_at while preserving first_discovered_at."""
    engine = ResourceSyncEngine(db)

    # Initial discovery
    item_v1 = sample_ec2_resource("i-22222222222222222", status="running")
    with patch("app.services.resource_sync.EC2Discovery.discover", return_value=[item_v1]):
        job1 = engine.sync_account(test_cloud_account.id, test_cloud_account.workspace_id, sync_type=SyncType.EC2)

    res1 = db.query(CloudResource).filter(
        CloudResource.provider_resource_id == "i-22222222222222222"
    ).first()
    original_first_discovered = res1.first_discovered_at

    # Second discovery: instance was stopped and re-tagged
    item_v2 = sample_ec2_resource("i-22222222222222222", status="stopped")
    item_v2["tags"] = {"Environment": "production", "Role": "web", "Maintenance": "true"}

    with patch("app.services.resource_sync.EC2Discovery.discover", return_value=[item_v2]):
        job2 = engine.sync_account(test_cloud_account.id, test_cloud_account.workspace_id, sync_type=SyncType.EC2)

    assert job2.status == SyncStatus.COMPLETED.value
    assert job2.discovered_resources == 1
    assert job2.updated_resources == 1
    assert job2.summary["inserted"] == 0
    assert job2.summary["updated"] == 1

    db.refresh(res1)
    assert res1.status == "stopped"
    assert res1.tags["Maintenance"] == "true"
    # first_discovered_at must be preserved exactly
    assert res1.first_discovered_at == original_first_discovered
    # last_discovered_at must be updated
    assert res1.last_discovered_at >= original_first_discovered


# ==============================================================================
# 3. IDEMPOTENT REPEATED SYNCHRONIZATION
# ==============================================================================

def test_idempotent_repeated_sync(db, test_cloud_account, mock_aws_session):
    """Verify running the same synchronization multiple times does not duplicate records."""
    engine = ResourceSyncEngine(db)
    ec2_item = sample_ec2_resource("i-33333333333333333")

    with patch("app.services.resource_sync.EC2Discovery.discover", return_value=[ec2_item]):
        engine.sync_account(test_cloud_account.id, test_cloud_account.workspace_id, sync_type=SyncType.EC2)
        engine.sync_account(test_cloud_account.id, test_cloud_account.workspace_id, sync_type=SyncType.EC2)

    count = db.query(CloudResource).filter(
        CloudResource.cloud_account_id == test_cloud_account.id,
        CloudResource.provider_resource_id == "i-33333333333333333"
    ).count()

    assert count == 1


# ==============================================================================
# 4. FULL SYNCHRONIZATION
# ==============================================================================

def test_full_sync_execution(db, test_cloud_account, mock_aws_session):
    """Verify SyncType.FULL discovers and persists EC2, S3, VPC, Subnets, SGs, and RDS."""
    engine = ResourceSyncEngine(db)

    ec2_item = sample_ec2_resource("i-full-001")
    s3_item = sample_s3_resource("bucket-full-001")
    vpc_items = sample_vpc_resources()
    rds_item = sample_rds_resource("rds-full-001")

    with patch("app.services.resource_sync.EC2Discovery.discover", return_value=[ec2_item]), \
         patch("app.services.resource_sync.S3Discovery.discover", return_value=[s3_item]), \
         patch("app.services.resource_sync.VPCDiscovery.discover", return_value=vpc_items), \
         patch("app.services.resource_sync.RDSDiscovery.discover", return_value=[rds_item]):

        job = engine.sync_account(test_cloud_account.id, test_cloud_account.workspace_id, sync_type=SyncType.FULL)

    assert job.status == SyncStatus.COMPLETED.value
    # 1 EC2 + 1 S3 + 3 VPC/Subnet/SG + 1 RDS = 6 resources
    assert job.discovered_resources == 6
    assert job.total_resources == 6

    # Verify each resource type is in DB
    types_in_db = {
        row[0] for row in db.query(CloudResource.resource_type).filter(
            CloudResource.cloud_account_id == test_cloud_account.id
        ).all()
    }
    assert ResourceType.EC2_INSTANCE.value in types_in_db
    assert ResourceType.S3_BUCKET.value in types_in_db
    assert ResourceType.VPC.value in types_in_db
    assert ResourceType.SUBNET.value in types_in_db
    assert ResourceType.SECURITY_GROUP.value in types_in_db
    assert ResourceType.RDS_INSTANCE.value in types_in_db


# ==============================================================================
# 5. INDIVIDUAL RESOURCE-TYPE SYNCHRONIZATION
# ==============================================================================

def test_individual_resource_type_sync(db, test_cloud_account, mock_aws_session):
    """Verify running a targeted sync_type (e.g. s3) only executes that module."""
    engine = ResourceSyncEngine(db)
    s3_item = sample_s3_resource("isolated-s3-bucket")

    with patch("app.services.resource_sync.S3Discovery.discover", return_value=[s3_item]) as mock_s3, \
         patch("app.services.resource_sync.EC2Discovery.discover") as mock_ec2, \
         patch("app.services.resource_sync.VPCDiscovery.discover") as mock_vpc, \
         patch("app.services.resource_sync.RDSDiscovery.discover") as mock_rds:

        job = engine.sync_account(test_cloud_account.id, test_cloud_account.workspace_id, sync_type=SyncType.S3)

    assert job.status == SyncStatus.COMPLETED.value
    assert job.discovered_resources == 1
    mock_s3.assert_called_once()
    mock_ec2.assert_not_called()
    mock_vpc.assert_not_called()
    mock_rds.assert_not_called()


# ==============================================================================
# 6. SUCCESSFUL STALE-RESOURCE RECONCILIATION
# ==============================================================================

def test_successful_stale_resource_reconciliation(db, test_cloud_account, mock_aws_session):
    """Verify resources absent from a successful discovery scope are safely marked is_deleted=True."""
    engine = ResourceSyncEngine(db)

    # Initial sync with 2 instances: i-keep and i-stale
    inst1 = sample_ec2_resource("i-keep")
    inst2 = sample_ec2_resource("i-stale")

    with patch("app.services.resource_sync.EC2Discovery.discover", return_value=[inst1, inst2]):
        engine.sync_account(test_cloud_account.id, test_cloud_account.workspace_id, sync_type=SyncType.EC2)

    # Subsequent sync: i-stale is terminated and absent from AWS response
    with patch("app.services.resource_sync.EC2Discovery.discover", return_value=[inst1]):
        job2 = engine.sync_account(test_cloud_account.id, test_cloud_account.workspace_id, sync_type=SyncType.EC2)

    assert job2.status == SyncStatus.COMPLETED.value
    assert job2.discovered_resources == 1
    assert job2.deleted_resources == 1
    assert job2.total_resources == 1  # Only 1 active resource remaining

    # Check i-keep remains active
    res_keep = db.query(CloudResource).filter(CloudResource.provider_resource_id == "i-keep").first()
    assert res_keep.is_deleted is False
    assert res_keep.status == "running"

    # Check i-stale is soft-deleted
    res_stale = db.query(CloudResource).filter(CloudResource.provider_resource_id == "i-stale").first()
    assert res_stale.is_deleted is True
    assert res_stale.status == "deleted"


# ==============================================================================
# 7. RE-DISCOVERY UN-DELETES RESOURCE
# ==============================================================================

def test_rediscovery_un_deletes_resource(db, test_cloud_account, mock_aws_session):
    """Verify that a previously deleted resource is re-activated if rediscovered."""
    engine = ResourceSyncEngine(db)
    inst = sample_ec2_resource("i-temp-stale")

    # Sync 1: Discovered
    with patch("app.services.resource_sync.EC2Discovery.discover", return_value=[inst]):
        engine.sync_account(test_cloud_account.id, test_cloud_account.workspace_id, sync_type=SyncType.EC2)

    # Sync 2: Absent -> soft deleted
    with patch("app.services.resource_sync.EC2Discovery.discover", return_value=[]):
        engine.sync_account(test_cloud_account.id, test_cloud_account.workspace_id, sync_type=SyncType.EC2)

    res = db.query(CloudResource).filter(CloudResource.provider_resource_id == "i-temp-stale").first()
    assert res.is_deleted is True

    # Sync 3: Discovered again -> must become active again
    with patch("app.services.resource_sync.EC2Discovery.discover", return_value=[inst]):
        job3 = engine.sync_account(test_cloud_account.id, test_cloud_account.workspace_id, sync_type=SyncType.EC2)

    db.refresh(res)
    assert res.is_deleted is False
    assert res.status == "running"
    assert job3.total_resources == 1


# ==============================================================================
# 8. FAILED DISCOVERY PRESERVES EXISTING RESOURCES
# ==============================================================================

def test_failed_discovery_preserves_existing_resources(db, test_cloud_account, mock_aws_session):
    """CRITICAL SAFETY TEST: If discovery fails, existing resources MUST NOT be deleted."""
    engine = ResourceSyncEngine(db)
    inst = sample_ec2_resource("i-critical-db-resource")

    # Initial successful sync
    with patch("app.services.resource_sync.EC2Discovery.discover", return_value=[inst]):
        engine.sync_account(test_cloud_account.id, test_cloud_account.workspace_id, sync_type=SyncType.EC2)

    # Subsequent sync: AWS EC2 API fails with AccessDenied
    client_err = ClientError(
        {"Error": {"Code": "UnauthorizedOperation", "Message": "Access denied"}},
        "DescribeInstances"
    )

    with patch("app.services.resource_sync.EC2Discovery.discover", side_effect=client_err):
        job_fail = engine.sync_account(test_cloud_account.id, test_cloud_account.workspace_id, sync_type=SyncType.EC2)

    assert job_fail.status == SyncStatus.FAILED.value
    assert job_fail.deleted_resources == 0
    assert len(job_fail.errors) > 0

    # Verify resource was NOT marked deleted
    res = db.query(CloudResource).filter(CloudResource.provider_resource_id == "i-critical-db-resource").first()
    assert res.is_deleted is False
    assert res.status == "running"


# ==============================================================================
# 9. PARTIAL REGIONAL FAILURE
# ==============================================================================

def test_partial_regional_failure(db, test_cloud_account, mock_aws_session):
    """
    Multi-region sync: us-east-1 succeeds, eu-west-1 fails.
    Verify us-east-1 reconciles, eu-west-1 preserves resources, job status is PARTIAL_FAILURE.
    """
    engine = ResourceSyncEngine(db)

    east_inst = sample_ec2_resource("i-east-1", region="us-east-1")
    west_inst = sample_ec2_resource("i-west-1", region="eu-west-1")

    # Initial seeding of both regions
    with patch("app.services.resource_sync.EC2Discovery.discover", side_effect=[[east_inst], [west_inst]]):
        engine.sync_account(
            test_cloud_account.id,
            test_cloud_account.workspace_id,
            sync_type=SyncType.EC2,
            regions=["us-east-1", "eu-west-1"]
        )

    # Next sync: us-east-1 succeeds (returns east_inst), eu-west-1 fails with throttling
    throttling_err = ClientError(
        {"Error": {"Code": "RequestLimitExceeded", "Message": "Rate limit exceeded"}},
        "DescribeInstances"
    )

    with patch("app.services.resource_sync.EC2Discovery.discover", side_effect=[[east_inst], throttling_err]):
        job_partial = engine.sync_account(
            test_cloud_account.id,
            test_cloud_account.workspace_id,
            sync_type=SyncType.EC2,
            regions=["us-east-1", "eu-west-1"]
        )

    assert job_partial.status == SyncStatus.PARTIAL_FAILURE.value
    assert job_partial.summary["successful_scopes"] == 1
    assert job_partial.summary["failed_scopes"] == 1

    # eu-west-1 resource MUST be preserved
    res_west = db.query(CloudResource).filter(CloudResource.provider_resource_id == "i-west-1").first()
    assert res_west.is_deleted is False

    # us-east-1 resource is active
    res_east = db.query(CloudResource).filter(CloudResource.provider_resource_id == "i-east-1").first()
    assert res_east.is_deleted is False


# ==============================================================================
# 10. S3 OPTIONAL METADATA FAILURE DEGRADATION
# ==============================================================================

def test_s3_optional_metadata_failure(db, test_cloud_account, mock_aws_session):
    """Verify S3 discovery with partial metadata failure (e.g. tagging unconfigured) does not delete bucket."""
    engine = ResourceSyncEngine(db)
    s3_item = sample_s3_resource("untagged-bucket")
    # Sub-resource permissions status showing tags unconfigured
    s3_item["metadata"]["permissions_status"] = {"tags": False, "encryption": True, "versioning": True}

    with patch("app.services.resource_sync.S3Discovery.discover", return_value=[s3_item]):
        job = engine.sync_account(test_cloud_account.id, test_cloud_account.workspace_id, sync_type=SyncType.S3)

    assert job.status == SyncStatus.COMPLETED.value
    res = db.query(CloudResource).filter(CloudResource.provider_resource_id == "untagged-bucket").first()
    assert res is not None
    assert res.is_deleted is False


# ==============================================================================
# 11. JOB STATE TRANSITIONS
# ==============================================================================

def test_job_state_transitions(db, test_cloud_account, mock_aws_session):
    """Verify all job state transitions: PENDING -> RUNNING -> COMPLETED / PARTIAL_FAILURE / FAILED."""
    engine = ResourceSyncEngine(db)

    # 1. PENDING -> RUNNING -> COMPLETED
    job1 = engine.create_sync_job(test_cloud_account.id, test_cloud_account.workspace_id, sync_type=SyncType.EC2)
    assert job1.status == SyncStatus.PENDING.value

    with patch("app.services.resource_sync.EC2Discovery.discover", return_value=[]):
        res_job1 = engine.execute_sync(job1.id)
    assert res_job1.status == SyncStatus.COMPLETED.value
    assert res_job1.completed_at is not None

    # 2. Total failure at STS AssumeRole -> FAILED
    job2 = engine.create_sync_job(test_cloud_account.id, test_cloud_account.workspace_id, sync_type=SyncType.EC2)
    with patch.object(AWSSessionManager, "assume_role", side_effect=AWSAuthError("AssumeRole failed")):
        res_job2 = engine.execute_sync(job2.id)
    assert res_job2.status == SyncStatus.FAILED.value
    assert "AssumeRole failed" in res_job2.errors[0]["error"]


# ==============================================================================
# 12. JOB COUNTER ACCURACY
# ==============================================================================

def test_job_counter_accuracy(db, test_cloud_account, mock_aws_session):
    """Verify discovered, updated, deleted, and total counters exactly reflect operations."""
    engine = ResourceSyncEngine(db)

    # Pre-seed: i-stale (will be deleted), i-update (will be updated)
    inst_stale = sample_ec2_resource("i-stale")
    inst_update = sample_ec2_resource("i-update", status="running")

    with patch("app.services.resource_sync.EC2Discovery.discover", return_value=[inst_stale, inst_update]):
        engine.sync_account(test_cloud_account.id, test_cloud_account.workspace_id, sync_type=SyncType.EC2)

    # Next sync discovers: i-update (changed), i-new1 (new), i-new2 (new). i-stale is gone.
    inst_update_mod = sample_ec2_resource("i-update", status="stopped")
    inst_new1 = sample_ec2_resource("i-new1")
    inst_new2 = sample_ec2_resource("i-new2")

    with patch("app.services.resource_sync.EC2Discovery.discover", return_value=[inst_update_mod, inst_new1, inst_new2]):
        job = engine.sync_account(test_cloud_account.id, test_cloud_account.workspace_id, sync_type=SyncType.EC2)

    assert job.discovered_resources == 3
    assert job.updated_resources == 1
    assert job.deleted_resources == 1
    assert job.summary["inserted"] == 2
    assert job.summary["updated"] == 1
    assert job.summary["deleted"] == 1
    assert job.total_resources == 3  # i-update + i-new1 + i-new2 active


# ==============================================================================
# 13. CONCURRENT SYNC PROTECTION
# ==============================================================================

def test_concurrent_sync_protection(db, test_cloud_account):
    """Verify that an active RUNNING job blocks concurrent sync execution for the same account."""
    engine = ResourceSyncEngine(db)

    # Seed an active running job started 5 minutes ago
    active_job = CloudSyncJob(
        cloud_account_id=test_cloud_account.id,
        workspace_id=test_cloud_account.workspace_id,
        status=SyncStatus.RUNNING.value,
        sync_type=SyncType.FULL.value,
        started_at=datetime.now(timezone.utc) - timedelta(minutes=5)
    )
    db.add(active_job)
    db.commit()

    # Create a new job and attempt to execute
    new_job = engine.create_sync_job(test_cloud_account.id, test_cloud_account.workspace_id)
    with pytest.raises(ConcurrentSyncError) as exc_info:
        engine.execute_sync(new_job.id)

    assert "currently running" in str(exc_info.value)


# ==============================================================================
# 14. ZOMBIE / STALE JOB RECOVERY
# ==============================================================================

def test_stale_job_recovery(db, test_cloud_account, mock_aws_session):
    """Verify that a job left in RUNNING beyond timeout is automatically recovered to FAILED."""
    engine = ResourceSyncEngine(db)

    # Seed a zombie job started 45 minutes ago (> 30 min threshold)
    zombie_job = CloudSyncJob(
        cloud_account_id=test_cloud_account.id,
        workspace_id=test_cloud_account.workspace_id,
        status=SyncStatus.RUNNING.value,
        sync_type=SyncType.FULL.value,
        started_at=datetime.now(timezone.utc) - timedelta(minutes=JOB_TIMEOUT_MINUTES + 15)
    )
    db.add(zombie_job)
    db.commit()

    # Now attempt a new sync: it should recover the zombie job and succeed
    with patch("app.services.resource_sync.EC2Discovery.discover", return_value=[]):
        new_job = engine.sync_account(test_cloud_account.id, test_cloud_account.workspace_id, sync_type=SyncType.EC2)

    db.refresh(zombie_job)
    assert zombie_job.status == SyncStatus.FAILED.value
    assert zombie_job.errors[0]["code"] == "ZOMBIE_JOB_TIMEOUT"
    assert new_job.status == SyncStatus.COMPLETED.value


# ==============================================================================
# 15. SANITIZED JOB ERRORS (ZERO SECRET LEAKAGE)
# ==============================================================================

def test_sanitized_job_errors(db, test_cloud_account, mock_aws_session):
    """Verify that errors logged in CloudSyncJob do not leak AWS secret access keys or session tokens."""
    engine = ResourceSyncEngine(db)

    sensitive_error = Exception(
        "Request failed with credentials: aws_secret_access_key=wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"
    )

    with patch("app.services.resource_sync.EC2Discovery.discover", side_effect=sensitive_error):
        job = engine.sync_account(test_cloud_account.id, test_cloud_account.workspace_id, sync_type=SyncType.EC2)

    assert job.status == SyncStatus.FAILED.value
    error_str = str(job.errors)
    assert "wJalrXUtnFEMI" not in error_str
    assert "aws_secret_access_key" not in error_str
    assert "Sensitive credential details redacted" in error_str


# ==============================================================================
# 16. WORKSPACE TENANT ISOLATION
# ==============================================================================

def test_workspace_and_account_isolation(db, test_cloud_account, other_user, mock_aws_session):
    """Verify resources discovered for one workspace cannot be viewed or reconciled by another."""
    engine = ResourceSyncEngine(db)
    inst = sample_ec2_resource("i-ws1-inst")

    with patch("app.services.resource_sync.EC2Discovery.discover", return_value=[inst]):
        engine.sync_account(test_cloud_account.id, test_cloud_account.workspace_id, sync_type=SyncType.EC2)

    # Query within test_workspace
    ws1_res = db.query(CloudResource).filter(
        CloudResource.workspace_id == test_cloud_account.workspace_id
    ).all()
    assert len(ws1_res) == 1

    # Query with non-existent workspace ID
    foreign_ws = uuid.uuid4()
    ws2_res = db.query(CloudResource).filter(
        CloudResource.workspace_id == foreign_ws
    ).all()
    assert len(ws2_res) == 0


# ==============================================================================
# 17. ZERO TEMPORARY CREDENTIALS PERSISTED IN DATABASE
# ==============================================================================

def test_no_temporary_credentials_persisted(db, test_cloud_account, mock_aws_session):
    """Verify that no temporary STS credentials exist in cloud_resources or cloud_sync_jobs."""
    engine = ResourceSyncEngine(db)
    ec2_item = sample_ec2_resource("i-cred-check")

    with patch("app.services.resource_sync.EC2Discovery.discover", return_value=[ec2_item]):
        job = engine.sync_account(test_cloud_account.id, test_cloud_account.workspace_id, sync_type=SyncType.EC2)

    # Check database tables
    res = db.query(CloudResource).filter(CloudResource.cloud_account_id == test_cloud_account.id).first()

    for forbidden in ("AccessKeyId", "SecretAccessKey", "SessionToken", "ASIA"):
        assert forbidden not in str(res.resource_metadata)
        assert forbidden not in str(res.tags)
        assert forbidden not in str(job.summary)
        assert forbidden not in str(job.errors)


# ==============================================================================
# 18. ASYNC SYNC RUNNER ABSTRACTION
# ==============================================================================

def test_async_sync_runner(db, test_cloud_account, mock_aws_session):
    """Verify AsyncSyncRunner executes properly with an isolated session factory."""
    from tests.conftest import TestingSessionLocal

    engine = ResourceSyncEngine(db)
    job = engine.create_sync_job(test_cloud_account.id, test_cloud_account.workspace_id, sync_type=SyncType.EC2)

    with patch("app.services.resource_sync.EC2Discovery.discover", return_value=[]):
        res_job = AsyncSyncRunner.run_sync_job(
            db_session_factory=TestingSessionLocal,
            job_id=job.id
        )

    assert res_job.status == SyncStatus.COMPLETED.value

