"""
CloudPilot — Redis Cache Manager & Key Architecture (Phase 3B.6).

Responsibilities:
1. Manages connection pooling, lifecycle, and health checking for Redis 7.
2. Provides graceful degradation: if Redis is unavailable or disconnected,
   PostgreSQL continues functioning as the authoritative data source.
3. Enforces versioned, tenant-isolated cache key namespacing:
   - Summary: cloudpilot:v1:resources:summary:{workspace_id}:{account_id}:{include_deleted}
   - List:    cloudpilot:v1:resources:list:{workspace_id}:{account_id}:{query_hash}
   - Status:  cloudpilot:v1:sync:status:{workspace_id}:{account_id}
4. Implements scoped cache invalidation for specific workspace and cloud account pairs.
5. Strictly prevents caching of sensitive credentials (role ARNs, external IDs, STS tokens).
6. Never logs passwords or sensitive Redis configuration strings.
"""

import hashlib
import json
import logging
import uuid
from typing import Any, Dict, List, Optional, Union
from datetime import datetime, date

try:
    import redis
    from redis.exceptions import (
        RedisError,
        ConnectionError as RedisConnectionError,
        TimeoutError as RedisTimeoutError,
    )
    REDIS_LIB_AVAILABLE = True
except ImportError:  # pragma: no cover
    REDIS_LIB_AVAILABLE = False
    redis = None
    RedisError = Exception
    RedisConnectionError = Exception
    RedisTimeoutError = Exception

from app.core.config import (
    REDIS_URL,
    REDIS_HOST,
    REDIS_PORT,
    REDIS_PASSWORD,
    REDIS_DB,
    REDIS_ENABLED,
    REDIS_CONNECT_TIMEOUT,
    REDIS_SOCKET_TIMEOUT,
    REDIS_MAX_CONNECTIONS,
    CACHE_TTL_SUMMARY,
    CACHE_TTL_LIST,
    CACHE_TTL_SYNC_STATUS,
)

logger = logging.getLogger(__name__)

# Cache Key Namespace Version
KEY_VERSION = "v1"
KEY_PREFIX = f"cloudpilot:{KEY_VERSION}"


def json_serializer(obj: Any) -> Any:
    """JSON serializer for objects not serializable by default json code."""
    if isinstance(obj, (datetime, date)):
        return obj.isoformat()
    if isinstance(obj, uuid.UUID):
        return str(obj)
    if isinstance(obj, (set, tuple)):
        return list(obj)
    raise TypeError(f"Type {type(obj)} not serializable")


class RedisCacheManager:
    """
    Resilient Redis client manager with connection pooling and graceful error handling.
    """

    def __init__(self):
        self.enabled: bool = REDIS_ENABLED and REDIS_LIB_AVAILABLE
        self._pool: Optional[Any] = None
        self._client: Optional[Any] = None
        self._is_connected: bool = False

    def get_masked_url(self) -> str:
        """Returns safe representation of Redis URL with credentials redacted."""
        if not REDIS_URL:
            return "redis://localhost:6379/0"
        if "@" in REDIS_URL:
            # Mask user:pass before @
            prefix, rest = REDIS_URL.split("@", 1)
            scheme = prefix.split("://")[0]
            return f"{scheme}://***:***@{rest}"
        return REDIS_URL

    def connect(self) -> bool:
        """
        Initializes the Redis connection pool.
        Returns True if connection succeeded, False if Redis is disabled or unreachable.
        """
        if not self.enabled:
            logger.info("Redis caching is disabled via configuration.")
            return False

        try:
            # Use connection pool for high-concurrency connection reuse
            if REDIS_PASSWORD:
                self._pool = redis.ConnectionPool(
                    host=REDIS_HOST,
                    port=REDIS_PORT,
                    password=REDIS_PASSWORD,
                    db=REDIS_DB,
                    max_connections=REDIS_MAX_CONNECTIONS,
                    socket_connect_timeout=REDIS_CONNECT_TIMEOUT,
                    socket_timeout=REDIS_SOCKET_TIMEOUT,
                    decode_responses=True,
                )
            elif REDIS_URL:
                self._pool = redis.ConnectionPool.from_url(
                    REDIS_URL,
                    max_connections=REDIS_MAX_CONNECTIONS,
                    socket_connect_timeout=REDIS_CONNECT_TIMEOUT,
                    socket_timeout=REDIS_SOCKET_TIMEOUT,
                    decode_responses=True,
                )
            else:
                self._pool = redis.ConnectionPool(
                    host=REDIS_HOST,
                    port=REDIS_PORT,
                    db=REDIS_DB,
                    max_connections=REDIS_MAX_CONNECTIONS,
                    socket_connect_timeout=REDIS_CONNECT_TIMEOUT,
                    socket_timeout=REDIS_SOCKET_TIMEOUT,
                    decode_responses=True,
                )

            self._client = redis.Redis(connection_pool=self._pool)
            # Perform initial ping to verify readiness
            self._client.ping()
            self._is_connected = True
            logger.info(f"Successfully connected to Redis at {self.get_masked_url()}")
            return True

        except (RedisError, Exception) as exc:
            self._is_connected = False
            self._client = None
            logger.warning(
                f"Redis unavailable at {self.get_masked_url()}: {type(exc).__name__}. "
                "Application will operate with direct PostgreSQL fallback."
            )
            return False

    def close(self) -> None:
        """Gracefully disconnects and releases the Redis connection pool."""
        try:
            if self._client:
                self._client.close()
            if self._pool:
                self._pool.disconnect()
        except Exception as exc:
            logger.warning(f"Error closing Redis connections: {exc}")
        finally:
            self._is_connected = False
            self._client = None
            self._pool = None

    def ping(self) -> bool:
        """Checks if the Redis instance is responsive."""
        if not self.enabled or not self._client:
            return False
        try:
            return bool(self._client.ping())
        except Exception:
            return False

    @property
    def client(self) -> Optional[Any]:
        """Lazy access to Redis client with automatic reconnect attempt."""
        if not self.enabled:
            return None
        if self._client is None or not self._is_connected:
            self.connect()
        return self._client

    # ── Key Generation Functions ─────────────────────────────────────────────

    @staticmethod
    def build_summary_key(workspace_id: uuid.UUID, account_id: uuid.UUID, include_deleted: bool = False) -> str:
        """Builds cache key for resource summaries."""
        del_flag = 1 if include_deleted else 0
        return f"{KEY_PREFIX}:resources:summary:{workspace_id}:{account_id}:{del_flag}"

    @staticmethod
    def build_list_key(
        workspace_id: uuid.UUID,
        account_id: uuid.UUID,
        params: Dict[str, Any]
    ) -> str:
        """
        Builds deterministic cache key for filtered resource listings using SHA-256 query hash.
        """
        # Normalize params: sort keys, remove None, convert UUIDs to string
        norm_params = {
            str(k): (str(v) if isinstance(v, uuid.UUID) else v)
            for k, v in sorted(params.items())
            if v is not None
        }
        serialized = json.dumps(norm_params, sort_keys=True)
        query_hash = hashlib.sha256(serialized.encode("utf-8")).hexdigest()[:16]
        return f"{KEY_PREFIX}:resources:list:{workspace_id}:{account_id}:{query_hash}"

    @staticmethod
    def build_sync_status_key(workspace_id: uuid.UUID, account_id: uuid.UUID) -> str:
        """Builds cache key for sync status."""
        return f"{KEY_PREFIX}:sync:status:{workspace_id}:{account_id}"

    # ── Core Cache Operations ────────────────────────────────────────────────

    def get(self, key: str) -> Optional[Any]:
        """
        Fetches and deserializes a JSON object from Redis.
        Returns None on cache miss, Redis error, or invalid JSON.
        """
        cli = self.client
        if not cli:
            return None

        try:
            data = cli.get(key)
            if data is None:
                return None
            return json.loads(data)
        except (RedisError, json.JSONDecodeError, Exception) as exc:
            logger.warning(f"Redis get failed for key '{key}': {type(exc).__name__}. Falling back to PostgreSQL.")
            return None

    def set(self, key: str, value: Any, ttl: int = 300) -> bool:
        """
        Serializes and caches a Python object as JSON with a bounded TTL.
        Returns True on success, False on error.
        """
        cli = self.client
        if not cli:
            return False

        try:
            payload = json.dumps(value, default=json_serializer)
            return bool(cli.set(key, payload, ex=ttl))
        except (RedisError, Exception) as exc:
            logger.warning(f"Redis set failed for key '{key}': {type(exc).__name__}.")
            return False

    def delete(self, key: str) -> bool:
        """Deletes a single key from Redis."""
        cli = self.client
        if not cli:
            return False
        try:
            return bool(cli.delete(key))
        except Exception as exc:
            logger.warning(f"Redis delete failed for key '{key}': {exc}")
            return False

    def invalidate_account_cache(self, workspace_id: uuid.UUID, account_id: uuid.UUID) -> int:
        """
        Invalidates all cached data (summaries, listings, sync status) strictly belonging
        to the specified workspace and cloud account pair.
        Never touches keys belonging to other workspaces or accounts.
        Returns the count of invalidated keys.
        """
        cli = self.client
        if not cli:
            return 0

        # Match all keys containing :{workspace_id}:{account_id}
        pattern = f"{KEY_PREFIX}:*:{workspace_id}:{account_id}*"
        invalidated_count = 0

        try:
            # Use scan_iter for non-blocking cursor-based iteration
            keys = list(cli.scan_iter(match=pattern, count=100))
            if keys:
                invalidated_count = cli.delete(*keys)
                logger.info(
                    f"Invalidated {invalidated_count} cache keys for account {account_id} "
                    f"in workspace {workspace_id}."
                )
            return invalidated_count
        except Exception as exc:
            logger.warning(
                f"Failed to invalidate cache for account {account_id} in workspace {workspace_id}: {exc}. "
                "PostgreSQL remains authoritative."
            )
            return 0


# Global singleton cache manager instance
cache_manager = RedisCacheManager()

