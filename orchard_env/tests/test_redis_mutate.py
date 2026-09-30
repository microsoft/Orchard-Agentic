"""Tests for Redis sandbox compare-and-set and separate service state."""

import json

import pytest
from redis.exceptions import WatchError

from orchard_env.orchestrator.redis_store import RedisSandboxStore


class FakePipeline:
    """Small redis.asyncio Pipeline model for WATCH/MULTI tests."""

    def __init__(self, client: "FakeRedisClient"):
        self.client = client
        self.watched: dict[str, int] = {}
        self.buffered: list[tuple] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info):
        return False

    async def watch(self, *keys):
        self.watched = {key: self.client.versions.get(key, 0) for key in keys}

    async def unwatch(self):
        self.watched = {}

    async def get(self, key):
        return self.client.data.get(key)

    async def exists(self, key):
        return int(key in self.client.data or key in self.client.hashes)

    async def hget(self, key, field):
        return self.client.hashes.get(key, {}).get(field)

    async def hlen(self, key):
        return len(self.client.hashes.get(key, {}))

    def multi(self):
        self.buffered = []

    def set(self, key, value, ex=None):
        self.buffered.append(("set", key, value))
        return self

    def hset(self, key, field, value):
        self.buffered.append(("hset", key, field, value))
        return self

    def expire(self, key, ttl):
        self.buffered.append(("expire", key, ttl))
        return self

    async def execute(self):
        if any(
            self.client.versions.get(key, 0) != version
            for key, version in self.watched.items()
        ):
            raise WatchError("WATCHed key changed")
        for operation in self.buffered:
            if operation[0] == "set":
                _, key, value = operation
                self.client.write(key, value)
            elif operation[0] == "hset":
                _, key, field, value = operation
                self.client.hashes.setdefault(key, {})[field] = value
                self.client.bump(key)
        return [True] * len(self.buffered)


class FakeRedisClient:
    def __init__(self):
        self.data: dict[str, str] = {}
        self.hashes: dict[str, dict[str, str]] = {}
        self.versions: dict[str, int] = {}

    def bump(self, key):
        self.versions[key] = self.versions.get(key, 0) + 1

    def write(self, key, value):
        self.data[key] = value
        self.bump(key)

    def pipeline(self):
        return FakePipeline(self)

    async def expire(self, key, ttl):
        return key in self.data or key in self.hashes

    async def hget(self, key, field):
        return self.hashes.get(key, {}).get(field)

    async def hgetall(self, key):
        return dict(self.hashes.get(key, {}))

    async def hdel(self, key, field):
        if field not in self.hashes.get(key, {}):
            return 0
        del self.hashes[key][field]
        self.bump(key)
        return 1

    async def get(self, key):
        return self.data.get(key)

    async def set(self, key, value, ex=None):
        self.write(key, value)
        return True

    async def delete(self, *keys):
        deleted = 0
        for key in keys:
            deleted += int(key in self.data or key in self.hashes)
            self.data.pop(key, None)
            self.hashes.pop(key, None)
            self.bump(key)
        return deleted


@pytest.fixture
def store():
    redis_store = RedisSandboxStore(redis_url="redis://unused")
    client = FakeRedisClient()
    client.write(
        "sandbox:s1",
        json.dumps(
            {
                "sandbox_id": "s1",
                "created_at": 100.0,
                "ready": False,
                "last_heartbeat": None,
            }
        ),
    )

    async def _ensure_connected():
        return client

    redis_store._ensure_connected = _ensure_connected
    redis_store._fake_client = client
    return redis_store


class TestSandboxMutation:
    @pytest.mark.asyncio
    async def test_update_uses_compare_and_set(self, store):
        assert await store.update_sandbox("s1", {"last_heartbeat": 123.0})
        stored = json.loads(store._fake_client.data["sandbox:s1"])
        assert stored["last_heartbeat"] == 123.0

    @pytest.mark.asyncio
    async def test_conflicting_field_write_is_preserved(self, store):
        client = store._fake_client
        attempts = {"count": 0}

        def mutate(record):
            attempts["count"] += 1
            if attempts["count"] == 1:
                client.write(
                    "sandbox:s1",
                    json.dumps(
                        {
                            "sandbox_id": "s1",
                            "created_at": 100.0,
                            "ready": True,
                            "last_heartbeat": None,
                        }
                    ),
                )
            return {"last_heartbeat": 123.0}

        result = await store.mutate_sandbox("s1", mutate)
        assert attempts["count"] == 2
        assert result["ready"] is True
        assert result["last_heartbeat"] == 123.0

    @pytest.mark.asyncio
    async def test_missing_sandbox_returns_none(self, store):
        assert await store.mutate_sandbox("gone", lambda record: {"a": 1}) is None

    @pytest.mark.asyncio
    async def test_contention_eventually_fails(self, store):
        client = store._fake_client

        def mutate(record):
            client.write("sandbox:s1", json.dumps(record))
            return {"ready": True}

        with pytest.raises(TimeoutError, match="contention"):
            await store.mutate_sandbox("s1", mutate, max_attempts=3)


class TestServiceState:
    @pytest.mark.asyncio
    async def test_expose_creates_separate_hash_state(self, store):
        result = await store.expose_service("s1", 8000, "g1", max_services=8)
        assert result == ("g1", True)
        assert await store.get_service_generation("s1", 8000, 100.0) == "g1"
        sandbox = json.loads(store._fake_client.data["sandbox:s1"])
        assert "exposed_ports" not in sandbox

    @pytest.mark.asyncio
    async def test_stale_state_cannot_authorize_reused_sandbox_id(self, store):
        await store.expose_service("s1", 8000, "old", max_services=8)
        store._fake_client.write(
            "sandbox:s1",
            json.dumps(
                {
                    "sandbox_id": "s1",
                    "created_at": 200.0,
                    "ready": True,
                    "last_heartbeat": None,
                }
            ),
        )
        assert await store.get_service_generation("s1", 8000, 200.0) is None
        assert await store.expose_service("s1", 8000, "new", max_services=8) == (
            "new",
            True,
        )

    @pytest.mark.asyncio
    async def test_active_reexpose_returns_same_generation(self, store):
        await store.expose_service("s1", 8000, "g1", max_services=8)
        assert await store.expose_service("s1", 8000, "g2", max_services=8) == (
            "g1",
            False,
        )

    @pytest.mark.asyncio
    async def test_revoke_then_reexpose_uses_new_generation(self, store):
        await store.expose_service("s1", 8000, "g1", max_services=8)
        assert await store.revoke_service("s1", 8000)
        assert await store.expose_service("s1", 8000, "g2", max_services=8) == (
            "g2",
            True,
        )

    @pytest.mark.asyncio
    async def test_limit_is_atomic(self, store):
        await store.expose_service("s1", 8000, "g1", max_services=1)
        with pytest.raises(ValueError, match="MAX_SERVICES_PER_SANDBOX"):
            await store.expose_service("s1", 8001, "g2", max_services=1)

    @pytest.mark.asyncio
    async def test_unknown_sandbox_not_exposed(self, store):
        assert await store.expose_service("gone", 8000, "g1", max_services=8) is None


class TestPodIpState:
    @pytest.mark.asyncio
    async def test_pod_ip_is_bound_to_sandbox_instance(self, store):
        await store.store_pod_ip("s1", "10.0.0.1", 100.0)
        assert await store.get_pod_ip("s1", 100.0) == "10.0.0.1"
        assert await store.get_pod_ip("s1", 200.0) is None

    @pytest.mark.asyncio
    async def test_delete_removes_cached_ip(self, store):
        await store.store_pod_ip("s1", "10.0.0.1", 100.0)
        await store.delete_pod_ip("s1")
        assert await store.get_pod_ip("s1", 100.0) is None
