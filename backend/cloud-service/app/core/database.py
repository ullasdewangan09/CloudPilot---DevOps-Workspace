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


def init_db(target_engine=None):
    """
    Create the cloud service tables and indexes using raw SQL DDL.
    Creates:
    1. cloud_accounts
    2. cloud_resources
    3. cloud_sync_jobs
    along with compound unique constraints, foreign keys, and performance indexes.
    This avoids cross-service SQLAlchemy metadata FK resolution issues
    while strictly enforcing database-level integrity.
    """
    db_engine = target_engine or engine
    with db_engine.begin() as conn:
        # 1. cloud_accounts table
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

        # 2. cloud_resources table
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS cloud_resources (
                id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                cloud_account_id UUID NOT NULL REFERENCES cloud_accounts(id) ON DELETE CASCADE,
                workspace_id UUID NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
                provider VARCHAR(30) NOT NULL DEFAULT 'aws',
                resource_type VARCHAR(50) NOT NULL,
                provider_resource_id VARCHAR(255) NOT NULL,
                name VARCHAR(255),
                region VARCHAR(100) NOT NULL,
                status VARCHAR(50) NOT NULL DEFAULT 'unknown',
                tags JSONB NOT NULL DEFAULT '{}'::jsonb,
                metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
                is_deleted BOOLEAN NOT NULL DEFAULT FALSE,
                first_discovered_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                last_discovered_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                CONSTRAINT uq_account_resource_type_provider_id
                    UNIQUE (cloud_account_id, resource_type, provider_resource_id)
            );
        """))
        conn.execute(text("""
            CREATE INDEX IF NOT EXISTS ix_cloud_resources_workspace_id
            ON cloud_resources (workspace_id);
        """))
        conn.execute(text("""
            CREATE INDEX IF NOT EXISTS ix_cloud_resources_account_type
            ON cloud_resources (cloud_account_id, resource_type);
        """))
        conn.execute(text("""
            CREATE INDEX IF NOT EXISTS ix_cloud_resources_region
            ON cloud_resources (region);
        """))
        conn.execute(text("""
            CREATE INDEX IF NOT EXISTS ix_cloud_resources_status
            ON cloud_resources (status);
        """))
        conn.execute(text("""
            CREATE INDEX IF NOT EXISTS ix_cloud_resources_is_deleted
            ON cloud_resources (is_deleted);
        """))
        conn.execute(text("""
            CREATE INDEX IF NOT EXISTS ix_cloud_resources_last_discovered
            ON cloud_resources (cloud_account_id, resource_type, last_discovered_at);
        """))
        conn.execute(text("""
            CREATE INDEX IF NOT EXISTS ix_cloud_resources_tags
            ON cloud_resources USING GIN (tags);
        """))

        # 3. cloud_sync_jobs table
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS cloud_sync_jobs (
                id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                cloud_account_id UUID NOT NULL REFERENCES cloud_accounts(id) ON DELETE CASCADE,
                workspace_id UUID NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
                triggered_by_user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
                status VARCHAR(30) NOT NULL DEFAULT 'pending',
                sync_type VARCHAR(30) NOT NULL DEFAULT 'full',
                total_resources INTEGER NOT NULL DEFAULT 0,
                discovered_resources INTEGER NOT NULL DEFAULT 0,
                updated_resources INTEGER NOT NULL DEFAULT 0,
                deleted_resources INTEGER NOT NULL DEFAULT 0,
                summary JSONB NOT NULL DEFAULT '{}'::jsonb,
                errors JSONB NOT NULL DEFAULT '[]'::jsonb,
                worker_id VARCHAR(100),
                attempts INTEGER NOT NULL DEFAULT 0,
                max_attempts INTEGER NOT NULL DEFAULT 3,
                heartbeat_at TIMESTAMPTZ,
                next_retry_at TIMESTAMPTZ,
                started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                completed_at TIMESTAMPTZ,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            );
        """))
        # Phase 3B.7 Idempotent Migration: Add worker and retry columns if table already existed
        conn.execute(text("ALTER TABLE cloud_sync_jobs ADD COLUMN IF NOT EXISTS worker_id VARCHAR(100);"))
        conn.execute(text("ALTER TABLE cloud_sync_jobs ADD COLUMN IF NOT EXISTS attempts INTEGER NOT NULL DEFAULT 0;"))
        conn.execute(text("ALTER TABLE cloud_sync_jobs ADD COLUMN IF NOT EXISTS max_attempts INTEGER NOT NULL DEFAULT 3;"))
        conn.execute(text("ALTER TABLE cloud_sync_jobs ADD COLUMN IF NOT EXISTS heartbeat_at TIMESTAMPTZ;"))
        conn.execute(text("ALTER TABLE cloud_sync_jobs ADD COLUMN IF NOT EXISTS next_retry_at TIMESTAMPTZ;"))

        conn.execute(text("""
            CREATE INDEX IF NOT EXISTS ix_cloud_sync_jobs_workspace_id
            ON cloud_sync_jobs (workspace_id);
        """))
        conn.execute(text("""
            CREATE INDEX IF NOT EXISTS ix_cloud_sync_jobs_account_started
            ON cloud_sync_jobs (cloud_account_id, started_at DESC);
        """))
        conn.execute(text("""
            CREATE INDEX IF NOT EXISTS ix_cloud_sync_jobs_status
            ON cloud_sync_jobs (status);
        """))
        conn.execute(text("""
            CREATE INDEX IF NOT EXISTS ix_cloud_sync_jobs_pending_claim
            ON cloud_sync_jobs (status, next_retry_at)
            WHERE status = 'pending';
        """))
        conn.execute(text("""
            CREATE INDEX IF NOT EXISTS ix_cloud_sync_jobs_running_heartbeat
            ON cloud_sync_jobs (status, heartbeat_at)
            WHERE status = 'running';
        """))
