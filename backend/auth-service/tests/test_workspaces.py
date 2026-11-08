import uuid
import pytest
from app.models.workspace import Workspace, WorkspaceMember, WorkspaceAuditLog
from app.models.user import User


# ==============================================================================
# 1. WORKSPACE CRUD TESTS
# ==============================================================================

def test_create_workspace_success(client, auth_headers, test_user, db):
    """Verify workspace creation sets owner, creates owner membership, and logs audit."""
    payload = {
        "name": "Production Infrastructure",
        "description": "Core Kubernetes and AWS clusters",
        "environment": "production",
        "icon": "cloud-icon",
        "color": "#4f46e5"
    }
    response = client.post("/api/v1/workspaces", json=payload, headers=auth_headers)
    assert response.status_code == 201, response.text
    data = response.json()
    assert data["name"] == payload["name"]
    assert data["owner_id"] == test_user.id
    assert data["environment"] == "production"
    assert data["is_active"] is True
    workspace_id = data["id"]

    # 1. Verify owner membership record in database
    owner_member = db.query(WorkspaceMember).filter(
        WorkspaceMember.workspace_id == workspace_id,
        WorkspaceMember.user_id == test_user.id
    ).first()
    assert owner_member is not None
    assert owner_member.role == "owner"

    # 2. Verify audit log entry
    audit_log = db.query(WorkspaceAuditLog).filter(
        WorkspaceAuditLog.workspace_id == workspace_id,
        WorkspaceAuditLog.action == "WORKSPACE_CREATED"
    ).first()
    assert audit_log is not None
    assert audit_log.user_id == test_user.id


def test_get_workspace_by_id(client, auth_headers, test_user):
    """Verify owner can retrieve workspace details by UUID."""
    create_resp = client.post(
        "/api/v1/workspaces",
        json={"name": "Dev Workspace", "environment": "development"},
        headers=auth_headers
    )
    ws_id = create_resp.json()["id"]

    get_resp = client.get(f"/api/v1/workspaces/{ws_id}", headers=auth_headers)
    assert get_resp.status_code == 200
    assert get_resp.json()["id"] == ws_id
    assert get_resp.json()["name"] == "Dev Workspace"


def test_update_workspace(client, auth_headers, db):
    """Verify owner can update workspace metadata and action is logged in audit log."""
    create_resp = client.post(
        "/api/v1/workspaces",
        json={"name": "Old Name", "environment": "staging"},
        headers=auth_headers
    )
    ws_id = create_resp.json()["id"]

    update_payload = {"name": "Updated Staging Cluster", "color": "#10b981"}
    put_resp = client.put(f"/api/v1/workspaces/{ws_id}", json=update_payload, headers=auth_headers)
    assert put_resp.status_code == 200
    assert put_resp.json()["name"] == "Updated Staging Cluster"
    assert put_resp.json()["color"] == "#10b981"

    # Verify audit log recorded update
    audit = db.query(WorkspaceAuditLog).filter(
        WorkspaceAuditLog.workspace_id == ws_id,
        WorkspaceAuditLog.action == "WORKSPACE_UPDATED"
    ).first()
    assert audit is not None


def test_soft_delete_workspace(client, auth_headers, db):
    """Verify deleting a workspace sets is_active=False and removes it from active listings."""
    create_resp = client.post(
        "/api/v1/workspaces",
        json={"name": "To Be Deleted", "environment": "sandbox"},
        headers=auth_headers
    )
    ws_id = create_resp.json()["id"]

    del_resp = client.delete(f"/api/v1/workspaces/{ws_id}", headers=auth_headers)
    assert del_resp.status_code == 200

    # Direct DB inspection: row persists but is_active is False
    ws = db.query(Workspace).filter(Workspace.id == ws_id).first()
    assert ws is not None
    assert ws.is_active is False

    # Soft-deleted workspace cannot be retrieved via active GET endpoint
    get_resp = client.get(f"/api/v1/workspaces/{ws_id}", headers=auth_headers)
    assert get_resp.status_code == 404

    # Verify audit log recorded deletion
    audit = db.query(WorkspaceAuditLog).filter(
        WorkspaceAuditLog.workspace_id == ws_id,
        WorkspaceAuditLog.action == "WORKSPACE_DELETED"
    ).first()
    assert audit is not None


def test_workspace_pagination(client, auth_headers):
    """Verify listing workspaces respects pagination parameters."""
    for i in range(5):
        client.post(
            "/api/v1/workspaces",
            json={"name": f"Cluster-{i}", "environment": "development"},
            headers=auth_headers
        )

    # Request page 1 with limit 2
    resp = client.get("/api/v1/workspaces?page=1&limit=2", headers=auth_headers)
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 5
    assert len(data["items"]) == 2
    assert data["page"] == 1
    assert data["limit"] == 2


def test_workspace_search(client, auth_headers):
    """Verify workspace search matches on name or description substrings."""
    client.post(
        "/api/v1/workspaces",
        json={"name": "Alpha-K8s", "description": "Primary edge nodes", "environment": "production"},
        headers=auth_headers
    )
    client.post(
        "/api/v1/workspaces",
        json={"name": "Beta-Docker", "description": "Local test runner", "environment": "testing"},
        headers=auth_headers
    )

    search_resp = client.get("/api/v1/workspaces/search?q=Alpha", headers=auth_headers)
    assert search_resp.status_code == 200
    items = search_resp.json()
    assert len(items) == 1
    assert items[0]["name"] == "Alpha-K8s"


def test_workspace_stats(client, auth_headers):
    """Verify stats endpoint aggregates workspace counts by environment."""
    client.post("/api/v1/workspaces", json={"name": "WS1", "environment": "production"}, headers=auth_headers)
    client.post("/api/v1/workspaces", json={"name": "WS2", "environment": "production"}, headers=auth_headers)
    client.post("/api/v1/workspaces", json={"name": "WS3", "environment": "development"}, headers=auth_headers)

    stats_resp = client.get("/api/v1/workspaces/stats", headers=auth_headers)
    assert stats_resp.status_code == 200
    stats = stats_resp.json()
    assert stats["total_workspaces"] == 3
    assert stats["production"] == 2
    assert stats["development"] == 1


# ==============================================================================
# 2. WORKSPACE MEMBERSHIP & RBAC TESTS
# ==============================================================================

def test_add_and_list_workspace_members(client, auth_headers, second_user, db):
    """Verify owner can add another user to workspace and list members."""
    create_resp = client.post(
        "/api/v1/workspaces",
        json={"name": "Team Workspace", "environment": "staging"},
        headers=auth_headers
    )
    ws_id = create_resp.json()["id"]

    # Add second user as developer
    add_resp = client.post(
        f"/api/v1/workspaces/{ws_id}/members",
        json={"user_id": second_user.id, "role": "developer"},
        headers=auth_headers
    )
    assert add_resp.status_code == 201
    assert add_resp.json()["user_id"] == second_user.id
    assert add_resp.json()["role"] == "developer"

    # List members
    list_resp = client.get(f"/api/v1/workspaces/{ws_id}/members", headers=auth_headers)
    assert list_resp.status_code == 200
    members = list_resp.json()
    member_user_ids = [m["user_id"] for m in members]
    assert second_user.id in member_user_ids

    # Verify audit log
    audit = db.query(WorkspaceAuditLog).filter(
        WorkspaceAuditLog.workspace_id == ws_id,
        WorkspaceAuditLog.action == "WORKSPACE_MEMBER_ADDED"
    ).first()
    assert audit is not None


def test_add_duplicate_member_rejected(client, auth_headers, second_user):
    """Verify adding an existing member returns 400 Bad Request."""
    create_resp = client.post(
        "/api/v1/workspaces",
        json={"name": "Duplicate Test WS", "environment": "testing"},
        headers=auth_headers
    )
    ws_id = create_resp.json()["id"]

    client.post(
        f"/api/v1/workspaces/{ws_id}/members",
        json={"user_id": second_user.id, "role": "developer"},
        headers=auth_headers
    )
    # Attempt duplicate addition
    dup_resp = client.post(
        f"/api/v1/workspaces/{ws_id}/members",
        json={"user_id": second_user.id, "role": "admin"},
        headers=auth_headers
    )
    assert dup_resp.status_code == 400
    assert "already a member" in dup_resp.json()["detail"].lower()


def test_add_nonexistent_user_rejected(client, auth_headers):
    """Verify adding a non-existent user returns 404 Not Found."""
    create_resp = client.post(
        "/api/v1/workspaces",
        json={"name": "Ghost Test WS", "environment": "development"},
        headers=auth_headers
    )
    ws_id = create_resp.json()["id"]

    resp = client.post(
        f"/api/v1/workspaces/{ws_id}/members",
        json={"user_id": 999999, "role": "developer"},
        headers=auth_headers
    )
    assert resp.status_code == 404


def test_remove_workspace_member(client, auth_headers, second_user, db):
    """Verify owner can remove a non-owner member and action is logged in audit log."""
    create_resp = client.post(
        "/api/v1/workspaces",
        json={"name": "Removal WS", "environment": "development"},
        headers=auth_headers
    )
    ws_id = create_resp.json()["id"]

    client.post(
        f"/api/v1/workspaces/{ws_id}/members",
        json={"user_id": second_user.id, "role": "viewer"},
        headers=auth_headers
    )

    del_resp = client.delete(f"/api/v1/workspaces/{ws_id}/members/{second_user.id}", headers=auth_headers)
    assert del_resp.status_code == 200

    # Verify audit log
    audit = db.query(WorkspaceAuditLog).filter(
        WorkspaceAuditLog.workspace_id == ws_id,
        WorkspaceAuditLog.action == "WORKSPACE_MEMBER_REMOVED"
    ).first()
    assert audit is not None


def test_remove_owner_member_rejected(client, auth_headers, test_user):
    """Verify attempting to remove the workspace owner from membership returns 400."""
    create_resp = client.post(
        "/api/v1/workspaces",
        json={"name": "Owner Protect WS", "environment": "development"},
        headers=auth_headers
    )
    ws_id = create_resp.json()["id"]

    del_resp = client.delete(f"/api/v1/workspaces/{ws_id}/members/{test_user.id}", headers=auth_headers)
    assert del_resp.status_code == 400
    assert "owner cannot be removed" in del_resp.json()["detail"].lower()


# ==============================================================================
# 3. CROSS-WORKSPACE ISOLATION & AUTHORIZATION CHECKS
# ==============================================================================

def test_cross_tenant_isolation_get_workspace_forbidden(client, auth_headers, second_auth_headers):
    """Verify User B cannot access User A's private workspace (403 Forbidden)."""
    create_resp = client.post(
        "/api/v1/workspaces",
        json={"name": "User A Private WS", "environment": "production"},
        headers=auth_headers
    )
    ws_id = create_resp.json()["id"]

    # User B requests User A's workspace
    access_resp = client.get(f"/api/v1/workspaces/{ws_id}", headers=second_auth_headers)
    assert access_resp.status_code == 403


def test_cross_tenant_isolation_update_workspace_forbidden(client, auth_headers, second_auth_headers):
    """Verify User B cannot update User A's workspace (403 Forbidden)."""
    create_resp = client.post(
        "/api/v1/workspaces",
        json={"name": "User A Secure WS", "environment": "production"},
        headers=auth_headers
    )
    ws_id = create_resp.json()["id"]

    update_resp = client.put(
        f"/api/v1/workspaces/{ws_id}",
        json={"name": "Hijacked Workspace"},
        headers=second_auth_headers
    )
    assert update_resp.status_code == 403


def test_cross_tenant_isolation_delete_workspace_forbidden(client, auth_headers, second_auth_headers):
    """Verify User B cannot delete User A's workspace (403 Forbidden)."""
    create_resp = client.post(
        "/api/v1/workspaces",
        json={"name": "User A Indestructible WS", "environment": "production"},
        headers=auth_headers
    )
    ws_id = create_resp.json()["id"]

    del_resp = client.delete(f"/api/v1/workspaces/{ws_id}", headers=second_auth_headers)
    assert del_resp.status_code == 403

