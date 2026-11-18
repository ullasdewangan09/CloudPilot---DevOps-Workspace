"""
AWS STS AssumeRole Integration Tests — Phase 3B.1 Automated Test Suite.

Verifies:
1. Successful AssumeRole with ephemeral temporary credentials.
2. Successful GetCallerIdentity verification.
3. Target AWS Account ID matches expected CloudPilot account ID.
4. Target AWS Account ID mismatch is rejected (AWSAccountMismatchError).
5. AccessDenied error handling without credential leakage (AWSAuthError).
6. Invalid Role ARN format rejection (AWSValidationError).
7. Missing External ID rejection (AWSValidationError).
8. Temporary credentials expiration detection (is_expired).
9. AWS network timeout / connection errors (AWSConnectionError).
10. Missing boto3 library error handling (AWSSessionError).
11. Zero temporary credentials written to PostgreSQL database.
12. Zero temporary credentials exposed in logs, API outputs, or string representations.
"""

import logging
import uuid
from datetime import datetime, timezone, timedelta
from unittest.mock import patch, MagicMock

import pytest
from botocore.exceptions import (
    ClientError,
    EndpointConnectionError,
    ConnectTimeoutError,
    ReadTimeoutError,
)
from sqlalchemy import text

from app.models.cloud_account import CloudAccount
from app.providers.aws.session import (
    AWSSessionManager,
    AWSSessionError,
    AWSAuthError,
    AWSValidationError,
    AWSAccountMismatchError,
    AWSConnectionError,
)
from app.core.security import encrypt_credential


# ── Sample Test Fixtures / Mock Helpers ─────────────────────────────────────

MOCK_ROLE_ARN = "arn:aws:iam::123456789012:role/CloudPilotDiscoveryRole"
MOCK_EXTERNAL_ID = "cloudpilot-secure-external-id-456"
MOCK_ACCOUNT_ID = "123456789012"
MOCK_REGION = "us-east-1"

MOCK_ACCESS_KEY_ID = "ASIAIOSFODNN7EXAMPLE"
MOCK_SECRET_ACCESS_KEY = "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"
MOCK_SESSION_TOKEN = "AQoDYXdzEJr1EXAMPLEq837nd83n483EXAMPLE=="


def create_mock_sts_client(caller_account: str = MOCK_ACCOUNT_ID):
    """Generate a mock boto3 STS client returning valid AssumeRole and CallerIdentity payloads."""
    mock_sts = MagicMock()
    mock_sts.assume_role.return_value = {
        "Credentials": {
            "AccessKeyId": MOCK_ACCESS_KEY_ID,
            "SecretAccessKey": MOCK_SECRET_ACCESS_KEY,
            "SessionToken": MOCK_SESSION_TOKEN,
            "Expiration": datetime.now(timezone.utc) + timedelta(seconds=1800)
        }
    }
    mock_sts.get_caller_identity.return_value = {
        "Account": caller_account,
        "Arn": f"arn:aws:sts::{caller_account}:assumed-role/CloudPilotDiscoveryRole/CloudPilot-Session",
        "UserId": "AROA1234567890EXAMPLE:CloudPilot-Session"
    }
    return mock_sts


# ==============================================================================
# 1. SUCCESSFUL ASSUMEROLE & STS CALLER IDENTITY
# ==============================================================================

@patch("boto3.client")
def test_assume_role_success(mock_boto_client):
    """Verify AssumeRole succeeds with expected parameters and returns sanitized identity."""
    mock_sts = create_mock_sts_client()
    mock_boto_client.return_value = mock_sts

    manager = AWSSessionManager(
        role_arn=MOCK_ROLE_ARN,
        external_id=MOCK_EXTERNAL_ID,
        account_id=MOCK_ACCOUNT_ID,
        region=MOCK_REGION,
        duration_seconds=1800
    )

    identity = manager.assume_role()

    # 1. Verify STS AssumeRole call arguments
    mock_sts.assume_role.assert_called_once()
    call_kwargs = mock_sts.assume_role.call_args[1]
    assert call_kwargs["RoleArn"] == MOCK_ROLE_ARN
    assert call_kwargs["ExternalId"] == MOCK_EXTERNAL_ID
    assert call_kwargs["DurationSeconds"] == 1800
    assert call_kwargs["RoleSessionName"].startswith("CloudPilot-Session-")

    # 2. Verify returned sanitized identity
    assert identity["account_id"] == MOCK_ACCOUNT_ID
    assert identity["status"] == "active"
    assert "assumed_role_arn" in identity
    assert manager.is_expired() is False


@patch("boto3.client")
def test_get_caller_identity_success(mock_boto_client):
    """Verify CallerIdentity extracts Account, Arn, and UserId without leaking credentials."""
    mock_sts = create_mock_sts_client()
    mock_boto_client.return_value = mock_sts

    manager = AWSSessionManager(
        role_arn=MOCK_ROLE_ARN,
        external_id=MOCK_EXTERNAL_ID,
        account_id=MOCK_ACCOUNT_ID
    )

    manager.assume_role()
    identity = manager.get_sanitized_identity()

    assert identity is not None
    assert identity["account_id"] == MOCK_ACCOUNT_ID
    assert "CloudPilotDiscoveryRole" in identity["assumed_role_arn"]
    assert identity["user_id"] == "AROA1234567890EXAMPLE:CloudPilot-Session"

    # Credentials MUST NEVER be in identity dict
    assert "AccessKeyId" not in identity
    assert "SecretAccessKey" not in identity
    assert "SessionToken" not in identity


# ==============================================================================
# 2. AWS ACCOUNT ID VALIDATION & MISMATCH REJECTION
# ==============================================================================

@patch("boto3.client")
def test_account_id_matches_expected(mock_boto_client):
    """Verify assume_role succeeds when caller account matches configured account ID."""
    mock_sts = create_mock_sts_client(caller_account="123456789012")
    mock_boto_client.return_value = mock_sts

    manager = AWSSessionManager(
        role_arn="arn:aws:iam::123456789012:role/MatchingRole",
        external_id="ext-matching-id",
        account_id="123456789012"
    )
    result = manager.assume_role()
    assert result["account_id"] == "123456789012"


@patch("boto3.client")
def test_account_id_mismatch_rejected(mock_boto_client):
    """Verify assume_role raises AWSAccountMismatchError and purges in-memory credentials if account IDs differ."""
    # STS returns different AWS account ID than expected
    mock_sts = create_mock_sts_client(caller_account="999888777666")
    mock_boto_client.return_value = mock_sts

    manager = AWSSessionManager(
        role_arn="arn:aws:iam::123456789012:role/TargetRole",
        external_id="ext-secret-id",
        account_id="123456789012"
    )

    with pytest.raises(AWSAccountMismatchError) as exc_info:
        manager.assume_role()

    assert exc_info.value.code == "ACCOUNT_ID_MISMATCH"
    assert "999888777666" in str(exc_info.value)
    assert "123456789012" in str(exc_info.value)

    # Ephemeral credentials MUST be immediately wiped
    assert manager._credentials is None
    assert manager.get_sanitized_identity() is None


# ==============================================================================
# 3. ERROR HANDLING & SECURITY SAFEGUARDS
# ==============================================================================

@patch("boto3.client")
def test_assume_role_access_denied(mock_boto_client):
    """Verify ClientError AccessDenied is mapped to AWSAuthError without revealing internal secrets."""
    mock_sts = MagicMock()
    mock_boto_client.return_value = mock_sts

    error_response = {
        "Error": {
            "Code": "AccessDenied",
            "Message": "User arn:aws:iam::000:root is not authorized to perform sts:AssumeRole"
        }
    }
    mock_sts.assume_role.side_effect = ClientError(error_response, "AssumeRole")

    manager = AWSSessionManager(
        role_arn=MOCK_ROLE_ARN,
        external_id=MOCK_EXTERNAL_ID
    )

    with pytest.raises(AWSAuthError) as exc_info:
        manager.assume_role()

    assert exc_info.value.code == "AccessDenied"
    assert "access denied" in str(exc_info.value).lower()
    # Secrets or raw sensitive strings should not leak
    assert MOCK_EXTERNAL_ID not in str(exc_info.value)


def test_invalid_role_arn_rejected():
    """Verify malformed role ARNs are immediately rejected with AWSValidationError."""
    invalid_arns = [
        "not-an-arn",
        "arn:aws:s3:::bucket-name",
        "arn:aws:ec2:us-east-1:123456789012:instance/i-12345",
        "",
        None,
    ]
    for bad_arn in invalid_arns:
        with pytest.raises(AWSValidationError) as exc_info:
            AWSSessionManager(role_arn=bad_arn, external_id=MOCK_EXTERNAL_ID)
        assert exc_info.value.code in ("MISSING_ROLE_ARN", "INVALID_ROLE_ARN")


def test_missing_external_id_rejected():
    """Verify missing or empty external ID is rejected with AWSValidationError."""
    for empty_id in [None, "", "   "]:
        with pytest.raises(AWSValidationError) as exc_info:
            AWSSessionManager(role_arn=MOCK_ROLE_ARN, external_id=empty_id)
        assert exc_info.value.code == "MISSING_EXTERNAL_ID"


def test_invalid_duration_rejected():
    """Verify session duration outside AWS boundaries (900s - 43200s) is rejected."""
    for bad_dur in [500, 899, 43201, 86400]:
        with pytest.raises(AWSValidationError) as exc_info:
            AWSSessionManager(role_arn=MOCK_ROLE_ARN, external_id=MOCK_EXTERNAL_ID, duration_seconds=bad_dur)
        assert exc_info.value.code == "INVALID_DURATION"


# ==============================================================================
# 4. CREDENTIAL EXPIRATION & CLIENT FACTORY
# ==============================================================================

def test_expired_temporary_credentials():
    """Verify is_expired detects expired tokens and accounts for 60s buffer."""
    manager = AWSSessionManager(role_arn=MOCK_ROLE_ARN, external_id=MOCK_EXTERNAL_ID)

    # Initial state: no credentials loaded -> expired
    assert manager.is_expired() is True

    # Token expiring in 30 seconds -> within 60-second safety buffer -> treated as expired
    manager._credentials = {
        "AccessKeyId": MOCK_ACCESS_KEY_ID,
        "SecretAccessKey": MOCK_SECRET_ACCESS_KEY,
        "SessionToken": MOCK_SESSION_TOKEN,
        "Expiration": datetime.now(timezone.utc) + timedelta(seconds=30)
    }
    assert manager.is_expired() is True

    # Token expiring in 10 minutes -> not expired
    manager._credentials["Expiration"] = datetime.now(timezone.utc) + timedelta(minutes=10)
    assert manager.is_expired() is False


@patch("boto3.client")
def test_aws_network_errors(mock_boto_client):
    """Verify network connection and timeout errors raise AWSConnectionError."""
    mock_sts = MagicMock()
    mock_boto_client.return_value = mock_sts

    for net_err in [
        EndpointConnectionError(endpoint_url="https://sts.us-east-1.amazonaws.com"),
        ConnectTimeoutError(endpoint_url="https://sts.us-east-1.amazonaws.com"),
        ReadTimeoutError(endpoint_url="https://sts.us-east-1.amazonaws.com")
    ]:
        mock_sts.assume_role.side_effect = net_err
        manager = AWSSessionManager(role_arn=MOCK_ROLE_ARN, external_id=MOCK_EXTERNAL_ID)
        with pytest.raises(AWSConnectionError):
            manager.assume_role()


def test_missing_boto3_handling():
    """Verify AWSSessionError is raised when boto3 is not installed/available."""
    with patch("app.providers.aws.session.BOTO3_AVAILABLE", False):
        manager = AWSSessionManager(role_arn=MOCK_ROLE_ARN, external_id=MOCK_EXTERNAL_ID)
        with pytest.raises(AWSSessionError) as exc_info:
            manager.assume_role()
        assert exc_info.value.code == "MISSING_BOTO3"


# ==============================================================================
# 5. ZERO PERSISTENCE & LEAKAGE VERIFICATION
# ==============================================================================

def test_no_temporary_credentials_written_to_postgresql(db, test_workspace):
    """Verify temporary credentials (AccessKeyId, SecretAccessKey, SessionToken) are NEVER stored in PostgreSQL."""
    # 1. Inspect table columns in PostgreSQL
    columns = [row[0] for row in db.execute(text("""
        SELECT column_name
        FROM information_schema.columns
        WHERE table_name = 'cloud_accounts';
    """)).fetchall()]

    assert "access_key_id" not in columns
    assert "secret_access_key" not in columns
    assert "session_token" not in columns
    assert "temporary_credentials" not in columns

    # 2. Create account using Fernet encrypted role_arn and external_id
    plain_arn = "arn:aws:iam::123456789012:role/ProdRole"
    plain_ext = "unique-ext-token-999"

    enc_arn = encrypt_credential(plain_arn)
    enc_ext = encrypt_credential(plain_ext)

    account = CloudAccount(
        workspace_id=test_workspace,
        provider="aws",
        name="Zero Leakage Test",
        account_id="123456789012",
        default_region="us-east-1",
        role_arn=enc_arn,
        external_id=enc_ext,
        status="pending",
        is_active=True
    )
    db.add(account)
    db.commit()

    # 3. Instantiate AWSSessionManager from ORM account
    session_mgr = AWSSessionManager.from_cloud_account(account)
    assert session_mgr.role_arn == plain_arn
    assert session_mgr.external_id == plain_ext

    # 4. Verify DB row content after instantiation
    stored_row = db.execute(
        text("SELECT role_arn, external_id, status FROM cloud_accounts WHERE id = :id"),
        {"id": account.id}
    ).fetchone()

    # Database still holds ONLY Fernet ciphertext (starts with gAAAAA)
    assert stored_row[0].startswith("gAAAAA")
    assert stored_row[1].startswith("gAAAAA")
    # Verify no temporary credentials exist in DB text fields
    for field_val in stored_row:
        if isinstance(field_val, str):
            assert MOCK_ACCESS_KEY_ID not in field_val
            assert MOCK_SECRET_ACCESS_KEY not in field_val
            assert MOCK_SESSION_TOKEN not in field_val


@patch("boto3.client")
def test_no_temporary_credentials_exposed_in_logs_or_repr(mock_boto_client, caplog):
    """Verify string representations and loggers never leak temporary credentials or secret keys."""
    mock_sts = create_mock_sts_client()
    mock_boto_client.return_value = mock_sts

    manager = AWSSessionManager(
        role_arn=MOCK_ROLE_ARN,
        external_id=MOCK_EXTERNAL_ID,
        account_id=MOCK_ACCOUNT_ID
    )
    manager.assume_role()

    # 1. Test __repr__ and __str__
    repr_str = repr(manager)
    str_val = str(manager)

    assert MOCK_ACCESS_KEY_ID not in repr_str
    assert MOCK_SECRET_ACCESS_KEY not in repr_str
    assert MOCK_SESSION_TOKEN not in repr_str
    assert MOCK_EXTERNAL_ID not in repr_str

    assert MOCK_ACCESS_KEY_ID not in str_val
    assert MOCK_SECRET_ACCESS_KEY not in str_val
    assert MOCK_SESSION_TOKEN not in str_val
    assert MOCK_EXTERNAL_ID not in str_val

    # 2. Test Logging
    with caplog.at_level(logging.DEBUG):
        logger = logging.getLogger("cloudpilot.test")
        logger.info("Session Manager initialized: %s", manager)
        logger.debug("Identity: %s", manager.get_sanitized_identity())

        log_text = caplog.text
        assert MOCK_ACCESS_KEY_ID not in log_text
        assert MOCK_SECRET_ACCESS_KEY not in log_text
        assert MOCK_SESSION_TOKEN not in log_text


# ==============================================================================
# 6. EPHEMERAL CLIENT & SESSION GENERATION
# ==============================================================================

@patch("boto3.Session")
@patch("boto3.client")
def test_ephemeral_client_and_session_creation(mock_boto_client, mock_boto_session):
    """Verify get_client and get_session return ephemeral clients with assumed credentials."""
    mock_sts = create_mock_sts_client()
    mock_boto_client.return_value = mock_sts

    manager = AWSSessionManager(
        role_arn=MOCK_ROLE_ARN,
        external_id=MOCK_EXTERNAL_ID,
        account_id=MOCK_ACCOUNT_ID,
        region="us-east-1"
    )

    # 1. get_client for ec2
    manager.get_client("ec2")
    # Verify boto3.client was invoked with ephemeral credentials
    assert mock_boto_client.call_count >= 2  # 1 for sts, 1 for ec2
    ec2_call = mock_boto_client.call_args_list[-1]
    assert ec2_call[0][0] == "ec2"
    assert ec2_call[1]["aws_access_key_id"] == MOCK_ACCESS_KEY_ID
    assert ec2_call[1]["aws_secret_access_key"] == MOCK_SECRET_ACCESS_KEY
    assert ec2_call[1]["aws_session_token"] == MOCK_SESSION_TOKEN

    # 2. get_session
    manager.get_session()
    mock_boto_session.assert_called_once_with(
        aws_access_key_id=MOCK_ACCESS_KEY_ID,
        aws_secret_access_key=MOCK_SECRET_ACCESS_KEY,
        aws_session_token=MOCK_SESSION_TOKEN,
        region_name="us-east-1"
    )

