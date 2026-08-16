from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker, declarative_base

from app.core.config import DATABASE_URL

engine = create_engine(
    DATABASE_URL,
    pool_pre_ping=True
)

SessionLocal = sessionmaker(
    autocommit=False,
    autoflush=False,
    bind=engine
)

Base = declarative_base()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db():
    """
    Create the cloud_accounts table using raw SQL DDL.
    This avoids cross-service SQLAlchemy metadata FK resolution issues
    while still respecting the FK constraint at the database level.
    """
    with engine.begin() as conn:
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS cloud_accounts (
                id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                workspace_id UUID NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
                provider VARCHAR(30) NOT NULL,
                name VARCHAR(100) NOT NULL,
                account_id VARCHAR(255),
                default_region VARCHAR(100),
                role_arn VARCHAR(500),
                external_id VARCHAR(255),
                status VARCHAR(30) NOT NULL DEFAULT 'pending',
                is_active BOOLEAN NOT NULL DEFAULT TRUE,
                description TEXT,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            );
        """))
        # Indexes for fast workspace-scoped queries
        conn.execute(text("""
            CREATE INDEX IF NOT EXISTS ix_cloud_accounts_workspace_id
            ON cloud_accounts (workspace_id);
        """))
        conn.execute(text("""
            CREATE INDEX IF NOT EXISTS ix_cloud_accounts_is_active
            ON cloud_accounts (is_active);
        """))
        conn.execute(text("""
            CREATE INDEX IF NOT EXISTS ix_cloud_accounts_provider
            ON cloud_accounts (provider);
        """))
