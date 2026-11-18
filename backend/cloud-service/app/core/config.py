import os
from pathlib import Path
from dotenv import load_dotenv

SERVICE_ROOT = Path(__file__).resolve().parents[2]


def _load_nearest_dotenv(start_path: Path):
    for directory in [start_path, *start_path.parents]:
        env_path = directory / ".env"
        if env_path.exists():
            load_dotenv(env_path)
            break


_load_nearest_dotenv(SERVICE_ROOT)
load_dotenv(SERVICE_ROOT / ".env", override=True)


def _required_env(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(
            f"{name} is not set. Add it to {SERVICE_ROOT / '.env'} "
            "or export it before starting the cloud service."
        )
    return value


DATABASE_URL = os.getenv("TEST_DATABASE_URL") or _required_env("DATABASE_URL")
JWT_SECRET = _required_env("JWT_SECRET")

JWT_ALGORITHM = os.getenv("JWT_ALGORITHM", "HS256")
ACCESS_TOKEN_EXPIRE_MINUTES = int(os.getenv("ACCESS_TOKEN_EXPIRE_MINUTES", 60))

PORT = int(os.getenv("PORT", 8001))
API_BASE_URL = os.getenv("API_BASE_URL", "http://localhost:8001")
FERNET_KEY = os.getenv("FERNET_KEY") or os.getenv("ENCRYPTION_KEY", "9AHRW3AtKGsdZ6BIELRR4NjPRHFjOfRggwI9Tu0T9zI=")

# ── Redis Cache Configuration ────────────────────────────────────────────────
REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")
REDIS_HOST = os.getenv("REDIS_HOST", "localhost")
REDIS_PORT = int(os.getenv("REDIS_PORT", 6379))
REDIS_PASSWORD = os.getenv("REDIS_PASSWORD", None)
REDIS_DB = int(os.getenv("REDIS_DB", 0))
REDIS_ENABLED = os.getenv("REDIS_ENABLED", "true").lower() in ("true", "1", "yes")

# Connection Pool & Timeout Settings
REDIS_CONNECT_TIMEOUT = float(os.getenv("REDIS_CONNECT_TIMEOUT", 2.0))
REDIS_SOCKET_TIMEOUT = float(os.getenv("REDIS_SOCKET_TIMEOUT", 2.0))
REDIS_MAX_CONNECTIONS = int(os.getenv("REDIS_MAX_CONNECTIONS", 20))

# Cache TTL Configuration (in seconds)
CACHE_TTL_SUMMARY = int(os.getenv("CACHE_TTL_SUMMARY", 300))        # 5 minutes
CACHE_TTL_LIST = int(os.getenv("CACHE_TTL_LIST", 120))              # 2 minutes
CACHE_TTL_SYNC_STATUS = int(os.getenv("CACHE_TTL_SYNC_STATUS", 30))  # 30 seconds

