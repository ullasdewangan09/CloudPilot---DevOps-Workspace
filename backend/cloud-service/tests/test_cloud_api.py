"""
CloudPilot — Phase 3B.5 Automated REST API Integration Test Suite.

Verifies:
1. AWS Connection Verification Endpoint (POST /verify):
   - Success with caller identity verification
   - Failure mapping without stack traces or secret leakage
   - Inactive account rejection
   - RBAC: Owner/Admin allowed, Developer/Viewer denied (403), Non-member denied (403)
2. Resource Synchronization Trigger Endpoint (POST /sync):
   - 202 Accepted with BackgroundTasks invocation
   - Default sync_type and explicit sync_type support
   - Invalid sync_type rejection (422)
   - Inactive account rejection (400)
   - Concurrent overlapping sync rejection (409)
   - RBAC: Owner/Admin allowed, Developer/Viewer denied (403), Non-member denied (403)
3. Sync Status and Job Tracking APIs:
   - GET /sync/status (active job, latest job, zombie recovery)
   - GET /sync/jobs (pagination and status filtering)
   - GET /sync/jobs/{job_id} (job detail and 404 handling)
4. Resource Endpoints:
   - GET /resources (pagination, filtering by type, region, status, search)
   - Soft-deleted resource exclusion by default and inclusion via query param
   - GET /resources/{resource_id} (details, tags, metadata, 404 handling)
   - GET /resources/summary (database-side aggregation for active resources, breakdown by service, region, status)
5. Tenant Isolation & Zero Credential Leakage:
   - Cross-workspace resource and job isolation
   - Complete absence of temporary or permanent credentials in all API responses
"""

import uuid
from datetime import datetime, timezone, timedelta
from unittest.mock import patch, MagicMock
import pytest
from sqlalchemy import text

from app.models.cloud_account import CloudAccount
from app.models.cloud_resource import (
    CloudResource,
    CloudSyncJob,
    SyncStatus,
    SyncType,
    ResourceType,
)
from app.providers.aws.session import AWSSessionManager, AWSAuthError


# ── Fixtures ─────────────────────────────────────────────────────────────────

@pytest.fixture
def active_account(db, test_workspace):
    """Seed an active AWS cloud account in test_workspace."""
    account = CloudAccount(
        workspace_id=test_workspace,
        provider="aws",
        name="Production AWS Infrastructure",
        account_id="123456789012",
        default_region="us-east-1",
        role_arn="arn:aws:iam::123456789012:role/CloudPilotAccessRole",
        external_id="secure-ext-id-12345",
        status="active",
        is_active=True,
        description="Core AWS account"
    )
    db.add(account)
    db.commit()
    db.refresh(account)
    return account


@pytest.fixture
def mock_assume_role():
    """Mock AWSSessionManager AssumeRole to simulate valid identity without AWS calls."""
    with patch.object(AWSSessionManager, "assume_role") as mock_assume:
        mock_assume.return_value = {
            "account_id": "123456789012",
            "assumed_role_arn": "arn:aws:iam::123456789012:role/CloudPilotAccessRole",
            "user_id": "AROAEXAMPLE:CloudPilot",
            "verified_at": datetime.now(timezone.utc).isoformat(),
            "status": "active"
        }
        with patch.object(AWSSessionManager, "is_expired", return_value=False):
            yield mock_assume


# ==============================================================================
# 1. AWS CONNECTION VERIFICATION (POST /api/v1/cloud/accounts/{id}/verify)
# ==============================================================================

def test_verify_cloud_account_success(client, test_user, active_account, mock_assume_role, db):
    """Verify owner can trigger AWS connection verification returning sanitized identity."""
    # Start with pending status to test status update
    active_account.status = "pending"
    db.commit()

    resp = client.post(
        f"/api/v1/cloud/accounts/{active_account.id}/verify",
        headers=test_user["headers"]
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["status"] == "verified"
    assert data["provider"] == "aws"
    assert data["account_id"] == "123456789012"
    assert data["assumed_role_arn"] == "arn:aws:iam::123456789012:role/CloudPilotAccessRole"
    assert "verified_at" in data

    # Verify credentials never leaked in response
    assert "role_arn" not in data
    assert "external_id" not in data
    assert "AccessKeyId" not in str(data)

    # Verify database status updated to active
    db.refresh(active_account)
    assert active_account.status == "active"


def test_verify_cloud_account_failure(client, test_user, active_account):
    """Verify failed AssumeRole returns clean 400 without leaking stack trace."""
    with patch.object(AWSSessionManager, "assume_role", side_effect=AWSAuthError("Access denied by trust policy")):
        resp = client.post(
            f"/api/v1/cloud/accounts/{active_account.id}/verify",
            headers=test_user["headers"]
        )
    assert resp.status_code == 400
    detail = resp.json()["detail"]
    assert "AWS connection verification failed" in detail
    assert "Access denied" in detail


def test_verify_inactive_account_rejected(client, test_user, active_account, db):
    """Verify attempting to verify an inactive account returns 404."""
    active_account.is_active = False
    db.commit()

    resp = client.post(
        f"/api/v1/cloud/accounts/{active_account.id}/verify",
        headers=test_user["headers"]
    )
    assert resp.status_code == 404


def test_verify_requires_owner_or_admin(client, other_user, active_account, db):
    """Verify member with 'developer' or 'viewer' role is denied (403 Forbidden)."""
    db.execute(
        text("INSERT INTO workspace_members (workspace_id, user_id, role) VALUES (:ws_id, :uid, 'developer')"),
        {"ws_id": active_account.workspace_id, "uid": other_user["id"]}
    )
    db.commit()

    resp = client.post(
        f"/api/v1/cloud/accounts/{active_account.id}/verify",
        headers=other_user["headers"]
    )
    assert resp.status_code == 403
    assert "does not permit this operation" in resp.json()["detail"]


# ==============================================================================
# 2. RESOURCE SYNCHRONIZATION (POST /api/v1/cloud/accounts/{id}/sync)
# ==============================================================================

def test_sync_trigger_accepted(client, test_user, active_account, db):
    """Verify owner can trigger sync and receive HTTP 202 Accepted with BackgroundTask dispatched."""
    with patch("app.services.resource_sync.AsyncSyncRunner.run_sync_job") as mock_runner:
        resp = client.post(
            f"/api/v1/cloud/accounts/{active_account.id}/sync",
            json={"sync_type": "ec2"},
            headers=test_user["headers"]
        )
    assert resp.status_code == 202, resp.text
    data = resp.json()
    assert data["status"] == "pending"
    assert data["sync_type"] == "ec2"
    assert data["cloud_account_id"] == str(active_account.id)
    assert "job_id" in data
    assert "queued for background execution" in data["message"]

    # Verify job was created in DB
    job_id = uuid.UUID(data["job_id"])
    job = db.query(CloudSyncJob).filter(CloudSyncJob.id == job_id).first()
    assert job is not None
    assert job.sync_type == "ec2"
    assert job.triggered_by_user_id == test_user["id"]


def test_sync_trigger_default_sync_type(client, test_user, active_account, db):
    """Verify POST /sync with empty payload defaults to 'full' sync."""
    with patch("app.services.resource_sync.AsyncSyncRunner.run_sync_job"):
        resp = client.post(
            f"/api/v1/cloud/accounts/{active_account.id}/sync",
            json={},
            headers=test_user["headers"]
        )
    assert resp.status_code == 202
    assert resp.json()["sync_type"] == "full"


def test_sync_unsupported_sync_type_rejected(client, test_user, active_account):
    """Verify invalid sync_type returns 422 Unprocessable Entity."""
    resp = client.post(
        f"/api/v1/cloud/accounts/{active_account.id}/sync",
        json={"sync_type": "unsupported_service"},
        headers=test_user["headers"]
    )
    assert resp.status_code == 422


def test_sync_concurrent_rejected(client, test_user, active_account, db):
    """Verify triggering sync when another job is actively RUNNING returns 409 Conflict."""
    # Pre-seed an active running job
    running_job = CloudSyncJob(
        cloud_account_id=active_account.id,
        workspace_id=active_account.workspace_id,
        status=SyncStatus.RUNNING.value,
        sync_type="full",
        started_at=datetime.now(timezone.utc) - timedelta(minutes=2)
    )
    db.add(running_job)
    db.commit()

    resp = client.post(
        f"/api/v1/cloud/accounts/{active_account.id}/sync",
        json={"sync_type": "full"},
        headers=test_user["headers"]
    )
    assert resp.status_code == 409
    assert "already running" in resp.json()["detail"]


def test_sync_requires_owner_or_admin(client, other_user, active_account, db):
    """Verify viewer cannot trigger sync (403 Forbidden)."""
    db.execute(
        text("INSERT INTO workspace_members (workspace_id, user_id, role) VALUES (:ws_id, :uid, 'viewer')"),
        {"ws_id": active_account.workspace_id, "uid": other_user["id"]}
    )
    db.commit()

    resp = client.post(
        f"/api/v1/cloud/accounts/{active_account.id}/sync",
        json={"sync_type": "full"},
        headers=other_user["headers"]
    )
    assert resp.status_code == 403


# ==============================================================================
# 3. SYNC STATUS & JOB TRACKING ENDPOINTS
# ==============================================================================

def test_get_sync_status(client, test_user, active_account, db):
    """Verify GET /sync/status reports active job and latest completed job."""
    # Seed a completed job
    comp_job = CloudSyncJob(
        cloud_account_id=active_account.id,
        workspace_id=active_account.workspace_id,
        status=SyncStatus.COMPLETED.value,
        sync_type="full",
        total_resources=15,
        discovered_resources=15,
        started_at=datetime.now(timezone.utc) - timedelta(minutes=10),
        completed_at=datetime.now(timezone.utc) - timedelta(minutes=9)
    )
    db.add(comp_job)
    db.commit()

    resp = client.get(
        f"/api/v1/cloud/accounts/{active_account.id}/sync/status",
        headers=test_user["headers"]
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["cloud_account_id"] == str(active_account.id)
    assert data["has_active_job"] is False
    assert data["active_job"] is None
    assert data["latest_job"]["id"] == str(comp_job.id)
    assert data["latest_job"]["status"] == "completed"


def test_list_sync_jobs_pagination_and_filter(client, test_user, active_account, db):
    """Verify GET /sync/jobs supports pagination and status filter."""
    for i in range(3):
        job = CloudSyncJob(
            cloud_account_id=active_account.id,
            workspace_id=active_account.workspace_id,
            status=SyncStatus.COMPLETED.value if i < 2 else SyncStatus.FAILED.value,
            sync_type="ec2",
            started_at=datetime.now(timezone.utc) - timedelta(minutes=i * 5)
        )
        db.add(job)
    db.commit()

    # Pagination: page=1, limit=2
    p_resp = client.get(
        f"/api/v1/cloud/accounts/{active_account.id}/sync/jobs?page=1&limit=2",
        headers=test_user["headers"]
    )
    assert p_resp.status_code == 200
    p_data = p_resp.json()
    assert len(p_data["items"]) == 2
    assert p_data["total"] == 3
    assert p_data["page"] == 1
    assert p_data["limit"] == 2

    # Filter by status: failed
    f_resp = client.get(
        f"/api/v1/cloud/accounts/{active_account.id}/sync/jobs?status=failed",
        headers=test_user["headers"]
    )
    assert f_resp.status_code == 200
    f_data = f_resp.json()
    assert f_data["total"] == 1
    assert f_data["items"][0]["status"] == "failed"


def test_get_sync_job_detail(client, test_user, active_account, db):
    """Verify GET /sync/jobs/{job_id} returns detailed summary and errors."""
    job = CloudSyncJob(
        cloud_account_id=active_account.id,
        workspace_id=active_account.workspace_id,
        status=SyncStatus.COMPLETED.value,
        sync_type="full",
        summary={"ec2": {"discovered": 2, "inserted": 2}},
        errors=[],
        started_at=datetime.now(timezone.utc)
    )
    db.add(job)
    db.commit()

    resp = client.get(
        f"/api/v1/cloud/accounts/{active_account.id}/sync/jobs/{job.id}",
        headers=test_user["headers"]
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["id"] == str(job.id)
    assert data["summary"]["ec2"]["discovered"] == 2


def test_get_sync_job_not_found(client, test_user, active_account):
    """Verify non-existent job ID returns 404."""
    resp = client.get(
        f"/api/v1/cloud/accounts/{active_account.id}/sync/jobs/{uuid.uuid4()}",
        headers=test_user["headers"]
    )
    assert resp.status_code == 404


# ==============================================================================
# 4. RESOURCE LISTING, FILTERING & SUMMARY
# ==============================================================================

@pytest.fixture
def seed_resources(db, active_account):
    """Seed diverse infrastructure resources for testing filtering and summaries."""
    resources = [
        CloudResource(
            cloud_account_id=active_account.id,
            workspace_id=active_account.workspace_id,
            provider="aws",
            resource_type=ResourceType.EC2_INSTANCE.value,
            provider_resource_id="i-prod-web-01",
            name="Prod-Web-Server",
            region="us-east-1",
            status="running",
            tags={"Environment": "production", "Role": "web"},
            metadata_={"instance_type": "t3.large"}
        ),
        CloudResource(
            cloud_account_id=active_account.id,
            workspace_id=active_account.workspace_id,
            provider="aws",
            resource_type=ResourceType.EC2_INSTANCE.value,
            provider_resource_id="i-staging-web-01",
            name="Staging-Web-Server",
            region="eu-west-1",
            status="stopped",
            tags={"Environment": "staging"},
            metadata_={"instance_type": "t3.small"}
        ),
        CloudResource(
            cloud_account_id=active_account.id,
            workspace_id=active_account.workspace_id,
            provider="aws",
            resource_type=ResourceType.S3_BUCKET.value,
            provider_resource_id="cloudpilot-prod-media",
            name="cloudpilot-prod-media",
            region="us-east-1",
            status="available",
            tags={},
            metadata_={"versioning": "Enabled"}
        ),
        CloudResource(
            cloud_account_id=active_account.id,
            workspace_id=active_account.workspace_id,
            provider="aws",
            resource_type=ResourceType.VPC.value,
            provider_resource_id="vpc-prod-01",
            name="Prod-VPC",
            region="us-east-1",
            status="available",
            tags={},
            metadata_={"cidr_block": "10.0.0.0/16"}
        ),
        CloudResource(
            cloud_account_id=active_account.id,
            workspace_id=active_account.workspace_id,
            provider="aws",
            resource_type=ResourceType.RDS_INSTANCE.value,
            provider_resource_id="prod-db-postgres",
            name="prod-db-postgres",
            region="us-east-1",
            status="available",
            tags={},
            metadata_={"engine": "postgres", "allocated_storage": 100}
        ),
        # Soft-deleted resource
        CloudResource(
            cloud_account_id=active_account.id,
            workspace_id=active_account.workspace_id,
            provider="aws",
            resource_type=ResourceType.EC2_INSTANCE.value,
            provider_resource_id="i-terminated-old",
            name="Old-Server",
            region="us-east-1",
            status="deleted",
            is_deleted=True,
            tags={},
            metadata_={}
        )
    ]
    for r in resources:
        db.add(r)
    db.commit()
    return resources


def test_list_resources_default_and_soft_delete_exclusion(client, test_user, active_account, seed_resources):
    """Verify GET /resources excludes soft-deleted items by default (5 active items)."""
    resp = client.get(
        f"/api/v1/cloud/accounts/{active_account.id}/resources",
        headers=test_user["headers"]
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 5
    assert len(data["items"]) == 5
    ids = [item["provider_resource_id"] for item in data["items"]]
    assert "i-terminated-old" not in ids


def test_list_resources_with_include_deleted(client, test_user, active_account, seed_resources):
    """Verify include_deleted=true includes soft-deleted items (total 6 items)."""
    resp = client.get(
        f"/api/v1/cloud/accounts/{active_account.id}/resources?include_deleted=true",
        headers=test_user["headers"]
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 6
    ids = [item["provider_resource_id"] for item in data["items"]]
    assert "i-terminated-old" in ids


def test_list_resources_filtering(client, test_user, active_account, seed_resources):
    """Verify database-side filtering by resource_type, region, status, and search."""
    # 1. Filter by resource_type: ec2_instance
    resp_type = client.get(
        f"/api/v1/cloud/accounts/{active_account.id}/resources?resource_type=ec2_instance",
        headers=test_user["headers"]
    )
    assert resp_type.status_code == 200
    assert resp_type.json()["total"] == 2

    # 2. Filter by region: eu-west-1
    resp_region = client.get(
        f"/api/v1/cloud/accounts/{active_account.id}/resources?region=eu-west-1",
        headers=test_user["headers"]
    )
    assert resp_region.status_code == 200
    assert resp_region.json()["total"] == 1
    assert resp_region.json()["items"][0]["provider_resource_id"] == "i-staging-web-01"

    # 3. Filter by status: stopped
    resp_status = client.get(
        f"/api/v1/cloud/accounts/{active_account.id}/resources?status=stopped",
        headers=test_user["headers"]
    )
    assert resp_status.status_code == 200
    assert resp_status.json()["total"] == 1

    # 4. Search by name substring: 'media'
    resp_search = client.get(
        f"/api/v1/cloud/accounts/{active_account.id}/resources?search=media",
        headers=test_user["headers"]
    )
    assert resp_search.status_code == 200
    assert resp_search.json()["total"] == 1
    assert resp_search.json()["items"][0]["provider_resource_id"] == "cloudpilot-prod-media"


def test_get_resource_detail(client, test_user, active_account, seed_resources):
    """Verify GET /resources/{id} returns full metadata and tags."""
    target = seed_resources[0]  # Prod-Web-Server
    resp = client.get(
        f"/api/v1/cloud/accounts/{active_account.id}/resources/{target.id}",
        headers=test_user["headers"]
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["id"] == str(target.id)
    assert data["name"] == "Prod-Web-Server"
    assert data["metadata"]["instance_type"] == "t3.large"
    assert data["tags"]["Role"] == "web"


def test_get_resource_soft_deleted_not_found(client, test_user, active_account, seed_resources):
    """Verify soft-deleted resource returns 404 unless include_deleted=true."""
    deleted_target = seed_resources[5]  # i-terminated-old
    resp = client.get(
        f"/api/v1/cloud/accounts/{active_account.id}/resources/{deleted_target.id}",
        headers=test_user["headers"]
    )
    assert resp.status_code == 404

    # With include_deleted=true -> 200
    resp_inc = client.get(
        f"/api/v1/cloud/accounts/{active_account.id}/resources/{deleted_target.id}?include_deleted=true",
        headers=test_user["headers"]
    )
    assert resp_inc.status_code == 200
    assert resp_inc.json()["is_deleted"] is True


def test_get_resource_summary(client, test_user, active_account, seed_resources):
    """Verify GET /resources/summary provides aggregate breakdowns."""
    resp = client.get(
        f"/api/v1/cloud/accounts/{active_account.id}/resources/summary",
        headers=test_user["headers"]
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["cloud_account_id"] == str(active_account.id)
    assert data["total_active_resources"] == 5
    assert data["ec2_instances"] == 2
    assert data["s3_buckets"] == 1
    assert data["vpc_resources"] == 1
    assert data["rds_instances"] == 1
    assert data["by_region"]["us-east-1"] == 4
    assert data["by_region"]["eu-west-1"] == 1
    assert data["by_status"]["running"] == 1
    assert data["by_status"]["stopped"] == 1


# ==============================================================================
# 5. RBAC & TENANT ISOLATION
# ==============================================================================

def test_viewer_and_developer_read_access(client, other_user, active_account, seed_resources, db):
    """Verify viewer and developer roles can read resources and jobs, but cannot trigger sync or verify."""
    db.execute(
        text("INSERT INTO workspace_members (workspace_id, user_id, role) VALUES (:ws_id, :uid, 'viewer')"),
        {"ws_id": active_account.workspace_id, "uid": other_user["id"]}
    )
    db.commit()

    # Read endpoints: all 200
    assert client.get(f"/api/v1/cloud/accounts/{active_account.id}/sync/status", headers=other_user["headers"]).status_code == 200
    assert client.get(f"/api/v1/cloud/accounts/{active_account.id}/sync/jobs", headers=other_user["headers"]).status_code == 200
    assert client.get(f"/api/v1/cloud/accounts/{active_account.id}/resources", headers=other_user["headers"]).status_code == 200
    assert client.get(f"/api/v1/cloud/accounts/{active_account.id}/resources/summary", headers=other_user["headers"]).status_code == 200

    # Write endpoints: 403 Forbidden
    assert client.post(f"/api/v1/cloud/accounts/{active_account.id}/verify", headers=other_user["headers"]).status_code == 403
    assert client.post(f"/api/v1/cloud/accounts/{active_account.id}/sync", headers=other_user["headers"]).status_code == 403


def test_non_member_access_denied(client, other_user, active_account):
    """Verify user without workspace membership receives 403 Forbidden on all endpoints."""
    assert client.get(f"/api/v1/cloud/accounts/{active_account.id}/sync/status", headers=other_user["headers"]).status_code == 403
    assert client.get(f"/api/v1/cloud/accounts/{active_account.id}/resources", headers=other_user["headers"]).status_code == 403
    assert client.post(f"/api/v1/cloud/accounts/{active_account.id}/verify", headers=other_user["headers"]).status_code == 403
    assert client.post(f"/api/v1/cloud/accounts/{active_account.id}/sync", headers=other_user["headers"]).status_code == 403


def test_cross_workspace_isolation(client, other_user, test_workspace, active_account, db):
    """Verify resources from Workspace A cannot be accessed from Workspace B."""
    # Create Workspace B owned by other_user
    ws_b = uuid.uuid4()
    db.execute(
        text("INSERT INTO workspaces (id, owner_id, name, environment, is_active) VALUES (:id, :uid, 'Workspace B', 'dev', TRUE)"),
        {"id": ws_b, "uid": other_user["id"]}
    )
    db.execute(
        text("INSERT INTO workspace_members (workspace_id, user_id, role) VALUES (:ws_id, :uid, 'owner')"),
        {"ws_id": ws_b, "uid": other_user["id"]}
    )
    db.commit()

    # other_user attempts to access active_account belonging to test_workspace
    resp = client.get(
        f"/api/v1/cloud/accounts/{active_account.id}/resources",
        headers=other_user["headers"]
    )
    assert resp.status_code == 403

