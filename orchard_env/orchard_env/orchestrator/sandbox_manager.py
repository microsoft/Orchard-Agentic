"""Sandbox lifecycle management with Redis support for multi-replica deployment."""

import asyncio
import logging
import secrets
import time
from dataclasses import asdict, dataclass, field

from orchard_env.orchestrator.k8s_client import K8sClient
from orchard_env.orchestrator.settings import settings

logger = logging.getLogger(__name__)


@dataclass
class Sandbox:
    """Sandbox metadata."""

    sandbox_id: str
    namespace: str
    image: str
    pod_name: str
    block_network: bool
    cpu: str
    memory: str
    created_at: float = field(default_factory=time.time)
    ready: bool = False
    creation_timeout: int = 3600  # Timeout requested during creation
    last_heartbeat: float | None = None  # Last heartbeat timestamp

    def to_dict(self) -> dict:
        """Convert to dictionary for storage."""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "Sandbox":
        """Create from dictionary."""
        return cls(**data)


class SandboxManager:
    """Manages sandbox lifecycle with optional Redis backend for multi-replica support."""

    def __init__(self, k8s_client: K8sClient, redis_store=None):
        """Initialize sandbox manager.

        Args:
            k8s_client: Kubernetes client
            redis_store: Optional RedisSandboxStore for multi-replica support.
                        If None and settings.use_redis is True, will be initialized lazily.
        """
        self.k8s = k8s_client
        self._redis_store = redis_store
        self._pod_watcher = None  # Set via set_pod_watcher()
        self._create_semaphore = asyncio.Semaphore(settings.max_concurrent_creates)

        # In-memory fallback (used when Redis is disabled)
        self._sandboxes: dict[str, Sandbox] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._services: dict[str, dict[int, str]] = {}
        self._global_lock = asyncio.Lock()

    def set_pod_watcher(self, pod_watcher):
        """Set the PodWatcher for cached pod status lookups."""
        self._pod_watcher = pod_watcher

    async def _k8s_ready_check(self, sandbox_id: str) -> bool:
        """Single live readiness check via K8s API (cache-miss fallback)."""
        try:
            status = await self.k8s.check_pod_status(
                name=f"sandbox-{sandbox_id}",
                namespace=self._get_namespace(sandbox_id),
            )
            return bool(status and status.get("ready"))
        except Exception as e:
            logger.warning(f"Live K8s readiness check failed for {sandbox_id}: {e}")
            return False

    async def wait_until_ready(self, sandbox_id: str, timeout: float) -> bool:
        """Return True if the in-pod agent is reachable/ready RIGHT NOW.

        Unlike the latched ``sandbox.ready`` flag (which never resets to False),
        this reflects the agent's current readiness via PodWatcher's live cache
        — a real-time view fed by a single Watch connection, so it costs zero
        extra K8s API calls.  If the pod is currently not-ready (cold start /
        flap), waits up to ``timeout`` for a live status change instead of
        polling.  Falls back to one K8s check only on a genuine cache miss
        (e.g. the pod was created on a different replica).
        """
        if not self._pod_watcher:
            return await self._k8s_ready_check(sandbox_id)

        status = self._pod_watcher.get_pod_status(sandbox_id)
        if status is not None and status.get("ready"):
            return True

        # Cache miss (cross-replica): one K8s check instead of blocking blind.
        if status is None and settings.agent_ready_cache_miss_k8s_fallback:
            if await self._k8s_ready_check(sandbox_id):
                return True

        # Known not-ready — wait (event-driven, no polling) for a status change.
        try:
            result = await self._pod_watcher.wait_for_ready(sandbox_id, timeout=timeout)
            return bool(result and result.get("ready"))
        except TimeoutError:
            return False

    async def get_pod_ip(self, sandbox_id: str) -> str | None:
        """Get Pod IP for direct agent communication.

        Lookup order (fast → slow):
        1. PodWatcher local cache  (in-memory, zero cost)
        2. Redis cache             (shared across replicas, ~1 ms)
        3. K8s API fallback        (slow; writes result back to Redis)

        Returns None if pod IP is not available.
        """
        sandbox = await self.get_sandbox(sandbox_id)
        if not sandbox:
            return None

        # 1. PodWatcher local cache (fastest). A new sandbox creation clears
        # this entry before the pod is created, and deletion clears it before
        # the ID can be reused.
        if self._pod_watcher:
            ip = self._pod_watcher.get_pod_ip(sandbox_id)
            if ip:
                return ip

        # 2. Redis cache (cross-replica, avoids K8s API call)
        redis_store = await self._get_redis_store()
        if redis_store:
            ip = await redis_store.get_pod_ip(sandbox_id, sandbox.created_at)
            if ip:
                return ip

        # 3. K8s API fallback (slow path — write back to Redis)
        ip = await self._fetch_pod_ip_from_k8s(sandbox_id)
        if ip and redis_store:
            await redis_store.store_pod_ip(sandbox_id, ip, sandbox.created_at)
        return ip

    async def get_current_pod_ip(self, sandbox_id: str) -> str | None:
        """Resolve the current Kubernetes pod directly, bypassing every cache.

        Service capabilities cross a trust boundary. A stale PodWatcher or
        Redis entry must never route one to a prior pod after a custom sandbox
        ID is reused, so service connection establishment pays one control-plane
        read for an authoritative target.
        """
        if not await self.get_sandbox(sandbox_id):
            return None
        return await self._fetch_pod_ip_from_k8s(sandbox_id)

    async def _fetch_pod_ip_from_k8s(self, sandbox_id: str) -> str | None:
        """Query K8s API for pod IP (slow path)."""
        namespace = self._get_namespace(sandbox_id)
        pod_name = f"sandbox-{sandbox_id}"
        try:
            pod = await self.k8s.get_pod(name=pod_name, namespace=namespace)
            if pod and pod.status and pod.status.pod_ip:
                logger.info(
                    f"Fetched pod IP from K8s API for {sandbox_id}: "
                    f"{pod.status.pod_ip}"
                )
                return pod.status.pod_ip
            return None
        except Exception as e:
            logger.warning(f"Failed to fetch pod IP from K8s API for {sandbox_id}: {e}")
            return None

    async def _cache_pod_ip(self, sandbox_id: str) -> None:
        """Cache pod IP in Redis (best-effort).

        Reads from PodWatcher local cache or K8s API,
        then writes to Redis for cross-replica visibility.
        """
        ip = None
        if self._pod_watcher:
            ip = self._pod_watcher.get_pod_ip(sandbox_id)
        if not ip:
            ip = await self._fetch_pod_ip_from_k8s(sandbox_id)
        if ip:
            redis_store = await self._get_redis_store()
            if redis_store:
                sandbox = await self.get_sandbox(sandbox_id)
                if sandbox:
                    await redis_store.store_pod_ip(sandbox_id, ip, sandbox.created_at)

    async def _get_redis_store(self):
        """Get Redis store, initializing if needed."""
        if not settings.use_redis:
            return None

        if self._redis_store is None:
            from orchard_env.orchestrator.redis_store import get_redis_store

            self._redis_store = await get_redis_store()

        return self._redis_store

    def _get_namespace(self, sandbox_id: str) -> str:
        """Get namespace for sandbox. All sandboxes share one namespace."""
        return settings.sandbox_namespace

    async def ensure_sandbox_namespace(self) -> None:
        """Ensure the shared sandbox namespace exists. Called once at startup."""
        namespace = settings.sandbox_namespace
        exists = await self.k8s.namespace_exists(namespace)
        if not exists:
            await self.k8s.create_namespace(
                name=namespace, labels={"app": "sandbox", "managed-by": "orchestrator"}
            )
            logger.info(f"Created shared sandbox namespace: {namespace}")
        else:
            logger.info(f"Shared sandbox namespace already exists: {namespace}")

        # Create a namespace-wide deny-all-egress NetworkPolicy once.
        # This applies to ALL pods in the namespace, so per-sandbox
        # create / delete of NetworkPolicy is no longer needed when
        # block_network=True (the default).  Saves 2 K8s API calls per
        # sandbox lifecycle.
        await self.k8s.create_network_policy(
            name="deny-all-egress",
            namespace=namespace,
            block_egress=True,
            pod_labels=None,  # targets every pod in the namespace
        )
        logger.info(f"Ensured namespace-wide deny-all-egress policy in {namespace}")

    async def _store_sandbox(self, sandbox: Sandbox) -> None:
        """Store sandbox in Redis or memory."""
        redis_store = await self._get_redis_store()

        if redis_store:
            await redis_store.store_sandbox(sandbox.sandbox_id, sandbox.to_dict())
        else:
            async with self._global_lock:
                self._sandboxes[sandbox.sandbox_id] = sandbox
                self._services.pop(sandbox.sandbox_id, None)
                if sandbox.sandbox_id not in self._locks:
                    self._locks[sandbox.sandbox_id] = asyncio.Lock()

    async def _update_sandbox(self, sandbox_id: str, updates: dict) -> None:
        """Update sandbox in Redis or memory."""
        redis_store = await self._get_redis_store()

        if redis_store:
            await redis_store.update_sandbox(sandbox_id, updates)
        else:
            async with self._global_lock:
                if sandbox_id in self._sandboxes:
                    for key, value in updates.items():
                        setattr(self._sandboxes[sandbox_id], key, value)

    async def _delete_sandbox_record(self, sandbox_id: str) -> None:
        """Delete sandbox record from Redis or memory."""
        redis_store = await self._get_redis_store()

        if redis_store:
            await redis_store.delete_sandbox(sandbox_id)
        else:
            async with self._global_lock:
                if sandbox_id in self._sandboxes:
                    del self._sandboxes[sandbox_id]
                if sandbox_id in self._locks:
                    del self._locks[sandbox_id]
                self._services.pop(sandbox_id, None)

    async def _get_all_sandbox_ids(self) -> set:
        """Get all tracked sandbox IDs."""
        redis_store = await self._get_redis_store()

        if redis_store:
            return await redis_store.get_all_sandbox_ids()
        else:
            async with self._global_lock:
                return set(self._sandboxes.keys())

    async def _get_ready_sandbox_ids(self) -> set:
        """Get IDs of sandboxes marked as ready."""
        redis_store = await self._get_redis_store()

        if redis_store:
            return await redis_store.get_ready_sandbox_ids()
        else:
            async with self._global_lock:
                return {
                    sid for sid, sandbox in self._sandboxes.items() if sandbox.ready
                }

    async def create_sandbox(
        self,
        sandbox_id: str,
        image: str,
        block_network: bool = True,
        cpu: str | None = None,
        memory: str | None = None,
        wait_ready: bool = True,
        timeout: int | None = None,
    ) -> Sandbox:
        """Create a new sandbox.

        All sandboxes are created as pods in the shared namespace.
        No per-sandbox namespace is created.

        Args:
            sandbox_id: Unique sandbox ID
            image: Container image to use
            block_network: Whether to block network egress
            cpu: CPU request/limit
            memory: Memory request/limit
            wait_ready: If True, wait for pod to be ready before returning.
                       If False, return immediately after creating pod (status will be pending).
            timeout: Timeout in seconds for pod to become ready. Defaults to 3600.

        Returns:
            Sandbox object (ready=True if pod is running, ready=False if still pending)
        """
        namespace = self._get_namespace(sandbox_id)
        pod_name = f"sandbox-{sandbox_id}"

        # Use provided values or defaults
        cpu = cpu or settings.default_cpu
        memory = memory or settings.default_memory
        timeout = timeout or 3600

        logger.info(
            f"Creating sandbox {sandbox_id} with image {image}, "
            f"block_network={block_network}, cpu={cpu}, memory={memory}, wait_ready={wait_ready}, timeout={timeout}"
        )

        # Track what we've created for cleanup on cancellation
        pod_created = False

        try:
            # A caller may intentionally reuse a custom sandbox ID after the
            # previous pod was deleted. Clear every local/shared cache before
            # creating the new pod so no stale target can be blessed as the new
            # sandbox incarnation.
            if self._pod_watcher:
                self._pod_watcher.prepare_sandbox(sandbox_id)
            redis_store = await self._get_redis_store()
            if redis_store:
                await redis_store.delete_pod_ip(sandbox_id)

            # Throttle concurrent creates to avoid overwhelming K8s API server
            # With 5 replicas × 20 concurrent creates = up to 100 K8s create calls
            async with self._create_semaphore:
                # Network isolation: The namespace-wide deny-all-egress
                # policy (created once at startup in ensure_sandbox_namespace)
                # covers the default block_network=True case — no per-sandbox
                # NetworkPolicy needed.
                #
                # If block_network=False, create an allow-egress policy that
                # targets only this pod (by sandbox-id label). K8s NetworkPolicy
                # rules are additive — if any policy allows the traffic, it's
                # permitted, even if another policy denies it.
                if not block_network:
                    await self.k8s.create_network_policy(
                        name=f"allow-egress-{sandbox_id}",
                        namespace=namespace,
                        block_egress=False,
                        pod_labels={"sandbox-id": sandbox_id},
                    )
                    logger.info(f"Created allow-egress policy for sandbox {sandbox_id}")

                # Create pod in the shared namespace
                await self.k8s.create_pod(
                    name=pod_name,
                    namespace=namespace,
                    image=image,
                    cpu=cpu,
                    memory=memory,
                    node_selector=settings.sandbox_node_selector,
                    working_dir=settings.default_working_dir,
                    sandbox_id=sandbox_id,
                )
                pod_created = True

            # Semaphore released — pod is submitted to K8s, no longer
            # holding a create slot while waiting for readiness.

            # Store sandbox metadata immediately (not ready yet if not waiting)
            sandbox = Sandbox(
                sandbox_id=sandbox_id,
                namespace=namespace,
                image=image,
                pod_name=pod_name,
                block_network=block_network,
                cpu=cpu,
                memory=memory,
                ready=False,  # Will be set to True after pod is ready
                creation_timeout=timeout,
            )

            # Store in Redis or memory
            await self._store_sandbox(sandbox)

            if not wait_ready:
                # Return immediately - client will poll for ready status
                logger.info(
                    f"Created sandbox {sandbox_id} (pending, not waiting for ready)"
                )
                return sandbox

            # Wait for pod to be ready
            ready = await self.k8s.wait_pod_ready(
                name=pod_name, namespace=namespace, timeout=timeout
            )

            if not ready:
                # Clean up if pod failed to start
                await self.delete_sandbox(sandbox_id)
                raise RuntimeError(f"Failed to start pod for sandbox {sandbox_id}")

            # Update sandbox to ready
            sandbox.ready = True
            await self._update_sandbox(sandbox_id, {"ready": True})

            # Cache pod IP in Redis for cross-replica lookups
            await self._cache_pod_ip(sandbox_id)

            logger.info(f"Successfully created sandbox {sandbox_id}")
            return sandbox

        except asyncio.CancelledError:
            # Request was cancelled (e.g., client disconnected)
            logger.warning(
                f"Sandbox creation cancelled for {sandbox_id}, cleaning up..."
            )
            if pod_created:
                try:
                    await self.k8s.delete_pod(pod_name, namespace)
                    logger.info(f"Cleaned up pod {pod_name} after cancellation")
                except Exception as cleanup_error:
                    logger.error(f"Failed to cleanup pod {pod_name}: {cleanup_error}")
            await self._delete_sandbox_record(sandbox_id)
            raise  # Re-raise to propagate cancellation

        except Exception as e:
            # Other errors - also cleanup
            logger.error(f"Error creating sandbox {sandbox_id}: {e}")
            if pod_created:
                try:
                    await self.k8s.delete_pod(pod_name, namespace)
                    logger.info(f"Cleaned up pod {pod_name} after error")
                except Exception as cleanup_error:
                    logger.error(f"Failed to cleanup pod {pod_name}: {cleanup_error}")
            await self._delete_sandbox_record(sandbox_id)
            raise

    async def get_sandbox(self, sandbox_id: str) -> Sandbox | None:
        """Get sandbox by ID."""
        redis_store = await self._get_redis_store()

        if redis_store:
            data = await redis_store.get_sandbox(sandbox_id)
            if data:
                return Sandbox.from_dict(data)
            return None
        else:
            async with self._global_lock:
                return self._sandboxes.get(sandbox_id)

    async def check_sandbox_ready(self, sandbox_id: str) -> Sandbox | None:
        """Check if sandbox is ready, updating status if needed.

        Prefers PodWatcher cache (zero API calls) when available.
        Falls back to direct K8s API call otherwise.

        Returns:
            Sandbox object with updated ready status, or None if not found.
            Also sets sandbox._pod_status dict with scheduling details.
        """
        sandbox = await self.get_sandbox(sandbox_id)

        # If not in store, try to recover from Kubernetes
        if not sandbox:
            sandbox = await self._recover_sandbox_from_k8s(sandbox_id)
            if not sandbox:
                return None

        # If already marked ready, return as-is
        if sandbox.ready:
            sandbox._pod_status = {
                "ready": True,
                "phase": "Running",
                "status": "ready",
                "message": "",
            }
            return sandbox

        # Try PodWatcher cache first (zero API calls)
        if self._pod_watcher:
            cached_status = self._pod_watcher.get_pod_status(sandbox_id)
            if cached_status is not None:
                sandbox._pod_status = cached_status
                if cached_status["ready"] and not sandbox.ready:
                    sandbox.ready = True
                    await self._update_sandbox(sandbox_id, {"ready": True})
                    await self._cache_pod_ip(sandbox_id)
                return sandbox

        # Fall back to direct K8s API call
        try:
            pod_status = await self.k8s.check_pod_status(
                name=sandbox.pod_name, namespace=sandbox.namespace
            )
            sandbox._pod_status = pod_status
            if pod_status["ready"] and not sandbox.ready:
                sandbox.ready = True
                await self._update_sandbox(sandbox_id, {"ready": True})
                await self._cache_pod_ip(sandbox_id)
            return sandbox
        except Exception as e:
            logger.error(f"Error checking sandbox {sandbox_id} status: {e}")
            sandbox._pod_status = {
                "ready": False,
                "phase": "Unknown",
                "status": "pending",
                "message": str(e),
            }
            return sandbox

    async def _recover_sandbox_from_k8s(self, sandbox_id: str) -> Sandbox | None:
        """Try to recover sandbox metadata from Kubernetes.

        This handles cases where orchestrator lost state but
        the sandbox still exists in Kubernetes.
        """
        namespace = self._get_namespace(sandbox_id)
        pod_name = f"sandbox-{sandbox_id}"

        try:
            # Try to get the pod from Kubernetes
            pod = await self.k8s.get_pod(name=pod_name, namespace=namespace)
            if not pod:
                return None

            # Extract info from pod
            container = pod.spec.containers[0] if pod.spec.containers else None
            image = container.image if container else "unknown"

            # Extract resources
            cpu = settings.default_cpu
            memory = settings.default_memory
            if container and container.resources and container.resources.limits:
                cpu = container.resources.limits.get("cpu", cpu)
                memory = container.resources.limits.get("memory", memory)

            # Check if network is blocked by looking for per-pod network policy
            block_network = await self.k8s.has_network_policy(
                namespace=namespace, name=f"deny-egress-{sandbox_id}"
            )

            # Create sandbox object
            sandbox = Sandbox(
                sandbox_id=sandbox_id,
                namespace=namespace,
                image=image,
                pod_name=pod_name,
                block_network=block_network,
                cpu=cpu,
                memory=memory,
                ready=False,  # Will be updated by caller
            )

            # Store it
            await self._store_sandbox(sandbox)
            logger.info(f"Recovered sandbox {sandbox_id} from Kubernetes")

            return sandbox

        except Exception as e:
            logger.debug(f"Could not recover sandbox {sandbox_id} from k8s: {e}")
            return None

    async def delete_sandbox(self, sandbox_id: str) -> None:
        """Delete a sandbox.

        Removes the sandbox from the store immediately (so it is no longer
        visible to API callers), then fires off the actual K8s resource
        cleanup in the background.  This makes the DELETE API call return
        nearly instantly and prevents delete operations from competing
        with create / exec for K8s API semaphore slots.
        """
        logger.info(f"Deleting sandbox {sandbox_id}")

        namespace = self._get_namespace(sandbox_id)
        sandbox = await self.get_sandbox(sandbox_id)
        pod_name = f"sandbox-{sandbox_id}"
        block_network = sandbox.block_network if sandbox else False

        # 1. Remove from store immediately — sandbox is "gone" for callers.
        await self._delete_sandbox_record(sandbox_id)
        if self._pod_watcher:
            self._pod_watcher.remove_sandbox(sandbox_id)

        # 2. Clean up K8s resources in the background (best-effort).
        asyncio.create_task(
            self._background_delete_k8s_resources(
                sandbox_id, pod_name, namespace, block_network
            )
        )

    async def _background_delete_k8s_resources(
        self,
        sandbox_id: str,
        pod_name: str,
        namespace: str,
        block_network: bool,
    ) -> None:
        """Background task: delete pod and per-sandbox NetworkPolicy from K8s."""
        try:
            await self.k8s.delete_pod(pod_name, namespace, grace_period_seconds=0)
        except Exception as e:
            logger.warning(f"Could not delete pod {pod_name}: {e}")

        # Clean up the per-sandbox allow-egress policy (if it was created)
        if not block_network:
            try:
                await self.k8s.delete_network_policy(
                    f"allow-egress-{sandbox_id}", namespace
                )
            except Exception as e:
                logger.warning(
                    f"Could not delete allow-egress policy for {sandbox_id}: {e}"
                )

        logger.info(f"Background cleanup completed for sandbox {sandbox_id}")

    async def get_sandbox_lock(self, sandbox_id: str) -> asyncio.Lock | None:
        """Get the execution lock for a sandbox.

        Note: For Redis mode, this returns a local lock. For true distributed
        locking, use the Redis store's acquire_lock/release_lock methods.
        """
        redis_store = await self._get_redis_store()

        if redis_store:
            # For Redis mode, we still use local locks for the async context
            # but the sandbox existence is checked from Redis
            if sandbox_id not in self._locks:
                if await redis_store.sandbox_exists(sandbox_id):
                    self._locks[sandbox_id] = asyncio.Lock()
            return self._locks.get(sandbox_id)
        else:
            async with self._global_lock:
                return self._locks.get(sandbox_id)

    async def heartbeat(self, sandbox_id: str) -> bool:
        """Update heartbeat timestamp for a sandbox.

        Args:
            sandbox_id: Sandbox ID

        Returns:
            True if sandbox exists and heartbeat was updated
        """
        sandbox = await self.get_sandbox(sandbox_id)
        if not sandbox:
            return False

        now = time.time()
        await self._update_sandbox(sandbox_id, {"last_heartbeat": now})
        logger.debug(f"Heartbeat received for sandbox {sandbox_id}")
        return True

    async def expose_service(
        self, sandbox_id: str, port: int
    ) -> tuple[str, bool] | None:
        """Expose a port and return ``(generation, created)``.

        Service state is stored separately from the sandbox JSON record. This
        keeps rolling upgrades schema-compatible with replicas that do not know
        about service endpoints yet. The opaque generation changes after a
        revoke/re-expose cycle, permanently invalidating old URLs.
        """
        generation = secrets.token_urlsafe(18)
        redis_store = await self._get_redis_store()

        if redis_store:
            result = await redis_store.expose_service(
                sandbox_id,
                port,
                generation,
                settings.max_services_per_sandbox,
            )
            if result and result[1]:
                logger.info(f"Exposed port {port} on sandbox {sandbox_id}")
            return result

        async with self._global_lock:
            if sandbox_id not in self._sandboxes:
                return None
            services = self._services.setdefault(sandbox_id, {})
            existing = services.get(port)
            if existing:
                return existing, False
            if len(services) >= settings.max_services_per_sandbox:
                raise ValueError(
                    f"Sandbox {sandbox_id} already exposes "
                    f"{settings.max_services_per_sandbox} ports "
                    "(MAX_SERVICES_PER_SANDBOX)"
                )
            services[port] = generation

        logger.info(f"Exposed port {port} on sandbox {sandbox_id}")
        return generation, True

    async def get_service_generation(self, sandbox_id: str, port: int) -> str | None:
        """Return the active generation for a port."""
        sandbox = await self.get_sandbox(sandbox_id)
        if not sandbox:
            return None
        redis_store = await self._get_redis_store()
        if redis_store:
            return await redis_store.get_service_generation(
                sandbox_id, port, sandbox.created_at
            )
        async with self._global_lock:
            return self._services.get(sandbox_id, {}).get(port)

    async def list_services(self, sandbox_id: str) -> list[int] | None:
        """Return exposed ports, or None if the sandbox does not exist."""
        sandbox = await self.get_sandbox(sandbox_id)
        if not sandbox:
            return None
        redis_store = await self._get_redis_store()
        if redis_store:
            services = await redis_store.get_services(sandbox_id, sandbox.created_at)
            return sorted(services)
        async with self._global_lock:
            return sorted(self._services.get(sandbox_id, {}))

    async def revoke_service(self, sandbox_id: str, port: int) -> bool:
        """Remove *port* from a sandbox's allowlist.

        Revocation takes effect immediately, including for capability tokens
        that have not yet expired, because the proxy re-checks the allowlist on
        every request.

        Returns:
            True if the port was exposed and has been revoked.
        """

        redis_store = await self._get_redis_store()
        if redis_store:
            revoked = await redis_store.revoke_service(sandbox_id, port)
        else:
            async with self._global_lock:
                services = self._services.get(sandbox_id, {})
                revoked = services.pop(port, None) is not None

        if revoked:
            logger.info(f"Revoked port {port} on sandbox {sandbox_id}")
        return revoked

    async def cleanup_expired_sandboxes(self) -> int:
        """Clean up sandboxes that exceeded TTL, stuck in pending state, or missed heartbeats."""
        logger.info("Starting sandbox TTL cleanup")

        redis_store = await self._get_redis_store()
        now = time.time()
        ttl_seconds = settings.sandbox_ttl_hours * 3600
        # Extra buffer beyond creation_timeout before considering a pending sandbox abandoned
        pending_timeout_buffer = settings.pending_timeout_buffer_seconds
        heartbeat_timeout = settings.heartbeat_timeout_seconds
        heartbeat_enabled = settings.heartbeat_cleanup_enabled

        to_delete = []

        if redis_store:
            sandboxes = await redis_store.get_all_sandboxes()
            for sandbox_id, data in sandboxes.items():
                age = now - data.get("created_at", now)
                is_ready = data.get("ready", False)
                creation_timeout = data.get("creation_timeout", 3600)
                last_heartbeat = data.get("last_heartbeat")

                # Delete if TTL exceeded
                if age > ttl_seconds:
                    to_delete.append((sandbox_id, "TTL expired"))
                # Delete if stuck in pending beyond its creation timeout + buffer
                elif not is_ready and age > creation_timeout + pending_timeout_buffer:
                    to_delete.append(
                        (
                            sandbox_id,
                            f"pending timeout (age={age:.0f}s, limit={creation_timeout + pending_timeout_buffer}s)",
                        )
                    )
                # Delete if heartbeat timed out
                # Only applies to sandboxes that have actively sent at least one heartbeat.
                # Sandboxes that never sent a heartbeat are NOT cleaned up here —
                # they rely on TTL expiration. This prevents premature deletion of
                # sandboxes from clients that don't use the heartbeat mechanism.
                elif heartbeat_enabled and is_ready and last_heartbeat is not None:
                    time_since_heartbeat = now - last_heartbeat
                    if time_since_heartbeat > heartbeat_timeout:
                        to_delete.append(
                            (
                                sandbox_id,
                                f"heartbeat timeout ({time_since_heartbeat:.0f}s since last heartbeat, limit={heartbeat_timeout}s)",
                            )
                        )
        else:
            async with self._global_lock:
                for sandbox_id, sandbox in self._sandboxes.items():
                    age = now - sandbox.created_at

                    # Delete if TTL exceeded
                    if age > ttl_seconds:
                        to_delete.append((sandbox_id, "TTL expired"))
                    # Delete if stuck in pending beyond its creation timeout + buffer
                    elif (
                        not sandbox.ready
                        and age > sandbox.creation_timeout + pending_timeout_buffer
                    ):
                        to_delete.append(
                            (
                                sandbox_id,
                                f"pending timeout (age={age:.0f}s, limit={sandbox.creation_timeout + pending_timeout_buffer}s)",
                            )
                        )
                    # Delete if heartbeat timed out
                    elif (
                        heartbeat_enabled
                        and sandbox.ready
                        and sandbox.last_heartbeat is not None
                    ):
                        time_since_heartbeat = now - sandbox.last_heartbeat
                        if time_since_heartbeat > heartbeat_timeout:
                            to_delete.append(
                                (
                                    sandbox_id,
                                    f"heartbeat timeout ({time_since_heartbeat:.0f}s since last heartbeat, limit={heartbeat_timeout}s)",
                                )
                            )

        # Delete expired/stuck sandboxes
        for sandbox_id, reason in to_delete:
            try:
                await self.delete_sandbox(sandbox_id)
                logger.info(f"Cleaned up sandbox {sandbox_id} ({reason})")
            except Exception as e:
                logger.error(f"Error cleaning up sandbox {sandbox_id}: {e}")

        if to_delete:
            logger.info(f"Cleaned up {len(to_delete)} sandboxes")

        return len(to_delete)

    async def reconcile_sandboxes(self) -> None:
        """Reconcile sandbox state with Kubernetes.

        Lists pods in the shared sandbox namespace and compares with the store.

        Handles two cases:
        1. Orphaned pods: exist in K8s but not tracked in store
        2. Missing pods: tracked in store but don't exist in K8s

        IMPORTANT: We must be careful not to remove sandboxes that are still
        being created (not yet ready). Only remove sandboxes that are marked
        as ready but whose pod has been deleted.
        """
        logger.info("Reconciling sandboxes with Kubernetes")

        namespace = settings.sandbox_namespace

        # Get all sandbox pods from Kubernetes (by label)
        k8s_sandbox_ids = await self.k8s.list_sandbox_pods(namespace)

        # If LIST failed, skip reconciliation to avoid mass-deleting sandbox records
        if k8s_sandbox_ids is None:
            logger.warning(
                "Skipping reconciliation: failed to list sandbox pods from K8s"
            )
            return

        # Get tracked sandbox IDs from store (Redis or memory)
        tracked_sandbox_ids = await self._get_all_sandbox_ids()
        ready_sandbox_ids = await self._get_ready_sandbox_ids()

        # Find orphaned pods (in K8s but not tracked in store)
        orphaned = k8s_sandbox_ids - tracked_sandbox_ids
        if orphaned:
            logger.info(f"Found {len(orphaned)} orphaned sandbox pods")
            for sandbox_id in orphaned:
                pod_name = f"sandbox-{sandbox_id}"
                try:
                    await self.k8s.delete_pod(pod_name, namespace)
                    logger.info(f"Deleted orphaned pod {pod_name}")
                except Exception as e:
                    logger.error(f"Error deleting orphaned pod {pod_name}: {e}")

        # Find missing pods (tracked AND READY but not in K8s)
        # Only remove sandboxes that were ready - pending ones might still be creating
        missing = ready_sandbox_ids - k8s_sandbox_ids
        if missing:
            logger.info(
                f"Found {len(missing)} missing sandbox pods (ready but pod gone)"
            )
            for sandbox_id in missing:
                await self._delete_sandbox_record(sandbox_id)
                logger.info(f"Removed tracking for missing sandbox {sandbox_id}")

        # Log info about pending sandboxes (for debugging)
        pending_count = len(tracked_sandbox_ids - ready_sandbox_ids)
        if pending_count > 0:
            logger.debug(
                f"Skipping {pending_count} pending sandboxes during reconciliation"
            )
