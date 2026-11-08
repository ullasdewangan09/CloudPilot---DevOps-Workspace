import os
import sys
from pathlib import Path

# Add service directory to sys.path
SERVICE_ROOT = Path(__file__).resolve().parents[1]
if str(SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(SERVICE_ROOT))

def get_test_db_url():
    if os.getenv("TEST_DATABASE_URL"):
        return os.getenv("TEST_DATABASE_URL")
    # Resolve password from git-ignored local .env if present
    auth_env = SERVICE_ROOT / ".env"
    if auth_env.exists():
        with open(auth_env, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line.startswith("DATABASE_URL="):
                    val = line.split("=", 1)[1].strip().strip('"').strip("'")
                    if "CloudPilot" in val:
                        return val.replace("/CloudPilot", "/cloudpilot_test")
    return "postgresql://postgres:postgres@localhost:5432/cloudpilot_test"

# Set test database URL and testing flag BEFORE any app imports
TEST_DB_URL = get_test_db_url()
os.environ["TEST_DATABASE_URL"] = TEST_DB_URL
os.environ["TESTING"] = "true"

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from fastapi.testclient import TestClient

from app.main import app
from app.core.database import get_db, Base
from app.core.security import hash_password, create_access_token
from app.models.user import User
from app.models.workspace import Workspace, WorkspaceMember, WorkspaceAuditLog

# Safety verification: ensure engine connects STRICTLY to cloudpilot_test
test_engine = create_engine(TEST_DB_URL, pool_pre_ping=True)
with test_engine.connect() as conn:
    current_db = conn.execute(text("SELECT current_database();")).scalar()
    if current_db != "cloudpilot_test":
        raise RuntimeError(
            f"CRITICAL SAFETY VIOLATION: Test suite connected to '{current_db}'! "
            "Must be 'cloudpilot_test'. Aborting all test operations."
        )

TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=test_engine)


@pytest.fixture(scope="session", autouse=True)
def setup_test_database():
    """Ensure all required tables exist in cloudpilot_test before running any tests."""
    with test_engine.connect() as conn:
        assert conn.execute(text("SELECT current_database();")).scalar() == "cloudpilot_test"
    
    Base.metadata.create_all(bind=test_engine)
    with test_engine.connect() as conn:
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS cloud_accounts (
                id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                workspace_id UUID NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
                provider VARCHAR(30) NOT NULL,
                name VARCHAR(100) NOT NULL,
                account_id VARCHAR(255),
                default_region VARCHAR(100),
                role_arn TEXT,
                external_id TEXT,
                status VARCHAR(30) NOT NULL DEFAULT 'pending',
                is_active BOOLEAN NOT NULL DEFAULT TRUE,
                description TEXT,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            );
        """))
        conn.commit()
    yield


@pytest.fixture(autouse=True)
def clean_database():
    """Clean up table records in cloudpilot_test between tests to ensure test isolation."""
    yield
    with test_engine.connect() as conn:
        # Strictly assert DB name before TRUNCATE
        assert conn.execute(text("SELECT current_database();")).scalar() == "cloudpilot_test"
        conn.execute(text("""
            TRUNCATE TABLE workspace_audit_logs, workspace_members, workspaces, users RESTART IDENTITY CASCADE;
        """))
        conn.commit()


@pytest.fixture
def db():
    """Provide an isolated database session for direct model queries in tests."""
    session = TestingSessionLocal()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture
def client(db):
    """Provide a FastAPI TestClient with get_db overridden to use cloudpilot_test."""
    def _override_get_db():
        session = TestingSessionLocal()
        try:
            yield session
        finally:
            session.close()

    app.dependency_overrides[get_db] = _override_get_db
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


@pytest.fixture
def test_user(db):
    """Create a verified test user in cloudpilot_test."""
    user = User(
        email="test_engineer@example.com",
        username="testengineer",
        password_hash=hash_password("SecurePassword123!"),
        role="developer",
        provider="local"
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


@pytest.fixture
def test_user_token(test_user):
    """Generate a valid JWT token for test_user."""
    return create_access_token({"sub": test_user.email, "role": test_user.role})


@pytest.fixture
def auth_headers(test_user_token):
    """Provide authorization headers containing valid bearer token."""
    return {"Authorization": f"Bearer {test_user_token}"}


@pytest.fixture
def second_user(db):
    """Create a second distinct user for multi-tenant and authorization isolation tests."""
    user = User(
        email="other_engineer@example.com",
        username="otherengineer",
        password_hash=hash_password("OtherPassword123!"),
        role="developer",
        provider="local"
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


@pytest.fixture
def second_auth_headers(second_user):
    """Provide authorization headers for the second user."""
    token = create_access_token({"sub": second_user.email, "role": second_user.role})
    return {"Authorization": f"Bearer {token}"}
