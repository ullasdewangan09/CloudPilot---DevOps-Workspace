from sqlalchemy import create_engine
from sqlalchemy import text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.orm import declarative_base

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
    from app.models.user import User
    from app.models.workspace import Workspace, WorkspaceMember, WorkspaceAuditLog

    with engine.begin() as connection:
        # Drop old workspaces and dependent tables to apply updated schemas
        connection.execute(text("DROP TABLE IF EXISTS workspace_members CASCADE;"))
        connection.execute(text("DROP TABLE IF EXISTS workspace_audit_logs CASCADE;"))
        connection.execute(text("DROP TABLE IF EXISTS workspaces CASCADE;"))

    Base.metadata.create_all(
        bind=engine,
        tables=[
            User.__table__,
            Workspace.__table__,
            WorkspaceMember.__table__,
            WorkspaceAuditLog.__table__
        ]
    )

    with engine.begin() as connection:
        connection.execute(
            text("ALTER TABLE users ADD COLUMN IF NOT EXISTS provider VARCHAR(50) DEFAULT 'local'")
        )
        connection.execute(
            text("ALTER TABLE users ADD COLUMN IF NOT EXISTS provider_id VARCHAR(255)")
        )
        connection.execute(
            text("ALTER TABLE users ADD COLUMN IF NOT EXISTS avatar_url VARCHAR(500)")
        )
        connection.execute(
            text("ALTER TABLE users ALTER COLUMN password_hash DROP NOT NULL")
        )

