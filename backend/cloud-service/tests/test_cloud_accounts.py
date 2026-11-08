import uuid
import pytest
from sqlalchemy import text
from app.core.security import decrypt_credential
from app.models.cloud_account import CloudAccount
from app.providers.aws import AWSProvider


# ==============================================================================
# 1. CLOUD ACCOUNT CRUD TESTS
# ==============================================================================

def test_create_cloud_account_success(client, test_user, test_workspace, db):
    """Verify owner can connect an AWS cloud account to a workspace."""
    payload = {
        "workspace_id": str(test_workspace),
        "name": "AWS Production Account",
        "provider": "aws",
        "account_id": "123456789012",
        "default_region": "us-east-1",
        "role_arn": "arn:aws:iam::123456789012:role/CloudPilotAccessRole",
        "external_id": "unique-external-id-999",
        "description": "Production compute and storage account"
    }
    response = client.post("/api/v1/cloud/accounts", json=payload, headers=test_user["headers"])
    assert response.status_code == 201, response.text
    data = response.json()
    assert data["name"] == payload["name"]
    assert data["provider"] == "aws"
    assert data["workspace_id"] == str(test_workspace)
    assert data["is_active"] is True
    assert "id" in data


def test_get_cloud_account_by_id(client, test_user, test_workspace):
    """Verify retrieval of a specific cloud account by UUID."""
    create_payload = {
        "workspace_id": str(test_workspace),
        "name": "AWS Staging",
        "provider": "aws",
        "account_id": "987654321098",
        "default_region": "us-west-2",
        "role_arn": "arn:aws:iam::987654321098:role/StagingRole",
        "external_id": "ext-staging-123"
    }
    create_resp = client.post("/api/v1/cloud/accounts", json=create_payload, headers=test_user["headers"])
    account_id = create_resp.json()["id"]

    get_resp = client.get(f"/api/v1/cloud/accounts/{account_id}", headers=test_user["headers"])
    assert get_resp.status_code == 200
    account_data = get_resp.json()
    assert account_data["id"] == account_id
    assert account_data["name"] == "AWS Staging"


def test_list_cloud_accounts_with_pagination(client, test_user, test_workspace):
    """Verify listing accounts returns paginated results matching the workspace."""
    for i in range(3):
        client.post(
            "/api/v1/cloud/accounts",
            json={
                "workspace_id": str(test_workspace),
                "name": f"Account-{i}",
                "provider": "aws",
                "default_region": "us-east-1",
                "role_arn": f"arn:aws:iam::11112222333{i}:role/Role-{i}"
            },
            headers=test_user["headers"]
        )

    list_resp = client.get(
        f"/api/v1/cloud/accounts?workspace_id={test_workspace}&page=1&limit=2",
        headers=test_user["headers"]
    )
    assert list_resp.status_code == 200
    data = list_resp.json()
    assert data["total"] == 3
    assert len(data["items"]) == 2
    assert data["page"] == 1
    assert data["limit"] == 2


def test_update_cloud_account(client, test_user, test_workspace):
    """Verify updating cloud account name, region, or description."""
    create_resp = client.post(
        "/api/v1/cloud/accounts",
        json={
            "workspace_id": str(test_workspace),
            "name": "Initial Name",
            "provider": "aws",
            "default_region": "us-east-1",
            "role_arn": "arn:aws:iam::123456789012:role/InitialRole"
        },
        headers=test_user["headers"]
    )
    account_id = create_resp.json()["id"]

    update_resp = client.put(
        f"/api/v1/cloud/accounts/{account_id}",
        json={"name": "Updated Production Name", "default_region": "eu-central-1"},
        headers=test_user["headers"]
    )
    assert update_resp.status_code == 200
    assert update_resp.json()["name"] == "Updated Production Name"
    assert update_resp.json()["default_region"] == "eu-central-1"


def test_soft_delete_cloud_account(client, test_user, test_workspace, db):
    """Verify deleting a cloud account sets is_active=False in DB and removes from listing."""
    create_resp = client.post(
        "/api/v1/cloud/accounts",
        json={
            "workspace_id": str(test_workspace),
            "name": "Ephemeral Account",
            "provider": "aws",
            "default_region": "us-east-1",
            "role_arn": "arn:aws:iam::123456789012:role/EphemeralRole"
        },
        headers=test_user["headers"]
    )
    account_id = create_resp.json()["id"]

    del_resp = client.delete(f"/api/v1/cloud/accounts/{account_id}", headers=test_user["headers"])
    assert del_resp.status_code == 200

    # Verify directly in DB: row persists with is_active = False
    row = db.execute(
        text("SELECT is_active FROM cloud_accounts WHERE id = :id"),
        {"id": account_id}
    ).fetchone()
    assert row is not None
    assert row[0] is False

    # Verify active listing does not return the deleted account
    list_resp = client.get(
        f"/api/v1/cloud/accounts?workspace_id={test_workspace}",
        headers=test_user["headers"]
    )
    active_ids = [item["id"] for item in list_resp.json()["items"]]
    assert account_id not in active_ids


# ==============================================================================
# 2. CREDENTIAL ENCRYPTION & SECURITY TESTS
# ==============================================================================

def test_credential_encryption_fernet_storage(client, test_user, test_workspace, db):
    """Verify role_arn and external_id are stored as Fernet encrypted ciphertext (gAAAAA...)."""
    plain_role_arn = "arn:aws:iam::555666777888:role/ProductionSecurityRole"
    plain_external_id = "super-confidential-external-token"

    create_resp = client.post(
        "/api/v1/cloud/accounts",
        json={
            "workspace_id": str(test_workspace),
            "name": "Secured Account",
            "provider": "aws",
            "role_arn": plain_role_arn,
            "external_id": plain_external_id
        },
        headers=test_user["headers"]
    )
    account_id = create_resp.json()["id"]

    # Inspect the raw stored column values directly from database
    row = db.execute(
        text("SELECT role_arn, external_id FROM cloud_accounts WHERE id = :id"),
        {"id": account_id}
    ).fetchone()

    db_role_arn, db_external_id = row[0], row[1]

    # Stored values MUST NOT be plaintext
    assert db_role_arn != plain_role_arn
    assert db_external_id != plain_external_id

    # Stored values MUST be valid Fernet ciphertext tokens (starts with gAAAAA)
    assert db_role_arn.startswith("gAAAAA")
    assert db_external_id.startswith("gAAAAA")

    # Internal decryption MUST recover the original plaintext
    decrypted_arn = decrypt_credential(db_role_arn)
    decrypted_ext_id = decrypt_credential(db_external_id)
    assert decrypted_arn == plain_role_arn
    assert decrypted_ext_id == plain_external_id


def test_credential_masking_in_api_response(client, test_user, test_workspace):
    """Verify sensitive credential fields (role_arn, external_id) are completely omitted from API JSON."""
    create_resp = client.post(
        "/api/v1/cloud/accounts",
        json={
            "workspace_id": str(test_workspace),
            "name": "Masked Credential Account",
            "provider": "aws",
            "role_arn": "arn:aws:iam::123456789012:role/SecretRole",
            "external_id": "secret-external-id"
        },
        headers=test_user["headers"]
    )
    data = create_resp.json()
    assert "role_arn" not in data
    assert "external_id" not in data

    # Verify GET endpoint also omits sensitive fields
    get_resp = client.get(f"/api/v1/cloud/accounts/{data['id']}", headers=test_user["headers"])
    get_data = get_resp.json()
    assert "role_arn" not in get_data
    assert "external_id" not in get_data


# ==============================================================================
# 3. WORKSPACE AUTHORIZATION & MULTI-TENANCY TESTS
# ==============================================================================

def test_unauthorized_user_cannot_access_workspace_accounts(client, other_user, test_workspace):
    """Verify user without workspace membership receives 403 Forbidden."""
    # Attempt to list accounts for test_workspace
    list_resp = client.get(
        f"/api/v1/cloud/accounts?workspace_id={test_workspace}",
        headers=other_user["headers"]
    )
    assert list_resp.status_code == 403
    assert "do not have access" in list_resp.json()["detail"].lower()

    # Attempt to create an account in test_workspace
    create_resp = client.post(
        "/api/v1/cloud/accounts",
        json={
            "workspace_id": str(test_workspace),
            "name": "Intruder Account",
            "provider": "aws",
            "role_arn": "arn:aws:iam::123456789012:role/IntruderRole"
        },
        headers=other_user["headers"]
    )
    assert create_resp.status_code == 403


def test_read_only_role_cannot_create_account(client, other_user, test_workspace, db):
    """Verify member with 'viewer' role can read but cannot create cloud accounts (403 Forbidden)."""
    # Grant other_user 'viewer' role in test_workspace
    db.execute(
        text("INSERT INTO workspace_members (workspace_id, user_id, role) VALUES (:ws_id, :uid, 'viewer')"),
        {"ws_id": test_workspace, "uid": other_user["id"]}
    )
    db.commit()

    # Viewer can read accounts list
    list_resp = client.get(
        f"/api/v1/cloud/accounts?workspace_id={test_workspace}",
        headers=other_user["headers"]
    )
    assert list_resp.status_code == 200

    # Viewer CANNOT create accounts (requires write role: owner or admin)
    create_resp = client.post(
        "/api/v1/cloud/accounts",
        json={
            "workspace_id": str(test_workspace),
            "name": "Viewer Forbidden Account",
            "provider": "aws",
            "role_arn": "arn:aws:iam::123456789012:role/ViewerRole"
        },
        headers=other_user["headers"]
    )
    assert create_resp.status_code == 403
    assert "does not permit this operation" in create_resp.json()["detail"].lower()


def test_admin_role_can_create_and_manage_accounts(client, other_user, test_workspace, db):
    """Verify member with 'admin' role has write access to create cloud accounts."""
    db.execute(
        text("INSERT INTO workspace_members (workspace_id, user_id, role) VALUES (:ws_id, :uid, 'admin')"),
        {"ws_id": test_workspace, "uid": other_user["id"]}
    )
    db.commit()

    create_resp = client.post(
        "/api/v1/cloud/accounts",
        json={
            "workspace_id": str(test_workspace),
            "name": "Admin Created Account",
            "provider": "aws",
            "default_region": "us-east-1",
            "role_arn": "arn:aws:iam::123456789012:role/AdminManagedRole"
        },
        headers=other_user["headers"]
    )
    assert create_resp.status_code == 201


# ==============================================================================
# 4. AWS PROVIDER ABSTRACTION TESTS
# ==============================================================================

def test_aws_provider_valid_role_arn_format():
    """Verify standard AWS 12-digit IAM Role ARN format validation."""
    valid_arn = "arn:aws:iam::123456789012:role/CloudPilotAccessRole"
    provider = AWSProvider(role_arn=valid_arn)
    assert provider.validate_role_arn_format() is True


def test_aws_provider_invalid_role_arn_format():
    """Verify malformed or invalid ARNs are rejected by provider."""
    invalid_arns = [
        "not-an-arn",
        "arn:aws:s3:::my-bucket",
        "arn:aws:ec2:us-east-1:123456789012:instance/i-1234",
        ""
    ]
    for arn in invalid_arns:
        provider = AWSProvider(role_arn=arn)
        assert provider.validate_role_arn_format() is False


def test_aws_provider_account_id_extraction():
    """Verify AWS 12-digit account ID is extracted correctly from Role ARN."""
    role_arn = "arn:aws:iam::987654321098:role/EngineeringServiceRole"
    provider = AWSProvider(role_arn=role_arn)
    info = provider.get_account_info()
    assert info["account_id"] == "987654321098"
    assert info["provider"] == "aws"


def test_aws_provider_safe_credential_validation():
    """Verify credentials validation runs safely without crashing when offline."""
    valid_arn = "arn:aws:iam::123456789012:role/SafeValidationRole"
    provider = AWSProvider(role_arn=valid_arn, region="us-east-1")
    # Returns True via graceful fallback / offline structure check
    assert provider.validate_credentials() is True
