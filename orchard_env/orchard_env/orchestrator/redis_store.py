"""Redis-based sandbox state store for multi-replica support."""

import asyncio
import json
import logging
import random
import time

import redis.asyncio as redis

from orchard_env.orchestrator.redis_connection import (
    create_redis_client,
    redis_log_target,
)
from orchard_env.orchestrator.settings import settings

logger = logging.getLogger(__name__)


class RedisSandboxStore:
    """Redis-based storage for sandbox state.

    This allows multiple orchestrator replicas to share sandbox state,
    avoiding race conditions in reconciliation.

    Key schema:
    - sandbox:{sandbox_id} -> JSON of Sandbox dataclass
    - sandbox:services:{sandbox_id} -> hash of port -> exposure generation
    - sandbox:lock:{sandbox_id} -> distributed lock for exec serialization
    - sandbox:all -> Set of all sandbox IDs
    """

    SANDBOX_PREFIX = "sandbox:"
    SERVICE_PREFIX = "sandbox:services:"
    SANDBOX_SET_KEY = "sandbox:all"
    LOCK_PREFIX = "sandbox:lock:"
    DEFAULT_TTL = 3600 * 24  # 24 hours

    def __init__(self, redis_url: str = None):
        """Initialize Redis store.

        Args:
            redis_url: Redis connection URL, e.g., redis://localhost:6379/0
        """
        self.redis_url = redis_url or settings.redis_url
        self._client: redis.Redis | None = None
        self._locks: dict[str, asyncio.Lock] = {}  # Local locks for async coordination

    async def connect(self) -> None:
        """Connect to Redis."""
        if self._client is None:
            self._client = create_redis_client(self.redis_url)
            # Test connection
            await self._client.ping()
            logger.info(f"Connected to Redis at {redis_log_target(self.redis_url)}")

    async def close(self) -> None:
        """Close Redis connection."""
        if self._client:
            await self._client.close()
            self._client = None
            logger.info("Closed Redis connection")

    async def _ensure_connected(self) -> redis.Redis:
        """Ensure we have a Redis connection."""
        if self._client is None:
            await self.connect()
        return self._client

    async def store_sandbox(self, sandbox_id: str, sandbox_data: dict) -> None:
        """Store sandbox metadata.

        Args:
            sandbox_id: Unique sandbox ID
            sandbox_data: Sandbox data as dict (from dataclass)
        """
        client = await self._ensure_connected()
        key = f"{self.SANDBOX_PREFIX}{sandbox_id}"

        # Store sandbox data with TTL
        await client.set(key, json.dumps(sandbox_data), ex=self.DEFAULT_TTL)

        # Add to set of all sandboxes
        await client.sadd(self.SANDBOX_SET_KEY, sandbox_id)

        logger.debug(f"Stored sandbox {sandbox_id} in Redis")

    async def get_sandbox(self, sandbox_id: str) -> dict | None:
        """Get sandbox metadata.

        Args:
            sandbox_id: Sandbox ID to retrieve

        Returns:
            Sandbox data as dict, or None if not found
        """
        client = await self._ensure_connected()
        key = f"{self.SANDBOX_PREFIX}{sandbox_id}"

        data = await client.get(key)
        if data:
            return json.loads(data)
        return None

    async def update_sandbox(self, sandbox_id: str, updates: dict) -> bool:
        """Update sandbox metadata without clobbering a concurrent writer."""
        updated = await self.mutate_sandbox(sandbox_id, lambda _record: updates)
        if updated is None:
            return False
        logger.debug(f"Updated sandbox {sandbox_id}: {updates}")
        return True

    async def mutate_sandbox(
        self, sandbox_id: str, mutate, max_attempts: int = 20
    ) -> dict | None:
        """Apply a read-modify-write to a sandbox record atomically.

        The sandbox record is JSON, so even updates to different fields require
        a compare-and-set: a GET/SET pair can restore stale values from another
        field. This runs the mutation inside ``WATCH``/``MULTI`` and retries if
        another writer commits first.

        Args:
            sandbox_id: Sandbox to modify.
            mutate: Callable taking the current record dict and returning the
                fields to write. Returning ``None`` aborts without writing.
                It may be called more than once, so it must be side-effect free.
            max_attempts: Retries before giving up on contention.

        Returns:
            The full updated record, or None if the sandbox does not exist or
            the mutation aborted.

        Raises:
            TimeoutError: If the record stayed contended for every attempt.
        """
        client = await self._ensure_connected()
        key = f"{self.SANDBOX_PREFIX}{sandbox_id}"

        for _attempt in range(max_attempts):
            async with client.pipeline() as pipe:
                try:
                    await pipe.watch(key)
                    data = await pipe.get(key)
                    if not data:
                        await pipe.unwatch()
                        return None

                    sandbox_data = json.loads(data)
                    updates = mutate(sandbox_data)
                    if updates is None:
                        await pipe.unwatch()
                        return None

                    sandbox_data.update(updates)
                    pipe.multi()
                    # Buffered until execute(): not awaited, unlike the
                    # immediate-mode watch/get calls above.
                    pipe.set(key, json.dumps(sandbox_data), ex=self.DEFAULT_TTL)
                    await pipe.execute()
                    return sandbox_data
                except redis.WatchError:
                    # Another replica wrote first; re-read and reapply.
                    await asyncio.sleep(
                        random.uniform(0, min(0.05, 0.001 * 2**_attempt))
                    )
                    continue

        raise TimeoutError(
            f"Could not update sandbox {sandbox_id} after {max_attempts} attempts "
            "due to contention"
        )

    async def expose_service(
        self,
        sandbox_id: str,
        port: int,
        generation: str,
        max_services: int,
        max_attempts: int = 50,
    ) -> tuple[str, bool] | None:
        """Atomically expose a port without changing the sandbox record schema.

        Returns:
            ``(generation, created)``. Re-exposing an active port returns its
            existing generation with ``created=False``. Returns None when the
            sandbox no longer exists.
        """
        client = await self._ensure_connected()
        sandbox_key = f"{self.SANDBOX_PREFIX}{sandbox_id}"
        service_key = f"{self.SERVICE_PREFIX}{sandbox_id}"
        port_field = str(port)

        for _attempt in range(max_attempts):
            async with client.pipeline() as pipe:
                try:
                    await pipe.watch(sandbox_key, service_key)
                    sandbox_data_raw = await pipe.get(sandbox_key)
                    if not sandbox_data_raw:
                        await pipe.unwatch()
                        return None
                    sandbox_created_at = json.loads(sandbox_data_raw)["created_at"]

                    existing = await pipe.hget(service_key, port_field)
                    if existing:
                        try:
                            existing_data = json.loads(existing)
                        except json.JSONDecodeError:
                            existing_data = {}
                        if existing_data.get("created_at") == sandbox_created_at:
                            # Execute a no-state-change transaction so a
                            # concurrent revoke or sandbox delete triggers
                            # WatchError instead of returning a stale URL.
                            pipe.multi()
                            pipe.expire(service_key, self.DEFAULT_TTL)
                            await pipe.execute()
                            return existing_data["generation"], False

                    if await pipe.hlen(service_key) >= max_services:
                        # A stale entry for the same port will be overwritten,
                        # not added, so it doesn't consume another slot.
                        if not existing:
                            await pipe.unwatch()
                            raise ValueError(
                                f"Sandbox {sandbox_id} already exposes "
                                f"{max_services} ports "
                                "(MAX_SERVICES_PER_SANDBOX)"
                            )

                    pipe.multi()
                    pipe.hset(
                        service_key,
                        port_field,
                        json.dumps(
                            {
                                "generation": generation,
                                "created_at": sandbox_created_at,
                            },
                            separators=(",", ":"),
                        ),
                    )
                    pipe.expire(service_key, self.DEFAULT_TTL)
                    await pipe.execute()
                    return generation, True
                except redis.WatchError:
                    await asyncio.sleep(
                        random.uniform(0, min(0.05, 0.001 * 2**_attempt))
                    )
                    continue

        raise TimeoutError(
            f"Could not expose port {port} on sandbox {sandbox_id} after "
            f"{max_attempts} attempts due to contention"
        )

    async def get_service_generation(
        self, sandbox_id: str, port: int, sandbox_created_at: float
    ) -> str | None:
        """Return a generation only when it belongs to this sandbox instance."""
        client = await self._ensure_connected()
        value = await client.hget(f"{self.SERVICE_PREFIX}{sandbox_id}", str(port))
        if not value:
            return None
        try:
            data = json.loads(value)
        except json.JSONDecodeError:
            return None
        if data.get("created_at") != sandbox_created_at:
            return None
        return data.get("generation")

    async def get_services(
        self, sandbox_id: str, sandbox_created_at: float
    ) -> dict[int, str]:
        """Return exposed ports and generations for a sandbox."""
        client = await self._ensure_connected()
        values = await client.hgetall(f"{self.SERVICE_PREFIX}{sandbox_id}")
        result: dict[int, str] = {}
        for port, value in values.items():
            try:
                data = json.loads(value)
            except json.JSONDecodeError:
                continue
            if data.get("created_at") != sandbox_created_at:
                continue
            result[int(port)] = data.get("generation", "")
        return result

    async def revoke_service(self, sandbox_id: str, port: int) -> bool:
        """Revoke a port. A later expose receives a fresh generation."""
        client = await self._ensure_connected()
        deleted = await client.hdel(f"{self.SERVICE_PREFIX}{sandbox_id}", str(port))
        return deleted > 0

    async def delete_sandbox(self, sandbox_id: str) -> bool:
        """Delete sandbox metadata.

        Args:
            sandbox_id: Sandbox ID to delete

        Returns:
            True if deleted, False if not found
        """
        client = await self._ensure_connected()
        key = f"{self.SANDBOX_PREFIX}{sandbox_id}"

        # Remove from set
        await client.srem(self.SANDBOX_SET_KEY, sandbox_id)

        # Delete key
        deleted = await client.delete(key)

        # Clean up lock and separately stored service state. Keeping services
        # out of the JSON record makes rolling upgrades schema-compatible.
        lock_key = f"{self.LOCK_PREFIX}{sandbox_id}"
        service_key = f"{self.SERVICE_PREFIX}{sandbox_id}"
        pod_ip_key = f"{self.POD_IP_PREFIX}{sandbox_id}"
        await client.delete(lock_key, service_key, pod_ip_key)

        if deleted:
            logger.debug(f"Deleted sandbox {sandbox_id} from Redis")
        return deleted > 0

    async def get_all_sandbox_ids(self) -> set[str]:
        """Get all tracked sandbox IDs.

        Returns:
            Set of sandbox IDs
        """
        client = await self._ensure_connected()
        return await client.smembers(self.SANDBOX_SET_KEY)

    async def get_all_sandboxes(self) -> dict[str, dict]:
        """Get all sandbox metadata.

        Returns:
            Dict mapping sandbox_id to sandbox data
        """
        client = await self._ensure_connected()
        sandbox_ids = await self.get_all_sandbox_ids()

        result = {}
        for sandbox_id in sandbox_ids:
            data = await self.get_sandbox(sandbox_id)
            if data:
                result[sandbox_id] = data
            else:
                # Clean up stale entry
                await client.srem(self.SANDBOX_SET_KEY, sandbox_id)

        return result

    async def acquire_lock(self, sandbox_id: str, timeout: int = 300) -> bool:
        """Acquire a distributed lock for a sandbox.

        Used for serializing exec operations on a sandbox.

        Args:
            sandbox_id: Sandbox ID to lock
            timeout: Lock timeout in seconds

        Returns:
            True if lock acquired, False otherwise
        """
        client = await self._ensure_connected()
        lock_key = f"{self.LOCK_PREFIX}{sandbox_id}"

        # Try to acquire lock with NX (only if not exists)
        acquired = await client.set(lock_key, str(time.time()), nx=True, ex=timeout)

        return acquired is not None

    async def release_lock(self, sandbox_id: str) -> None:
        """Release a distributed lock for a sandbox.

        Args:
            sandbox_id: Sandbox ID to unlock
        """
        client = await self._ensure_connected()
        lock_key = f"{self.LOCK_PREFIX}{sandbox_id}"
        await client.delete(lock_key)

    async def get_ready_sandbox_ids(self) -> set[str]:
        """Get IDs of sandboxes that are marked as ready.

        Returns:
            Set of sandbox IDs that are ready
        """
        sandboxes = await self.get_all_sandboxes()
        return {sid for sid, data in sandboxes.items() if data.get("ready", False)}

    async def sandbox_exists(self, sandbox_id: str) -> bool:
        """Check if a sandbox exists in the store.

        Args:
            sandbox_id: Sandbox ID to check

        Returns:
            True if exists, False otherwise
        """
        client = await self._ensure_connected()
        return await client.sismember(self.SANDBOX_SET_KEY, sandbox_id)

    # ---- Pod IP cache (lightweight, dedicated keys) ----

    POD_IP_PREFIX = "sandbox:ip:"
    POD_IP_TTL = 3600 * 24  # 24 hours — same as sandbox TTL

    async def store_pod_ip(
        self, sandbox_id: str, pod_ip: str, sandbox_created_at: float
    ) -> None:
        """Cache a pod IP in Redis for cross-replica lookups.

        Uses a dedicated key (``sandbox:ip:<id>``) instead of embedding
        it inside the sandbox JSON blob so that reads are a single
        O(1) GET rather than GET + JSON parse.
        """
        client = await self._ensure_connected()
        key = f"{self.POD_IP_PREFIX}{sandbox_id}"
        await client.set(
            key,
            json.dumps({"ip": pod_ip, "created_at": sandbox_created_at}),
            ex=self.POD_IP_TTL,
        )
        logger.debug(f"Stored pod IP for {sandbox_id} in Redis: {pod_ip}")

    async def get_pod_ip(
        self, sandbox_id: str, sandbox_created_at: float
    ) -> str | None:
        """Get cached pod IP from Redis.

        Returns:
            Pod IP string, or None if not cached.
        """
        client = await self._ensure_connected()
        key = f"{self.POD_IP_PREFIX}{sandbox_id}"
        value = await client.get(key)
        if not value:
            return None
        try:
            data = json.loads(value)
        except json.JSONDecodeError:
            return None
        if data.get("created_at") != sandbox_created_at:
            return None
        return data.get("ip")

    async def delete_pod_ip(self, sandbox_id: str) -> None:
        """Remove cached pod IP from Redis."""
        client = await self._ensure_connected()
        key = f"{self.POD_IP_PREFIX}{sandbox_id}"
        await client.delete(key)


# Global store instance
_redis_store: RedisSandboxStore | None = None


async def get_redis_store() -> RedisSandboxStore:
    """Get the global Redis store instance."""
    global _redis_store
    if _redis_store is None:
        _redis_store = RedisSandboxStore()
        await _redis_store.connect()
    return _redis_store


async def close_redis_store() -> None:
    """Close the global Redis store."""
    global _redis_store
    if _redis_store:
        await _redis_store.close()
        _redis_store = None
