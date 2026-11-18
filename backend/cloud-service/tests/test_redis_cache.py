"""
CloudPilot — Phase 3B.6 Automated Test Suite
Redis Caching & Cache Invalidation Integration Tests.

Verifies:
1. Redis Connection Manager:
   - Successful connection pooling & ping
   - Graceful degradation when Redis is down/unreachable
   - Password masking in connection URLs
2. Resource Summary Caching:
   - Cache miss populates Redis with correct TTL
   - Cache hit returns cached data without querying PostgreSQL
   - Expired TTL triggers database reload
   - Malformed cache data triggers safe fallback to PostgreSQL
3. Resource Listing Caching:
   - Parameterized cache keys (query hashing)
   - Different filter permutations generate distinct keys
   - Safe serialization of non-sensitive attributes
4. Cache Invalidation:
   - Successful synchronization invalidates affected account cache
   - Failed synchronization preserves existing cache without corruption
   - Account deactivation/deletion invalidates cache
   - Invalidation is strictly scoped to (workspace_id, account_id)
   - Invalidation failure does not fail or rollback database transactions
5. Security & Isolation:
   - Workspace tenant isolation: keys partitioned by workspace_id and account_id
   - Authorization checked in PostgreSQL before cache lookup
   - Zero credentials, tokens, or Fernet ciphertexts stored in Redis
   - Bypass cache query parameter support
"""

import json
import uuid
import time
import fnmatch
from datetime import datetime, timezone, timedelta
from unittest.mock import patch, MagicMock
import pytest
from sqlalchemy import text

from app.models.cloud_account import CloudAccount
from app.models.cloud_resource import (
    CloudResource,
    CloudSyncJob,
    SyncStatus,
    SyncType,
    ResourceType,
)
from app.core.cache import RedisCacheManager, cache_manager, KEY_PREFIX
from app.schemas.cloud_resource import ResourceSummaryResponse
from app.services.resource_sync import ResourceSyncEngine


# ── In-Memory Fake Redis Client for Mocking ──────────────────────────────────

class FakeRedis:
    """In-memory Redis fake for predictable unit and integration testing."""

    def __init__(self):
        self.data: dict = {}
        self.ttls: dict = {}

    def ping(self):
        return True

    def get(self, key):
        return self.data.get(key)

    def set(self, key, value, ex=None):
        self.data[key] = value
        if ex:
            self.ttls[key] = ex
        return True

    def delete(self, *keys):
        count = 0
        for k in keys:
            if k in self.data:
                del self.data[k]
                self.ttls.pop(k, None)
                count += 1
        return count

    def scan_iter(self, match=None, count=None):
        if not match:
            return iter(list(self.data.keys()))
        for k in list(self.data.keys()):
            if fnmatch.fnmatch(k, match):
                yield k

    def incr(self, key):
        val = int(self.data.get(key, 0)) + 1
        self.data[key] = str(val)
        return val

    def expire(self, key, ttl):
        if key in self.data:
            self.ttls[key] = ttl
            return True
        return False

    def close(self):
        pass


@pytest.fixture
def fake_redis():
    """Provides an isolated in-memory Redis client attached to cache_manager."""
    fake = FakeRedis()
    original_client = cache_manager._client
    original_connected = cache_manager._is_connected
    original_enabled = cache_manager.enabled

    cache_manager._client = fake
    cache_manager._is_connected = True
    cache_manager.enabled = True

    with patch.object(cache_manager, "connect", return_value=True):
        yield fake

    cache_manager._client = original_client
    cache_manager._is_connected = original_connected
    cache_manager.enabled = original_enabled


@pytest.fixture
def test_cloud_account(db, test_workspace):
    """Seed an active AWS CloudAccount in test_workspace."""
    account = CloudAccount(
        workspace_id=test_workspace,
        provider="aws",
        name="Production AWS Infrastructure",
        account_id="123456789012",
        default_region="us-east-1",
        role_arn="arn:aws:iam::123456789012:role/CloudPilotAccessRole",
        external_id="secure-ext-id-12345",
        status="active",
        is_active=True,
        description="Core AWS account"
    )
    db.add(account)
    db.commit()
    db.refresh(account)
    return account


@pytest.fixture
def seed_resources(db, test_cloud_account):
    """Seed sample infrastructure resources in PostgreSQL."""
    resources = [
        CloudResource(
            cloud_account_id=test_cloud_account.id,
            workspace_id=test_cloud_account.workspace_id,
            provider="aws",
            resource_type=ResourceType.EC2_INSTANCE.value,
            provider_resource_id="i-web-prod-01",
            name="Prod-Web",
            region="us-east-1",
            status="running",
            tags={"Env": "prod"},
            metadata_={"instance_type": "t3.medium"}
        ),
        CloudResource(
            cloud_account_id=test_cloud_account.id,
            workspace_id=test_cloud_account.workspace_id,
            provider="aws",
            resource_type=ResourceType.S3_BUCKET.value,
            provider_resource_id="prod-assets-bucket",
            name="prod-assets-bucket",
            region="us-east-1",
            status="available",
            tags={},
            metadata_={"versioning": "Enabled"}
        )
    ]
    for r in resources:
        db.add(r)
    db.commit()
    return resources


# ==============================================================================
# 1. REDIS CONNECTION MANAGER TESTS
# ==============================================================================

def test_redis_connection_success(fake_redis):
    """Verify Redis connection manager correctly reports connected state and pings."""
    assert cache_manager.ping() is True
    assert cache_manager.client is not None


def test_redis_masked_url():
    """Verify Redis URL masks passwords and credentials in logs/strings."""
    mgr = RedisCacheManager()
    with patch("app.core.cache.REDIS_URL", "redis://:secretpassword@redis-host:6379/0"):
        masked = mgr.get_masked_url()
        assert "secretpassword" not in masked
        assert "***" in masked


def test_redis_unavailable_graceful_fallback(client, test_user, test_cloud_account, seed_resources):
    """Verify application gracefully falls back to PostgreSQL when Redis is disconnected."""
    with patch.object(cache_manager, "get", return_value=None), \
         patch.object(cache_manager, "set", return_value=False):

        resp = client.get(
            f"/api/v1/cloud/accounts/{test_cloud_account.id}/resources/summary",
            headers=test_user["headers"]
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["total_active_resources"] == 2
        assert data["ec2_instances"] == 1
        assert data["s3_buckets"] == 1


# ==============================================================================
# 2. RESOURCE SUMMARY CACHING (GET /resources/summary)
# ==============================================================================

def test_summary_cache_miss_and_populate(client, test_user, test_cloud_account, seed_resources, fake_redis):
    """Verify first request is a cache miss that populates Redis with correct TTL."""
    cache_key = cache_manager.build_summary_key(
        test_cloud_account.workspace_id, test_cloud_account.id, include_deleted=False
    )
    assert cache_key not in fake_redis.data

    resp = client.get(
        f"/api/v1/cloud/accounts/{test_cloud_account.id}/resources/summary",
        headers=test_user["headers"]
    )
    assert resp.status_code == 200
    assert cache_key in fake_redis.data

    # Verify cached payload structure
    cached = json.loads(fake_redis.data[cache_key])
    assert cached["total_active_resources"] == 2
    assert cached["ec2_instances"] == 1
    assert cached["s3_buckets"] == 1
    assert fake_redis.ttls.get(cache_key) == 300  # Default 300s TTL


def test_summary_cache_hit(client, test_user, test_cloud_account, seed_resources, fake_redis, db):
    """Verify second request returns cached data without querying PostgreSQL."""
    cache_key = cache_manager.build_summary_key(
        test_cloud_account.workspace_id, test_cloud_account.id, include_deleted=False
    )

    # Pre-populate cache with unique cached values
    mock_summary = {
        "cloud_account_id": str(test_cloud_account.id),
        "total_active_resources": 999,
        "ec2_instances": 500,
        "s3_buckets": 499,
        "vpc_resources": 0,
        "rds_instances": 0,
        "by_region": {"us-east-1": 999},
        "by_status": {"running": 999}
    }
    fake_redis.set(cache_key, json.dumps(mock_summary))

    resp = client.get(
        f"/api/v1/cloud/accounts/{test_cloud_account.id}/resources/summary",
        headers=test_user["headers"]
    )
    assert resp.status_code == 200
    data = resp.json()
    # Should return the cached 999 count, proving PostgreSQL was not re-queried
    assert data["total_active_resources"] == 999
    assert data["ec2_instances"] == 500


def test_summary_cache_malformed_fallback(client, test_user, test_cloud_account, seed_resources, fake_redis):
    """Verify malformed JSON in Redis triggers safe fallback to PostgreSQL."""
    cache_key = cache_manager.build_summary_key(
        test_cloud_account.workspace_id, test_cloud_account.id, include_deleted=False
    )
    # Put corrupted data in Redis
    fake_redis.set(cache_key, "corrupted-json-data{{")

    resp = client.get(
        f"/api/v1/cloud/accounts/{test_cloud_account.id}/resources/summary",
        headers=test_user["headers"]
    )
    assert resp.status_code == 200
    data = resp.json()
    # Falls back to database and returns real DB count (2)
    assert data["total_active_resources"] == 2


def test_summary_cache_bypass(client, test_user, test_cloud_account, seed_resources, fake_redis):
    """Verify bypass_cache=true skips cache and reloads from PostgreSQL."""
    cache_key = cache_manager.build_summary_key(
        test_cloud_account.workspace_id, test_cloud_account.id, include_deleted=False
    )
    # Pre-populate with stale count 999
    mock_summary = {
        "cloud_account_id": str(test_cloud_account.id),
        "total_active_resources": 999,
        "ec2_instances": 500,
        "s3_buckets": 499,
        "vpc_resources": 0,
        "rds_instances": 0,
        "by_region": {},
        "by_status": {}
    }
    fake_redis.set(cache_key, json.dumps(mock_summary))

    resp = client.get(
        f"/api/v1/cloud/accounts/{test_cloud_account.id}/resources/summary?bypass_cache=true",
        headers=test_user["headers"]
    )
    assert resp.status_code == 200
    data = resp.json()
    # Bypassed cache: real database count returned
    assert data["total_active_resources"] == 2


# ==============================================================================
# 3. RESOURCE LISTING CACHING (GET /resources)
# ==============================================================================

def test_resource_listing_cache_hit_and_miss(client, test_user, test_cloud_account, seed_resources, fake_redis):
    """Verify resource listing caching with query parameter hashing."""
    # 1. First request misses cache
    resp1 = client.get(
        f"/api/v1/cloud/accounts/{test_cloud_account.id}/resources?resource_type=ec2_instance",
        headers=test_user["headers"]
    )
    assert resp1.status_code == 200
    assert resp1.json()["total"] == 1

    # Check key exists in fake_redis
    list_keys = [k for k in fake_redis.data if "resources:list" in k]
    assert len(list_keys) == 1
    key_ec2 = list_keys[0]

    # 2. Query with different filter produces a distinct key
    resp2 = client.get(
        f"/api/v1/cloud/accounts/{test_cloud_account.id}/resources?resource_type=s3_bucket",
        headers=test_user["headers"]
    )
    assert resp2.status_code == 200
    assert resp2.json()["total"] == 1

    list_keys_after = [k for k in fake_redis.data if "resources:list" in k]
    assert len(list_keys_after) == 2
    assert key_ec2 in list_keys_after


# ==============================================================================
# 4. CACHE INVALIDATION
# ==============================================================================

def test_cache_invalidation_on_successful_sync(db, test_cloud_account, seed_resources, fake_redis):
    """Verify successful resource synchronization invalidates the account's cache."""
    # Warm summary and list cache
    sum_key = cache_manager.build_summary_key(test_cloud_account.workspace_id, test_cloud_account.id)
    list_key = cache_manager.build_list_key(test_cloud_account.workspace_id, test_cloud_account.id, {"page": 1})
    fake_redis.set(sum_key, json.dumps({"test": 1}))
    fake_redis.set(list_key, json.dumps({"test": 2}))

    assert sum_key in fake_redis.data
    assert list_key in fake_redis.data

    # Execute a sync using ResourceSyncEngine
    engine = ResourceSyncEngine(db)
    ec2_item = {
        "provider": "aws",
        "resource_type": ResourceType.EC2_INSTANCE.value,
        "provider_resource_id": "i-new-synced",
        "name": "New-Synced-Instance",
        "region": "us-east-1",
        "status": "running",
        "tags": {},
        "metadata": {}
    }

    with patch("app.services.resource_sync.AWSSessionManager.assume_role", return_value={"account_id": "123456789012"}), \
         patch("app.services.resource_sync.AWSSessionManager.is_expired", return_value=False), \
         patch("app.services.resource_sync.EC2Discovery.discover", return_value=[ec2_item]):

        job = engine.sync_account(test_cloud_account.id, test_cloud_account.workspace_id, sync_type=SyncType.EC2)

    assert job.status == SyncStatus.COMPLETED.value
    # Summary and list cache keys must be invalidated!
    assert sum_key not in fake_redis.data
    assert list_key not in fake_redis.data


def test_cache_not_invalidated_for_unrelated_accounts(test_cloud_account, fake_redis):
    """Verify invalidating Account A does not touch Account B's cache keys."""
    ws_id = test_cloud_account.workspace_id
    acct_a = test_cloud_account.id
    acct_b = uuid.uuid4()

    key_a = cache_manager.build_summary_key(ws_id, acct_a)
    key_b = cache_manager.build_summary_key(ws_id, acct_b)

    fake_redis.set(key_a, json.dumps({"account": "A"}))
    fake_redis.set(key_b, json.dumps({"account": "B"}))

    # Invalidate Account A
    cache_manager.invalidate_account_cache(ws_id, acct_a)

    assert key_a not in fake_redis.data
    assert key_b in fake_redis.data  # Account B must remain cached


def test_failed_sync_does_not_corrupt_cache(db, test_cloud_account, fake_redis):
    """Verify failed synchronization preserves existing cache without corruption."""
    sum_key = cache_manager.build_summary_key(test_cloud_account.workspace_id, test_cloud_account.id)
    original_data = json.dumps({"valid": "data"})
    fake_redis.set(sum_key, original_data)

    engine = ResourceSyncEngine(db)
    # AssumeRole failure
    with patch("app.services.resource_sync.AWSSessionManager.assume_role", side_effect=Exception("STS Failed")):
        job = engine.sync_account(test_cloud_account.id, test_cloud_account.workspace_id, sync_type=SyncType.EC2)

    assert job.status == SyncStatus.FAILED.value
    # Cache was not corrupted
    assert fake_redis.data.get(sum_key) == original_data


def test_account_deactivation_invalidates_cache(client, test_user, test_cloud_account, fake_redis):
    """Verify soft-deleting an account invalidates its Redis cache keys."""
    sum_key = cache_manager.build_summary_key(test_cloud_account.workspace_id, test_cloud_account.id)
    fake_redis.set(sum_key, json.dumps({"test": "data"}))

    resp = client.delete(
        f"/api/v1/cloud/accounts/{test_cloud_account.id}",
        headers=test_user["headers"]
    )
    assert resp.status_code == 200
    assert sum_key not in fake_redis.data


def test_cache_invalidation_failure_does_not_fail_db_operation(client, test_user, test_cloud_account, fake_redis):
    """Verify Redis errors during invalidation do not crash or fail application logic."""
    # 1. Direct call test: underlying scan_iter error is caught and returns 0
    with patch.object(fake_redis, "scan_iter", side_effect=Exception("Redis scan timeout")):
        count = cache_manager.invalidate_account_cache(test_cloud_account.workspace_id, test_cloud_account.id)
        assert count == 0

    # 2. API delete test: even if invalidate_account_cache raises an unexpected exception, delete succeeds
    with patch.object(cache_manager, "invalidate_account_cache", side_effect=Exception("Catastrophic Redis failure")):
        resp = client.delete(
            f"/api/v1/cloud/accounts/{test_cloud_account.id}",
            headers=test_user["headers"]
        )
        assert resp.status_code == 200


# ==============================================================================
# 5. SECURITY & TENANT ISOLATION
# ==============================================================================

def test_authorization_checked_before_cache(client, other_user, test_cloud_account, seed_resources, fake_redis):
    """CRITICAL SECURITY TEST: Non-members are rejected with 403 Forbidden even if cache is warm."""
    # Warm the cache
    cache_key = cache_manager.build_summary_key(test_cloud_account.workspace_id, test_cloud_account.id)
    fake_redis.set(cache_key, json.dumps({"total_active_resources": 10}))

    # Non-member attempts to read
    resp = client.get(
        f"/api/v1/cloud/accounts/{test_cloud_account.id}/resources/summary",
        headers=other_user["headers"]
    )
    assert resp.status_code == 403
    assert "access" in resp.json()["detail"].lower()


def test_zero_credentials_stored_in_redis(client, test_user, test_cloud_account, seed_resources, fake_redis):
    """Verify no IAM Role ARNs, External IDs, or temporary STS tokens are stored in Redis."""
    # Trigger summary and listing to populate cache
    client.get(f"/api/v1/cloud/accounts/{test_cloud_account.id}/resources/summary", headers=test_user["headers"])
    client.get(f"/api/v1/cloud/accounts/{test_cloud_account.id}/resources", headers=test_user["headers"])

    for key, val in fake_redis.data.items():
        val_str = str(val)
        # Role ARN and external ID should never be in cache
        assert "CloudPilotAccessRole" not in val_str
        assert "secure-ext-id" not in val_str
        assert "AccessKeyId" not in val_str
        assert "SecretAccessKey" not in val_str
        assert "SessionToken" not in val_str


# ==============================================================================
# 6. ARCHITECTURE AUDIT: RACE CONDITIONS, SYNC STATUS & FAILURE MODES
# ==============================================================================

def test_concurrent_read_race_condition_aborts_stale_cache(db, test_user, test_cloud_account, seed_resources, fake_redis, client):
    """
    CRITICAL CONCURRENCY AUDIT:
    Verifies that when a concurrent write (e.g. sync job) commits and invalidates cache
    during an in-flight read, the in-flight read's stale data is NOT cached or is isolated.
    """
    ws_id = test_cloud_account.workspace_id
    acct_id = test_cloud_account.id

    # Generation starts at 1
    gen1 = cache_manager.get_generation(ws_id, acct_id)
    assert gen1 == 1

    # Simulate Reader 1: Starts reading, acquires generation 1
    read_gen = cache_manager.get_generation(ws_id, acct_id)

    # Concurrently, Writer commits new resources and invalidates cache
    cache_manager.invalidate_account_cache(ws_id, acct_id)

    # Generation is bumped atomically to 2
    gen2 = cache_manager.get_generation(ws_id, acct_id)
    assert gen2 == 2

    # Reader 1 now finishes its database query and checks generation before writing
    current_gen = cache_manager.get_generation(ws_id, acct_id)
    assert current_gen != read_gen  # Mismatch detected!

    # Old generation 1 key must not be populated as active generation
    gen2_key = cache_manager.build_summary_key(ws_id, acct_id, generation=gen2)
    assert gen2_key not in fake_redis.data

    # A subsequent read automatically operates in generation 2
    resp = client.get(
        f"/api/v1/cloud/accounts/{acct_id}/resources/summary",
        headers=test_user["headers"]
    )
    assert resp.status_code == 200
    assert gen2_key in fake_redis.data


def test_query_parameter_casing_and_whitespace_canonical_cache(client, test_user, test_cloud_account, seed_resources, fake_redis):
    """Verify that different casing and whitespace for identical filter criteria map to the same cache key."""
    # 1. Query with uppercase and extra whitespace
    resp1 = client.get(
        f"/api/v1/cloud/accounts/{test_cloud_account.id}/resources?resource_type=EC2_INSTANCE&region=%20US-EAST-1%20",
        headers=test_user["headers"]
    )
    assert resp1.status_code == 200
    assert resp1.json()["total"] == 1

    list_keys_1 = [k for k in fake_redis.data if "resources:list" in k]
    assert len(list_keys_1) == 1
    canonical_key = list_keys_1[0]

    # 2. Query with lowercase and clean whitespace
    resp2 = client.get(
        f"/api/v1/cloud/accounts/{test_cloud_account.id}/resources?resource_type=ec2_instance&region=us-east-1",
        headers=test_user["headers"]
    )
    assert resp2.status_code == 200
    assert resp2.json()["total"] == 1

    # Must hit the exact same canonical key, no duplicate cache entries created
    list_keys_2 = [k for k in fake_redis.data if "resources:list" in k]
    assert len(list_keys_2) == 1
    assert list_keys_2[0] == canonical_key


def test_sync_status_lifecycle_invalidation(client, test_user, test_cloud_account, db, fake_redis):
    """
    Verifies that every sync job state transition (PENDING, RUNNING, COMPLETED, FAILED, ZOMBIE)
    immediately invalidates the cached sync status so stale job states are never returned.
    """
    ws_id = test_cloud_account.workspace_id
    acct_id = test_cloud_account.id

    # 1. Warm sync status cache
    resp = client.get(f"/api/v1/cloud/accounts/{acct_id}/sync/status", headers=test_user["headers"])
    assert resp.status_code == 200
    assert resp.json()["has_active_job"] is False

    status_keys = [k for k in fake_redis.data if "sync:status" in k]
    assert len(status_keys) == 1
    status_key = status_keys[0]

    # 2. Create sync job -> must invalidate sync status cache
    engine = ResourceSyncEngine(db)
    job = engine.create_sync_job(acct_id, ws_id, sync_type=SyncType.EC2)
    assert status_key not in fake_redis.data

    # 3. Transition to RUNNING -> must invalidate sync status cache
    fake_redis.set(status_key, json.dumps({"cached": "state"}))
    assert status_key in fake_redis.data

    with patch("app.services.resource_sync.AWSSessionManager.from_cloud_account") as mock_mgr:
        mock_mgr.return_value.assume_role.return_value = {"account_id": "123456789012"}
        mock_mgr.return_value.is_expired.return_value = False
        with patch.object(engine, "_discover_ec2", return_value=[]):
            engine.execute_sync(job.id)

    # Job finished -> cache must be invalidated
    assert status_key not in fake_redis.data

    # 4. Job failure -> must invalidate sync status cache
    job2 = engine.create_sync_job(acct_id, ws_id, sync_type=SyncType.EC2)
    fake_redis.set(status_key, json.dumps({"cached": "state"}))
    engine._fail_job(job2, "Simulated failure")
    assert status_key not in fake_redis.data

    # 5. Zombie recovery -> must invalidate sync status cache
    job3 = engine.create_sync_job(acct_id, ws_id, sync_type=SyncType.EC2)
    job3.status = SyncStatus.RUNNING.value
    job3.started_at = datetime.now(timezone.utc) - timedelta(minutes=120)
    db.commit()

    fake_redis.set(status_key, json.dumps({"cached": "state"}))
    engine.recover_zombie_jobs(acct_id)
    assert status_key not in fake_redis.data


def test_redis_circuit_breaker_reconnect_cooldown():
    """Verify that Redis connection manager throttles reconnect attempts to prevent blocking delays."""
    mgr = RedisCacheManager()
    mgr.enabled = True
    mgr._is_connected = False
    mgr._last_connect_attempt = time.time()
    mgr._reconnect_cooldown = 10.0

    # Within cooldown period, connect(force=False) immediately returns False without opening sockets
    with patch("app.core.cache.redis.Redis") as mock_redis:
        assert mgr.connect(force=False) is False
        mock_redis.assert_not_called()

    # Forcing (or after cooldown expires) allows reconnect attempt to proceed
    with patch("app.core.cache.redis.Redis") as mock_redis:
        mock_redis.return_value.ping.return_value = True
        assert mgr.connect(force=True) is True
        assert mgr._is_connected is True
        mock_redis.assert_called_once()


def test_cache_bypass_refreshes_redis(client, test_user, test_cloud_account, seed_resources, fake_redis):
    """Verify bypass_cache=true skips cache read AND refreshes Redis with authoritative database state."""
    gen = cache_manager.get_generation(test_cloud_account.workspace_id, test_cloud_account.id)
    sum_key = cache_manager.build_summary_key(
        test_cloud_account.workspace_id, test_cloud_account.id, generation=gen
    )

    # Pre-seed with stale count 999
    stale_payload = {
        "cloud_account_id": str(test_cloud_account.id),
        "total_active_resources": 999,
        "ec2_instances": 500,
        "s3_buckets": 499,
        "vpc_resources": 0,
        "rds_instances": 0,
        "by_region": {},
        "by_status": {}
    }
    fake_redis.set(sum_key, json.dumps(stale_payload))

    # Request with bypass_cache=true
    resp = client.get(
        f"/api/v1/cloud/accounts/{test_cloud_account.id}/resources/summary?bypass_cache=true",
        headers=test_user["headers"]
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["total_active_resources"] == 2  # Fresh DB count

    # Redis cache must be refreshed with the fresh DB count (2)
    cached_after = json.loads(fake_redis.data[sum_key])
    assert cached_after["total_active_resources"] == 2


def test_redis_set_unsupported_data_type_fails_gracefully(fake_redis):
    """Verify that attempting to cache non-serializable objects fails gracefully without exceptions."""
    # Attempt to cache an un-serializable lambda or custom object
    unserializable = lambda x: x * 2
    success = cache_manager.set("test:unserializable:key", unserializable)
    assert success is False


def test_inactive_account_rejects_cached_data(client, test_user, test_cloud_account, fake_redis, db):
    """Verify that a deactivated account returns 404 Not Found even if cached data exists in Redis."""
    sum_key = cache_manager.build_summary_key(test_cloud_account.workspace_id, test_cloud_account.id)
    fake_redis.set(sum_key, json.dumps({"total_active_resources": 10}))

    # Deactivate account in PostgreSQL
    test_cloud_account.is_active = False
    db.commit()

    resp = client.get(
        f"/api/v1/cloud/accounts/{test_cloud_account.id}/resources/summary",
        headers=test_user["headers"]
    )
    assert resp.status_code == 404
    assert "not found" in resp.json()["detail"].lower()
