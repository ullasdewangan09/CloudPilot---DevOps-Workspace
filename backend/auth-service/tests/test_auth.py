import datetime
from jose import jwt
import pytest
from app.core.config import JWT_SECRET, JWT_ALGORITHM
from app.models.user import User


# ==============================================================================
# 1. USER REGISTRATION TESTS
# ==============================================================================

def test_register_user_success(client, db):
    """Verify standard user registration succeeds and persists user with hashed password."""
    payload = {
        "email": "newuser@example.com",
        "username": "newuser",
        "password": "Password123!"
    }
    response = client.post("/api/v1/auth/register", json=payload)
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["message"] == "User created successfully"
    assert "user_id" in data

    # Verify directly in test database
    user = db.query(User).filter(User.email == "newuser@example.com").first()
    assert user is not None
    assert user.username == "newuser"
    assert user.password_hash != "Password123!"
    assert user.password_hash.startswith("$2b$")


def test_register_duplicate_email_rejected(client, test_user):
    """Verify registering an existing email returns 400 Bad Request."""
    payload = {
        "email": test_user.email,
        "username": "distinct_user",
        "password": "Password123!"
    }
    response = client.post("/api/v1/auth/register", json=payload)
    assert response.status_code == 400
    assert "already exists" in response.json()["detail"].lower()


def test_register_duplicate_username_rejected(client, test_user):
    """Verify registering an existing username returns 400 Bad Request."""
    payload = {
        "email": "different_email@example.com",
        "username": test_user.username,
        "password": "Password123!"
    }
    response = client.post("/api/v1/auth/register", json=payload)
    assert response.status_code == 400
    assert "already exists" in response.json()["detail"].lower()


def test_register_missing_required_fields(client):
    """Verify missing username, email, or password triggers 422 Unprocessable Entity."""
    response = client.post("/api/v1/auth/register", json={"email": "incomplete@example.com"})
    assert response.status_code == 422


# ==============================================================================
# 2. PASSWORD SECURITY & LOGIN TESTS
# ==============================================================================

def test_password_hashing_stored_securely(test_user, db):
    """Verify passwords are never stored in plaintext and use bcrypt hashing format."""
    user = db.query(User).filter(User.id == test_user.id).first()
    assert user.password_hash != "SecurePassword123!"
    assert user.password_hash.startswith("$2b$") or user.password_hash.startswith("$2a$")
    assert len(user.password_hash) >= 50


def test_login_success(client, test_user):
    """Verify user can authenticate with valid credentials and receive access token."""
    login_data = {
        "email": test_user.email,
        "password": "SecurePassword123!"
    }
    response = client.post("/api/v1/auth/login", json=login_data)
    assert response.status_code == 200, response.text
    data = response.json()
    assert "access_token" in data
    assert data["token_type"] == "bearer"


def test_login_wrong_password_rejected(client, test_user):
    """Verify login with incorrect password returns 401 Unauthorized."""
    login_data = {
        "email": test_user.email,
        "password": "WrongPassword999!"
    }
    response = client.post("/api/v1/auth/login", json=login_data)
    assert response.status_code == 401
    assert "invalid credentials" in response.json()["detail"].lower()


def test_login_nonexistent_user_rejected(client):
    """Verify login with non-existent user returns 401 Unauthorized."""
    login_data = {
        "email": "ghost_user@example.com",
        "password": "AnyPassword123!"
    }
    response = client.post("/api/v1/auth/login", json=login_data)
    assert response.status_code == 401


# ==============================================================================
# 3. JWT & SESSION AUTHORIZATION TESTS
# ==============================================================================

def test_jwt_token_claims_and_structure(test_user, test_user_token):
    """Verify generated JWT contains expected payload claims: sub, role, exp."""
    decoded = jwt.decode(test_user_token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
    assert decoded["sub"] == test_user.email
    assert decoded["role"] == test_user.role
    assert "exp" in decoded
    # Expiration is in the future
    assert decoded["exp"] > datetime.datetime.now(datetime.timezone.utc).timestamp()


def test_me_endpoint_with_valid_token(client, auth_headers, test_user):
    """Verify /api/v1/auth/me returns current user profile with valid Bearer token."""
    response = client.get("/api/v1/auth/me", headers=auth_headers)
    assert response.status_code == 200, response.text
    profile = response.json()
    assert profile["id"] == test_user.id
    assert profile["email"] == test_user.email
    assert profile["username"] == test_user.username
    assert "password_hash" not in profile


def test_me_endpoint_expired_token_rejected(client):
    """Verify expired JWT tokens are rejected with 401 Unauthorized."""
    past_time = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=1)
    expired_payload = {
        "sub": "expired_user@example.com",
        "exp": int(past_time.timestamp())
    }
    expired_token = jwt.encode(expired_payload, JWT_SECRET, algorithm=JWT_ALGORITHM)
    headers = {"Authorization": f"Bearer {expired_token}"}
    response = client.get("/api/v1/auth/me", headers=headers)
    assert response.status_code == 401


def test_me_endpoint_malformed_token_rejected(client):
    """Verify malformed/garbage JWT strings are rejected with 401 Unauthorized."""
    headers = {"Authorization": "Bearer not.a.valid.jwt.token"}
    response = client.get("/api/v1/auth/me", headers=headers)
    assert response.status_code == 401


def test_me_endpoint_missing_token_rejected(client):
    """Verify calling protected endpoints without Authorization header returns 401."""
    response = client.get("/api/v1/auth/me")
    assert response.status_code == 401


# ==============================================================================
# 4. OAUTH & SANDBOX TESTS
# ==============================================================================

def test_oauth_providers_status(client):
    """Verify GET /api/v1/auth/providers returns configuration status for social providers."""
    response = client.get("/api/v1/auth/providers")
    assert response.status_code == 200
    providers = response.json()
    assert "google" in providers
    assert "github" in providers
    assert "microsoft" in providers


def test_oauth_login_redirects_to_sandbox_when_unconfigured(client):
    """Verify unconfigured OAuth provider redirects or responds with mock sandbox URL."""
    response = client.get("/api/v1/auth/microsoft/login", headers={"accept": "application/json"})
    assert response.status_code == 200
    data = response.json()
    assert "/api/v1/auth/mock/login" in data["url"]
    assert "provider=microsoft" in data["url"]


def test_mock_oauth_login_page_renders_html(client):
    """Verify mock OAuth portal renders interactive HTML sandbox."""
    response = client.get("/api/v1/auth/mock/login?provider=microsoft&redirect_uri=http://localhost:8000/api/v1/auth/microsoft/callback")
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    assert "Simulated Microsoft Authentication" in response.text


def test_mock_oauth_callback_generates_token(client, db):
    """Verify mock OAuth callback creates user and redirects with valid JWT."""
    callback_url = (
        "/api/v1/auth/microsoft/callback"
        "?code=mock_code_xyz"
        "&email=mock_oauth_dev@example.com"
        "&username=MockOAuthDev"
    )
    response = client.get(callback_url, follow_redirects=False)
    assert response.status_code in (302, 307)
    redirect_location = response.headers["location"]
    assert "token=" in redirect_location

    # Verify social user in test DB
    user = db.query(User).filter(User.email == "mock_oauth_dev@example.com").first()
    assert user is not None
    assert user.provider == "microsoft"
