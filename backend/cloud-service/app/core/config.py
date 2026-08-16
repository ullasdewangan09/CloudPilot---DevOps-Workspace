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


DATABASE_URL = _required_env("DATABASE_URL")
JWT_SECRET = _required_env("JWT_SECRET")

JWT_ALGORITHM = os.getenv("JWT_ALGORITHM", "HS256")
ACCESS_TOKEN_EXPIRE_MINUTES = int(os.getenv("ACCESS_TOKEN_EXPIRE_MINUTES", 60))

PORT = int(os.getenv("PORT", 8001))
API_BASE_URL = os.getenv("API_BASE_URL", "http://localhost:8001")
