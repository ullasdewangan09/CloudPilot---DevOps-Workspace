import os
from pathlib import Path
from urllib.parse import urlencode

from dotenv import load_dotenv


SERVICE_ROOT = Path(__file__).resolve().parents[2]


def _load_nearest_dotenv(start_path: Path):
    for directory in [start_path, *start_path.parents]:
        env_path = directory / ".env"
        if env_path.exists():
            load_dotenv(env_path)


_load_nearest_dotenv(SERVICE_ROOT)
load_dotenv(SERVICE_ROOT / ".env", override=True)


def _required_env(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(
            f"{name} is not set. Add it to {SERVICE_ROOT / '.env'} "
            "or export it before starting the auth service."
        )
    return value


DATABASE_URL = os.getenv("TEST_DATABASE_URL") or _required_env("DATABASE_URL")
JWT_SECRET = _required_env("JWT_SECRET")

JWT_ALGORITHM = os.getenv("JWT_ALGORITHM", "HS256")

ACCESS_TOKEN_EXPIRE_MINUTES = int(
    os.getenv("ACCESS_TOKEN_EXPIRE_MINUTES", 60)
)

REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")

API_BASE_URL = os.getenv("API_BASE_URL", "http://localhost:8000")
FRONTEND_SUCCESS_URL = os.getenv(
    "FRONTEND_SUCCESS_URL",
    f"{API_BASE_URL}/docs"
)

GOOGLE_CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID")
GOOGLE_CLIENT_SECRET = os.getenv("GOOGLE_CLIENT_SECRET")
GOOGLE_REDIRECT_URI = os.getenv(
    "GOOGLE_REDIRECT_URI",
    f"{API_BASE_URL}/api/v1/auth/google/callback"
)

GITHUB_CLIENT_ID = os.getenv("GITHUB_CLIENT_ID")
GITHUB_CLIENT_SECRET = os.getenv("GITHUB_CLIENT_SECRET")
GITHUB_REDIRECT_URI = os.getenv(
    "GITHUB_REDIRECT_URI",
    f"{API_BASE_URL}/api/v1/auth/github/callback"
)

MICROSOFT_CLIENT_ID = os.getenv("MICROSOFT_CLIENT_ID")
MICROSOFT_CLIENT_SECRET = os.getenv("MICROSOFT_CLIENT_SECRET")
MICROSOFT_REDIRECT_URI = os.getenv(
    "MICROSOFT_REDIRECT_URI",
    f"{API_BASE_URL}/api/v1/auth/microsoft/callback"
)
MICROSOFT_TENANT = os.getenv("MICROSOFT_TENANT", "common")


def with_query_params(base_url: str, params: dict) -> str:
    return f"{base_url}?{urlencode(params)}"
