import os
import sys
import uuid
from pathlib import Path

SERVICE_ROOT = Path(__file__).resolve().parents[1]
if str(SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(SERVICE_ROOT))

def get_test_db_url():
    if os.getenv("TEST_DATABASE_URL"):
        return os.getenv("TEST_DATABASE_URL")
    # Resolve password from git-ignored local .env if present
    cloud_env = SERVICE_ROOT / ".env"
    if cloud_env.exists():
        with open(cloud_env, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line.startswith("DATABASE_URL="):
                    val = line.split("=", 1)[1].strip().strip('"').strip("'")
                    if "CloudPilot" in val:
                        return val.replace("/CloudPilot", "/cloudpilot_test")
    return "postgresql://postgres:postgres@localhost:5432/cloudpilot_test"

TEST_DB_URL = get_test_db_url()
os.environ["TEST_DATABASE_URL"] = TEST_DB_URL
os.environ["TESTING"] = "true"

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from fastapi.testclient import TestClient

from datetime import datetime, timezone, timedelta
from jose import jwt

from app.main import app
from app.core.database import get_db
from app.core.config import JWT_SECRET, JWT_ALGORITHM

def create_test_token(data: dict) -> str:
    to_encode = data.copy()
    expire = datetime.now(timezone.utc) + timedelta(minutes=60)
    to_encode.update({"exp": expire})
    return jwt.encode(to_encode, JWT_SECRET, algorithm=JWT_ALGORITHM)

test_engine = create_engine(TEST_DB_URL, pool_pre_ping=True)
with test_engine.connect() as conn:
    current_db = conn.execute(text("SELECT current_database();")).scalar()
    if current_db != "cloudpilot_test":
        raise RuntimeError(
            f"CRITICAL SAFETY VIOLATION: Test suite connected to '{current_db}'! "
            "Must be 'cloudpilot_test'. Aborting all test operations."
        )

TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=test_engine)


@pytest.fixture(autouse=True)
def clean_database():
    """Ensure cloud_accounts and reference tables are cleaned up between tests in cloudpilot_test."""
    yield
    with test_engine.connect() as conn:
        assert conn.execute(text("SELECT current_database();")).scalar() == "cloudpilot_test"
        conn.execute(text("""
            TRUNCATE TABLE cloud_accounts, workspace_audit_logs, workspace_members, workspaces, users RESTART IDENTITY CASCADE;
        """))
        conn.commit()


@pytest.fixture
def db():
    """Provide isolated database session for direct table queries."""
    session = TestingSessionLocal()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture
def client():
    """Provide FastAPI TestClient with get_db dependency overridden to cloudpilot_test."""
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
    """Seed a test user in users table and return (user_id, token, auth_headers)."""
    with test_engine.begin() as conn:
        res = conn.execute(text("""
            INSERT INTO users (username, email, password_hash, role, provider)
            VALUES ('cloud_engineer', 'cloud_engineer@example.com', 'dummyhash', 'developer', 'local')
            RETURNING id;
        """))
        user_id = res.scalar()

    token = create_test_token({"sub": "cloud_engineer@example.com", "role": "developer"})
    return {
        "id": user_id,
        "email": "cloud_engineer@example.com",
        "token": token,
        "headers": {"Authorization": f"Bearer {token}"}
    }


@pytest.fixture
def other_user(db):
    """Seed another distinct user in users table and return user credentials."""
    with test_engine.begin() as conn:
        res = conn.execute(text("""
            INSERT INTO users (username, email, password_hash, role, provider)
            VALUES ('other_engineer', 'other_engineer@example.com', 'dummyhash', 'developer', 'local')
            RETURNING id;
        """))
        user_id = res.scalar()

    token = create_test_token({"sub": "other_engineer@example.com", "role": "developer"})
    return {
        "id": user_id,
        "email": "other_engineer@example.com",
        "token": token,
        "headers": {"Authorization": f"Bearer {token}"}
    }


@pytest.fixture
def test_workspace(test_user):
    """Seed an active workspace owned by test_user with owner membership."""
    ws_id = uuid.uuid4()
    with test_engine.begin() as conn:
        conn.execute(
            text("""
                INSERT INTO workspaces (id, owner_id, name, environment, is_active)
                VALUES (:id, :owner_id, 'Cloud Core Workspace', 'production', TRUE);
            """),
            {"id": ws_id, "owner_id": test_user["id"]}
        )
        conn.execute(
            text("""
                INSERT INTO workspace_members (workspace_id, user_id, role)
                VALUES (:workspace_id, :user_id, 'owner');
            """),
            {"workspace_id": ws_id, "user_id": test_user["id"]}
        )
    return ws_id
