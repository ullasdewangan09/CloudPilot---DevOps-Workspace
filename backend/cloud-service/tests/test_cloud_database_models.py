"""
Database Architecture & Model Integrity Tests — Phase 3B.2 Automated Test Suite.

Verifies:
1. CloudResource model creation, default values, UUID PK, and PostgreSQL JSONB persistence.
2. CloudSyncJob model creation, statistics counters, default values, and JSONB error logging.
3. Compound Unique Constraint uq_account_resource_type_provider_id enforcement.
4. Identifier collision tolerance across distinct resource types.
5. Foreign Key CASCADE deletion when cloud_accounts or workspaces are removed.
6. Foreign Key SET NULL handling when the triggering user is removed.
7. Workspace-level tenant isolation and indexed filtering.
8. JSONB tag querying and metadata indexing.
9. Bidirectional ORM relationships between CloudAccount, CloudResource, and CloudSyncJob.
10. Strict credential absence: zero AWS temporary or permanent credentials in resource/job tables.
11. Database index verification in PostgreSQL catalog.
"""

import uuid
import pytest
from datetime import datetime, timezone
from sqlalchemy import text, select
from sqlalchemy.exc import IntegrityError

from app.models.cloud_account import CloudAccount
from app.models.cloud_resource import (
    CloudResource,
    CloudSyncJob,
    SyncStatus,
    SyncType,
    ResourceType,
)


# ── Helper Fixture to Seed Cloud Account ────────────────────────────────────

@pytest.fixture
def test_cloud_account(db, test_workspace):
    """Seed an active CloudAccount in test_workspace."""
    account = CloudAccount(
        workspace_id=test_workspace,
        provider="aws",
        name="Production AWS Core",
        account_id="123456789012",
        default_region="us-east-1",
        role_arn="gAAAAABtestCiphertextRoleArn",
        external_id="gAAAAABtestCiphertextExtId",
        status="active",
        is_active=True,
        description="Core AWS infrastructure account"
    )
    db.add(account)
    db.commit()
    db.refresh(account)
    return account


# ==============================================================================
# 1. MODEL INSTANTIATION & PERSISTENCE
# ==============================================================================

def test_cloud_resource_creation_and_persistence(db, test_cloud_account, test_workspace):
    """Verify CloudResource creation, UUID primary key, JSONB tags/metadata, and defaults."""
    res = CloudResource(
        cloud_account_id=test_cloud_account.id,
        workspace_id=test_workspace,
        provider="aws",
        resource_type=ResourceType.EC2_INSTANCE,
        provider_resource_id="i-0123456789abcdef0",
        name="api-gateway-node-1",
        region="us-east-1",
        status="running",
        tags={"Environment": "production", "Role": "api-gateway", "ManagedBy": "CloudPilot"},
        metadata_={
            "instance_type": "t3.large",
            "private_ip": "10.0.1.100",
            "public_ip": "54.12.34.56",
            "vpc_id": "vpc-0a1b2c3d4e5f",
            "subnet_id": "subnet-11223344"
        }
    )
    db.add(res)
    db.commit()
    db.refresh(res)

    # 1. Verify primary key and non-null constraints
    assert isinstance(res.id, uuid.UUID)
    assert res.cloud_account_id == test_cloud_account.id
    assert res.workspace_id == test_workspace
    assert res.provider == "aws"
    assert res.resource_type == "ec2_instance"
    assert res.provider_resource_id == "i-0123456789abcdef0"
    assert res.name == "api-gateway-node-1"
    assert res.region == "us-east-1"
    assert res.status == "running"
    assert res.is_deleted is False

    # 2. Verify JSONB tags and metadata
    assert res.tags["Environment"] == "production"
    assert res.tags["ManagedBy"] == "CloudPilot"
    assert res.metadata_["instance_type"] == "t3.large"
    assert res.metadata_["private_ip"] == "10.0.1.100"
    assert res.resource_metadata["vpc_id"] == "vpc-0a1b2c3d4e5f"

    # 3. Verify server timestamps
    assert res.first_discovered_at is not None
    assert res.last_discovered_at is not None
    assert res.created_at is not None
    assert res.updated_at is not None


def test_cloud_sync_job_creation_and_persistence(db, test_cloud_account, test_workspace, test_user):
    """Verify CloudSyncJob creation, resource counters, JSONB summary, and error tracking."""
    job = CloudSyncJob(
        cloud_account_id=test_cloud_account.id,
        workspace_id=test_workspace,
        triggered_by_user_id=test_user["id"],
        status=SyncStatus.COMPLETED,
        sync_type=SyncType.FULL,
        total_resources=16,
        discovered_resources=12,
        updated_resources=3,
        deleted_resources=1,
        summary={"ec2": 10, "s3": 3, "vpc": 2, "rds": 1},
        errors=[{"service": "rds", "region": "ap-south-1", "error": "AccessDenied"}]
    )
    db.add(job)
    db.commit()
    db.refresh(job)

    assert isinstance(job.id, uuid.UUID)
    assert job.cloud_account_id == test_cloud_account.id
    assert job.workspace_id == test_workspace
    assert job.triggered_by_user_id == test_user["id"]
    assert job.status == "completed"
    assert job.sync_type == "full"
    assert job.total_resources == 16
    assert job.discovered_resources == 12
    assert job.updated_resources == 3
    assert job.deleted_resources == 1
    assert job.summary["ec2"] == 10
    assert len(job.errors) == 1
    assert job.errors[0]["service"] == "rds"
    assert job.started_at is not None


# ==============================================================================
# 2. UNIQUE CONSTRAINTS & COLLISION TOLERANCE
# ==============================================================================

def test_compound_unique_constraint_prevents_duplicate_resources(db, test_cloud_account, test_workspace):
    """Verify compound unique constraint on (cloud_account_id, resource_type, provider_resource_id)."""
    res1 = CloudResource(
        cloud_account_id=test_cloud_account.id,
        workspace_id=test_workspace,
        provider="aws",
        resource_type=ResourceType.S3_BUCKET,
        provider_resource_id="prod-assets-bucket",
        name="prod-assets-bucket",
        region="us-east-1",
        status="available"
    )
    db.add(res1)
    db.commit()

    # Attempt to insert identical account + resource_type + provider_resource_id
    res_duplicate = CloudResource(
        cloud_account_id=test_cloud_account.id,
        workspace_id=test_workspace,
        provider="aws",
        resource_type=ResourceType.S3_BUCKET,
        provider_resource_id="prod-assets-bucket",
        name="prod-assets-bucket-clone",
        region="us-east-1",
        status="available"
    )
    db.add(res_duplicate)

    with pytest.raises(IntegrityError) as exc_info:
        db.commit()

    assert "uq_account_resource_type_provider_id" in str(exc_info.value)
    db.rollback()


def test_unique_constraint_allows_same_id_across_different_resource_types(db, test_cloud_account, test_workspace):
    """
    Verify compound unique constraint allows identical provider_resource_id across DIFFERENT resource types.
    For example: a VPC and a Security Group sharing the identifier or tag name 'core-network'.
    """
    shared_id = "core-network-default"

    res_vpc = CloudResource(
        cloud_account_id=test_cloud_account.id,
        workspace_id=test_workspace,
        provider="aws",
        resource_type=ResourceType.VPC,
        provider_resource_id=shared_id,
        name="Core VPC",
        region="us-east-1",
        status="available"
    )
    res_sg = CloudResource(
        cloud_account_id=test_cloud_account.id,
        workspace_id=test_workspace,
        provider="aws",
        resource_type=ResourceType.SECURITY_GROUP,
        provider_resource_id=shared_id,
        name="Core SG",
        region="us-east-1",
        status="available"
    )

    db.add(res_vpc)
    db.add(res_sg)
    db.commit()

    # Both must coexist cleanly
    assert res_vpc.id != res_sg.id
    assert res_vpc.provider_resource_id == res_sg.provider_resource_id
    assert res_vpc.resource_type != res_sg.resource_type


def test_unique_constraint_allows_same_id_across_different_cloud_accounts(db, test_workspace):
    """Verify identical resources can exist cleanly across different cloud accounts (multi-account isolation)."""
    acct1 = CloudAccount(
        workspace_id=test_workspace,
        provider="aws",
        name="Dev Account",
        account_id="111111111111",
        default_region="us-east-1",
        status="active"
    )
    acct2 = CloudAccount(
        workspace_id=test_workspace,
        provider="aws",
        name="Prod Account",
        account_id="222222222222",
        default_region="us-east-1",
        status="active"
    )
    db.add_all([acct1, acct2])
    db.commit()

    shared_instance_id = "i-1234567890abcdef0"

    res1 = CloudResource(
        cloud_account_id=acct1.id,
        workspace_id=test_workspace,
        provider="aws",
        resource_type=ResourceType.EC2_INSTANCE,
        provider_resource_id=shared_instance_id,
        region="us-east-1",
        status="running"
    )
    res2 = CloudResource(
        cloud_account_id=acct2.id,
        workspace_id=test_workspace,
        provider="aws",
        resource_type=ResourceType.EC2_INSTANCE,
        provider_resource_id=shared_instance_id,
        region="us-east-1",
        status="running"
    )
    db.add_all([res1, res2])
    db.commit()

    assert res1.id != res2.id
    assert res1.cloud_account_id == acct1.id
    assert res2.cloud_account_id == acct2.id


# ==============================================================================
# 3. FOREIGN KEY RELATIONSHIPS & CASCADING DELETION
# ==============================================================================

def test_deleting_cloud_account_cascades_to_resources_and_sync_jobs(db, test_cloud_account, test_workspace, test_user):
    """Verify deleting a CloudAccount cascades and deletes child resources and sync jobs."""
    account_id = test_cloud_account.id

    res = CloudResource(
        cloud_account_id=account_id,
        workspace_id=test_workspace,
        provider="aws",
        resource_type=ResourceType.S3_BUCKET,
        provider_resource_id="ephemeral-bucket-test",
        region="us-east-1",
        status="available"
    )
    job = CloudSyncJob(
        cloud_account_id=account_id,
        workspace_id=test_workspace,
        triggered_by_user_id=test_user["id"],
        status=SyncStatus.COMPLETED,
        sync_type=SyncType.FULL
    )
    db.add_all([res, job])
    db.commit()

    # Confirm rows exist
    res_count = db.execute(
        text("SELECT count(*) FROM cloud_resources WHERE cloud_account_id = :id"),
        {"id": account_id}
    ).scalar()
    job_count = db.execute(
        text("SELECT count(*) FROM cloud_sync_jobs WHERE cloud_account_id = :id"),
        {"id": account_id}
    ).scalar()
    assert res_count == 1
    assert job_count == 1

    # Delete cloud account
    db.delete(test_cloud_account)
    db.commit()

    # Child rows MUST be automatically cascaded and removed
    res_count_after = db.execute(
        text("SELECT count(*) FROM cloud_resources WHERE cloud_account_id = :id"),
        {"id": account_id}
    ).scalar()
    job_count_after = db.execute(
        text("SELECT count(*) FROM cloud_sync_jobs WHERE cloud_account_id = :id"),
        {"id": account_id}
    ).scalar()
    assert res_count_after == 0
    assert job_count_after == 0


def test_user_deletion_sets_null_on_cloud_sync_job(db, test_cloud_account, test_workspace):
    """Verify deleting the user who triggered a sync job preserves the job and sets triggered_by_user_id = NULL."""
    # Seed a non-owner auxiliary user to trigger the job
    aux_user_id = db.execute(text("""
        INSERT INTO users (username, email, password_hash, role, provider)
        VALUES ('aux_trigger_user', 'aux_trigger@example.com', 'dummyhash', 'developer', 'local')
        RETURNING id;
    """)).scalar()
    db.commit()

    job = CloudSyncJob(
        cloud_account_id=test_cloud_account.id,
        workspace_id=test_workspace,
        triggered_by_user_id=aux_user_id,
        status=SyncStatus.COMPLETED,
        sync_type=SyncType.FULL
    )
    db.add(job)
    db.commit()
    db.refresh(job)

    # Delete the triggering user directly from PostgreSQL
    db.execute(text("DELETE FROM users WHERE id = :uid"), {"uid": aux_user_id})
    db.commit()

    # Sync job MUST still exist, with triggered_by_user_id = None
    db.refresh(job)
    assert job.triggered_by_user_id is None
    assert job.status == "completed"


# ==============================================================================
# 4. WORKSPACE-LEVEL TENANT ISOLATION & JSONB QUERYING
# ==============================================================================

def test_workspace_tenant_isolation_filtering(db, test_user):
    """Verify resource queries isolated strictly by workspace_id prevent cross-tenant data leakage."""
    # Seed two independent workspaces
    ws_a = uuid.uuid4()
    ws_b = uuid.uuid4()
    db.execute(
        text("INSERT INTO workspaces (id, owner_id, name, environment, is_active) VALUES (:id, :owner_id, 'Workspace A', 'prod', TRUE)"),
        {"id": ws_a, "owner_id": test_user["id"]}
    )
    db.execute(
        text("INSERT INTO workspaces (id, owner_id, name, environment, is_active) VALUES (:id, :owner_id, 'Workspace B', 'prod', TRUE)"),
        {"id": ws_b, "owner_id": test_user["id"]}
    )

    # Seed accounts in each workspace
    acct_a = CloudAccount(workspace_id=ws_a, provider="aws", name="Acct A", account_id="111111111111")
    acct_b = CloudAccount(workspace_id=ws_b, provider="aws", name="Acct B", account_id="222222222222")
    db.add_all([acct_a, acct_b])
    db.commit()

    # Seed resources
    res_a = CloudResource(
        cloud_account_id=acct_a.id,
        workspace_id=ws_a,
        provider="aws",
        resource_type=ResourceType.EC2_INSTANCE,
        provider_resource_id="i-tenant-a",
        region="us-east-1"
    )
    res_b = CloudResource(
        cloud_account_id=acct_b.id,
        workspace_id=ws_b,
        provider="aws",
        resource_type=ResourceType.EC2_INSTANCE,
        provider_resource_id="i-tenant-b",
        region="us-east-1"
    )
    db.add_all([res_a, res_b])
    db.commit()

    # Workspace A query MUST return ONLY Workspace A resources
    results_a = db.query(CloudResource).filter(CloudResource.workspace_id == ws_a).all()
    assert len(results_a) == 1
    assert results_a[0].provider_resource_id == "i-tenant-a"

    # Workspace B query MUST return ONLY Workspace B resources
    results_b = db.query(CloudResource).filter(CloudResource.workspace_id == ws_b).all()
    assert len(results_b) == 1
    assert results_b[0].provider_resource_id == "i-tenant-b"


def test_jsonb_tag_querying(db, test_cloud_account, test_workspace):
    """Verify querying resources by JSONB tag keys and values using PostgreSQL JSONB operators."""
    res_prod = CloudResource(
        cloud_account_id=test_cloud_account.id,
        workspace_id=test_workspace,
        provider="aws",
        resource_type=ResourceType.EC2_INSTANCE,
        provider_resource_id="i-prod-node",
        region="us-east-1",
        tags={"Environment": "production", "CostCenter": "101"}
    )
    res_dev = CloudResource(
        cloud_account_id=test_cloud_account.id,
        workspace_id=test_workspace,
        provider="aws",
        resource_type=ResourceType.EC2_INSTANCE,
        provider_resource_id="i-dev-node",
        region="us-east-1",
        tags={"Environment": "development", "CostCenter": "102"}
    )
    db.add_all([res_prod, res_dev])
    db.commit()

    # Query resources where tags->>'Environment' = 'production'
    prod_results = db.query(CloudResource).filter(
        CloudResource.tags["Environment"].astext == "production"
    ).all()

    assert len(prod_results) == 1
    assert prod_results[0].provider_resource_id == "i-prod-node"


# ==============================================================================
# 5. ORM RELATIONSHIPS & CREDENTIAL SAFETY
# ==============================================================================

def test_orm_bidirectional_relationships(db, test_cloud_account, test_workspace):
    """Verify bidirectional ORM navigation between CloudAccount, CloudResource, and CloudSyncJob."""
    res = CloudResource(
        cloud_account_id=test_cloud_account.id,
        workspace_id=test_workspace,
        provider="aws",
        resource_type=ResourceType.RDS_INSTANCE,
        provider_resource_id="production-postgres-db",
        region="us-east-1"
    )
    job = CloudSyncJob(
        cloud_account_id=test_cloud_account.id,
        workspace_id=test_workspace,
        status=SyncStatus.PENDING,
        sync_type=SyncType.RDS
    )
    db.add_all([res, job])
    db.commit()

    db.refresh(test_cloud_account)

    # 1. Parent -> Children
    assert len(test_cloud_account.resources) == 1
    assert test_cloud_account.resources[0].provider_resource_id == "production-postgres-db"
    assert len(test_cloud_account.sync_jobs) == 1
    assert test_cloud_account.sync_jobs[0].sync_type == "rds"

    # 2. Child -> Parent
    assert res.account.id == test_cloud_account.id
    assert job.account.id == test_cloud_account.id


def test_no_temporary_credentials_stored_in_resource_or_job_tables(db):
    """Verify no credential columns exist in cloud_resources or cloud_sync_jobs schemas."""
    sensitive_columns = {
        "access_key_id",
        "secret_access_key",
        "session_token",
        "temporary_credentials",
        "aws_secret",
        "role_arn",
        "external_id"
    }

    for table_name in ["cloud_resources", "cloud_sync_jobs"]:
        cols = [r[0].lower() for r in db.execute(text("""
            SELECT column_name
            FROM information_schema.columns
            WHERE table_name = :tbl;
        """), {"tbl": table_name}).fetchall()]

        # None of the sensitive column names should exist in either table
        intersection = sensitive_columns.intersection(set(cols))
        assert not intersection, f"CRITICAL SECURITY DEFECT: Table {table_name} contains sensitive columns: {intersection}"


def test_database_indexes_exist(db):
    """Verify required performance and integrity indexes exist in PostgreSQL catalog."""
    expected_resource_indexes = {
        "ix_cloud_resources_workspace_id",
        "ix_cloud_resources_account_type",
        "ix_cloud_resources_region",
        "ix_cloud_resources_status",
        "ix_cloud_resources_is_deleted",
        "ix_cloud_resources_last_discovered",
        "ix_cloud_resources_tags"
    }

    expected_sync_job_indexes = {
        "ix_cloud_sync_jobs_workspace_id",
        "ix_cloud_sync_jobs_account_started",
        "ix_cloud_sync_jobs_status"
    }

    # Query all index names on cloud_resources
    res_indexes = {r[0] for r in db.execute(text("""
        SELECT indexname
        FROM pg_indexes
        WHERE tablename = 'cloud_resources';
    """)).fetchall()}

    for exp_idx in expected_resource_indexes:
        assert exp_idx in res_indexes, f"Missing expected index: {exp_idx} on cloud_resources"

    # Query all index names on cloud_sync_jobs
    job_indexes = {r[0] for r in db.execute(text("""
        SELECT indexname
        FROM pg_indexes
        WHERE tablename = 'cloud_sync_jobs';
    """)).fetchall()}

    for exp_idx in expected_sync_job_indexes:
        assert exp_idx in job_indexes, f"Missing expected index: {exp_idx} on cloud_sync_jobs"
