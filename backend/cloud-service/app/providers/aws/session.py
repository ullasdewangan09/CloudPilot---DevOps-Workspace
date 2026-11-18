"""
AWS STS AssumeRole Ephemeral Credential Session Manager — Phase 3B.1

Responsibilities:
- Accepts cloud account credentials (Role ARN & External ID).
- Decrypts credentials in memory only using Fernet utilities.
- Calls AWS STS AssumeRole with unique External ID and CloudPilot session name.
- Verifies returned CallerIdentity matches the expected AWS Account ID.
- Provides ephemeral boto3 service clients without persisting credentials.
- Zero credential logging, zero database persistence of temporary tokens.
- Safe domain-level error handling without secret leakage.
"""

import re
import time
from datetime import datetime, timezone, timedelta
from typing import Dict, Any, Optional

try:
    import boto3
    from botocore.config import Config as BotoConfig
    from botocore.exceptions import (
        ClientError,
        BotoCoreError,
        EndpointConnectionError,
        ConnectTimeoutError,
        ReadTimeoutError,
        NoCredentialsError,
    )
    BOTO3_AVAILABLE = True
except ImportError:  # pragma: no cover
    BOTO3_AVAILABLE = False
    ClientError = Exception
    BotoCoreError = Exception
    EndpointConnectionError = Exception
    ConnectTimeoutError = Exception
    ReadTimeoutError = Exception
    NoCredentialsError = Exception
    BotoConfig = None

from app.core.security import decrypt_credential

# Standard AWS IAM Role ARN pattern: arn:aws:iam::<12-digit account>:role/<role-name>
AWS_ROLE_ARN_REGEX = re.compile(r"^arn:aws:iam::\d{12}:role/[\w+=,.@-]{1,512}$")
AWS_SESSION_NAME_REGEX = re.compile(r"^[\w+=,.@-]{2,64}$")


# ── Domain Exceptions ────────────────────────────────────────────────────────

class AWSSessionError(Exception):
    """Base exception for AWS session and STS errors."""
    def __init__(self, message: str, code: str = "AWS_ERROR"):
        super().__init__(message)
        self.message = message
        self.code = code


class AWSAuthError(AWSSessionError):
    """Authentication or authorization failures (AccessDenied, InvalidClientTokenId, ExpiredToken)."""
    pass


class AWSValidationError(AWSSessionError):
    """Input validation failures (invalid ARN, missing external ID, malformed policy)."""
    pass


class AWSAccountMismatchError(AWSSessionError):
    """Assumed role account ID does not match expected CloudPilot account ID."""
    pass


class AWSConnectionError(AWSSessionError):
    """Network connection failures, endpoint unreachable, or STS service unavailable."""
    pass


class AWSThrottlingError(AWSSessionError):
    """AWS rate limit or API throttling errors (Throttling, RequestLimitExceeded)."""
    pass


class AWSDiscoveryError(AWSSessionError):
    """Resource discovery execution failures."""
    def __init__(
        self,
        message: str,
        code: str = "DISCOVERY_ERROR",
        service: Optional[str] = None,
        region: Optional[str] = None
    ):
        super().__init__(message, code=code)
        self.service = service
        self.region = region


def handle_aws_client_error(
    exc: ClientError,
    service_name: str,
    operation_name: str,
    region: Optional[str] = None
) -> None:
    """Parse AWS ClientError during discovery into safe domain exceptions without leaking secrets."""
    error_info = exc.response.get("Error", {}) if hasattr(exc, "response") else {}
    error_code = error_info.get("Code", "UnknownError")
    error_msg = error_info.get("Message", "AWS request failed.")
    reg_str = f" in region {region}" if region else ""

    if error_code in ("AccessDenied", "UnauthorizedOperation", "AccessDeniedException"):
        raise AWSAuthError(
            f"Access denied for AWS {service_name}:{operation_name}{reg_str}. Verify IAM permissions.",
            code=error_code
        ) from exc
    elif error_code in ("ExpiredToken", "RequestExpired", "InvalidClientTokenId"):
        raise AWSAuthError(
            f"AWS security token expired or invalid during {service_name}:{operation_name}.",
            code=error_code
        ) from exc
    elif error_code in ("Throttling", "ThrottlingException", "RequestLimitExceeded", "PriorRequestNotComplete"):
        raise AWSThrottlingError(
            f"AWS rate limit exceeded for {service_name}:{operation_name}{reg_str}.",
            code=error_code
        ) from exc
    else:
        raise AWSDiscoveryError(
            f"AWS {service_name} error ({error_code}) during {operation_name}{reg_str}: {error_msg}",
            code=error_code,
            service=service_name,
            region=region
        ) from exc


# ── AWSSessionManager ────────────────────────────────────────────────────────

class AWSSessionManager:
    """
    Manages cross-account AWS STS AssumeRole temporary credential lifecycle.
    - Decrypts Role ARN & External ID in memory only via Fernet.
    - Assumes the cross-account role with unique External ID.
    - Verifies returned CallerIdentity against expected AWS Account ID.
    - Emits ephemeral boto3 service clients without persisting credentials.
    - Never logs or serializes secret keys or tokens.
    """

    DEFAULT_DURATION_SECONDS = 1800  # 30 minutes (AWS allowed range: 900 - 43200)
    MIN_DURATION_SECONDS = 900       # 15 minutes
    MAX_DURATION_SECONDS = 43200     # 12 hours
    SESSION_NAME_PREFIX = "CloudPilot-Session"

    def __init__(
        self,
        role_arn: Optional[str],
        external_id: Optional[str],
        account_id: Optional[str] = None,
        region: str = "us-east-1",
        duration_seconds: int = DEFAULT_DURATION_SECONDS
    ):
        # In-memory decryption if stored as Fernet ciphertext (starts with gAAAAA)
        self.role_arn: Optional[str] = decrypt_credential(role_arn.strip()) if role_arn else None
        self.external_id: Optional[str] = decrypt_credential(external_id.strip()) if external_id else None
        self.account_id: Optional[str] = account_id.strip() if account_id else None
        self.region: str = region.strip() if region else "us-east-1"
        self.duration_seconds: int = duration_seconds

        # Private in-memory credentials & identity cache (never logged/persisted)
        self._credentials: Optional[Dict[str, Any]] = None
        self._identity: Optional[Dict[str, Any]] = None

        if BOTO3_AVAILABLE and BotoConfig:
            self._boto_config = BotoConfig(
                retries={"max_attempts": 3, "mode": "standard"},
                connect_timeout=10,
                read_timeout=15
            )
        else:
            self._boto_config = None

        self._validate_inputs()

    def _validate_inputs(self) -> None:
        """Validate input parameters before attempting STS operations."""
        if not self.role_arn:
            raise AWSValidationError("Role ARN is required for AWS AssumeRole.", code="MISSING_ROLE_ARN")

        if not self.validate_role_arn_format(self.role_arn):
            raise AWSValidationError(
                f"Invalid Role ARN format: '{self.role_arn}'. Expected format: 'arn:aws:iam::<12-digit-account>:role/<role-name>'.",
                code="INVALID_ROLE_ARN"
            )

        if not self.external_id:
            raise AWSValidationError("External ID is required for cross-account AssumeRole security.", code="MISSING_EXTERNAL_ID")

        if not (self.MIN_DURATION_SECONDS <= self.duration_seconds <= self.MAX_DURATION_SECONDS):
            raise AWSValidationError(
                f"DurationSeconds must be between {self.MIN_DURATION_SECONDS} and {self.MAX_DURATION_SECONDS}.",
                code="INVALID_DURATION"
            )

        # Extract account ID from role ARN if not explicitly provided
        if not self.account_id and ":iam::" in self.role_arn and ":role/" in self.role_arn:
            try:
                self.account_id = self.role_arn.split(":iam::")[1].split(":role/")[0]
            except Exception:
                pass

    @staticmethod
    def validate_role_arn_format(role_arn: Optional[str]) -> bool:
        """Verify role ARN matches standard AWS ARN syntax or development test format."""
        if not role_arn:
            return False
        if AWS_ROLE_ARN_REGEX.match(role_arn):
            return True
        if role_arn.startswith("arn:aws:iam::") and ":role/" in role_arn:
            return True
        return False

    @classmethod
    def from_cloud_account(
        cls,
        account: Any,
        region: Optional[str] = None,
        duration_seconds: int = DEFAULT_DURATION_SECONDS
    ) -> "AWSSessionManager":
        """Instantiate AWSSessionManager from a CloudAccount ORM instance."""
        return cls(
            role_arn=getattr(account, "role_arn", None),
            external_id=getattr(account, "external_id", None),
            account_id=getattr(account, "account_id", None),
            region=region or getattr(account, "default_region", "us-east-1") or "us-east-1",
            duration_seconds=duration_seconds
        )

    def assume_role(self) -> Dict[str, Any]:
        """
        Calls AWS STS AssumeRole, captures ephemeral credentials in memory,
        and verifies returned caller identity matches expected AWS account ID.
        Returns sanitized identity metadata (never credentials).
        """
        if not BOTO3_AVAILABLE:
            raise AWSSessionError("boto3 library is required for AWS operations.", code="MISSING_BOTO3")

        # Construct sanitized session name matching AWS regex ^[\w+=,.@-]{2,64}$
        acct_snippet = self.account_id[:12] if self.account_id else "Acct"
        role_session_name = f"{self.SESSION_NAME_PREFIX}-{acct_snippet}-{int(time.time())}"
        if not AWS_SESSION_NAME_REGEX.match(role_session_name):
            role_session_name = f"CloudPilot-Session-{int(time.time())}"

        try:
            # 1. Base STS client
            sts_client = boto3.client("sts", region_name=self.region, config=self._boto_config)

            # 2. AssumeRole
            response = sts_client.assume_role(
                RoleArn=self.role_arn,
                RoleSessionName=role_session_name,
                ExternalId=self.external_id,
                DurationSeconds=self.duration_seconds
            )

            # 3. Store temporary credentials in memory only
            creds = response["Credentials"]
            self._credentials = {
                "AccessKeyId": creds["AccessKeyId"],
                "SecretAccessKey": creds["SecretAccessKey"],
                "SessionToken": creds["SessionToken"],
                "Expiration": creds["Expiration"]
            }

            # 4. Immediate CallerIdentity verification using assumed role credentials
            assumed_sts = boto3.client(
                "sts",
                region_name=self.region,
                aws_access_key_id=self._credentials["AccessKeyId"],
                aws_secret_access_key=self._credentials["SecretAccessKey"],
                aws_session_token=self._credentials["SessionToken"],
                config=self._boto_config
            )
            identity = assumed_sts.get_caller_identity()

            caller_account = identity.get("Account")
            caller_arn = identity.get("Arn")
            caller_user_id = identity.get("UserId")

            # 5. Verify AWS Account ID match
            if self.account_id and str(caller_account) != str(self.account_id):
                # Invalidate credentials immediately
                self._credentials = None
                self._identity = None
                raise AWSAccountMismatchError(
                    f"Assumed role belongs to AWS account '{caller_account}', "
                    f"which does not match configured CloudPilot account '{self.account_id}'.",
                    code="ACCOUNT_ID_MISMATCH"
                )

            self._identity = {
                "account_id": caller_account,
                "assumed_role_arn": caller_arn,
                "user_id": caller_user_id,
                "verified_at": datetime.now(timezone.utc).isoformat(),
                "status": "active"
            }
            return self._identity

        except ClientError as exc:
            self._handle_client_error(exc)
        except NoCredentialsError as exc:
            raise AWSAuthError("Unable to locate host AWS credentials to perform AssumeRole.", code="NO_HOST_CREDENTIALS") from exc
        except (EndpointConnectionError, ConnectTimeoutError, ReadTimeoutError) as exc:
            raise AWSConnectionError("Unable to connect to AWS STS service. Network timeout or endpoint unreachable.") from exc
        except BotoCoreError as exc:
            raise AWSSessionError(f"AWS BotoCore error: {str(exc)}", code="BOTOCORE_ERROR") from exc

    def _handle_client_error(self, exc: ClientError) -> None:
        """Parse AWS ClientError into safe application-level exception without leaking secrets."""
        error_info = exc.response.get("Error", {}) if hasattr(exc, "response") else {}
        error_code = error_info.get("Code", "UnknownError")
        error_msg = error_info.get("Message", "AWS STS request failed.")

        if error_code in ("AccessDenied", "UnauthorizedOperation"):
            raise AWSAuthError(
                "AWS STS AssumeRole access denied. Verify that the target IAM role trusts CloudPilot "
                "and the External ID is valid.",
                code=error_code
            ) from exc
        elif error_code in ("InvalidClientTokenId", "SignatureDoesNotMatch"):
            raise AWSAuthError(
                "The AWS security token or credentials are invalid.",
                code=error_code
            ) from exc
        elif error_code == "ExpiredToken":
            raise AWSAuthError(
                "The temporary AWS security token has expired.",
                code=error_code
            ) from exc
        elif error_code == "MalformedPolicyDocument":
            raise AWSValidationError(
                "The IAM trust policy document is malformed.",
                code=error_code
            ) from exc
        elif error_code in ("ValidationError", "InvalidParameter", "InvalidParameterValue"):
            raise AWSValidationError(
                f"AWS validation error: {error_msg}",
                code=error_code
            ) from exc
        else:
            raise AWSSessionError(
                f"AWS STS error ({error_code}): {error_msg}",
                code=error_code
            ) from exc

    def is_expired(self) -> bool:
        """Check whether the active in-memory temporary credentials have expired (with 60s safety buffer)."""
        if not self._credentials or "Expiration" not in self._credentials:
            return True
        exp = self._credentials["Expiration"]
        if isinstance(exp, str):
            exp = datetime.fromisoformat(exp.replace("Z", "+00:00"))
        now = datetime.now(timezone.utc)
        if exp.tzinfo is None:
            exp = exp.replace(tzinfo=timezone.utc)
        return now >= (exp - timedelta(seconds=60))

    def get_client(self, service_name: str, region_name: Optional[str] = None):
        """Returns an ephemeral boto3 service client using assumed role credentials."""
        if not BOTO3_AVAILABLE:
            raise AWSSessionError("boto3 library is required for AWS operations.", code="MISSING_BOTO3")

        if self.is_expired():
            self.assume_role()

        return boto3.client(
            service_name,
            region_name=region_name or self.region,
            aws_access_key_id=self._credentials["AccessKeyId"],
            aws_secret_access_key=self._credentials["SecretAccessKey"],
            aws_session_token=self._credentials["SessionToken"],
            config=self._boto_config
        )

    def get_session(self):
        """Returns an ephemeral boto3 Session using assumed role credentials."""
        if not BOTO3_AVAILABLE:
            raise AWSSessionError("boto3 library is required for AWS operations.", code="MISSING_BOTO3")

        if self.is_expired():
            self.assume_role()

        return boto3.Session(
            aws_access_key_id=self._credentials["AccessKeyId"],
            aws_secret_access_key=self._credentials["SecretAccessKey"],
            aws_session_token=self._credentials["SessionToken"],
            region_name=self.region
        )

    def get_sanitized_identity(self) -> Optional[Dict[str, Any]]:
        """Returns caller identity metadata without any sensitive credentials."""
        return self._identity.copy() if self._identity else None

    def __repr__(self) -> str:
        """Sanitized string representation — never prints credentials or tokens."""
        return (
            f"<AWSSessionManager role_arn='{self.role_arn}' "
            f"account_id='{self.account_id}' region='{self.region}' "
            f"has_credentials={bool(self._credentials)}>"
        )

    def __str__(self) -> str:
        return self.__repr__()
