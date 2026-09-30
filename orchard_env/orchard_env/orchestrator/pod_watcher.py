"""Pod Watcher using Kubernetes Watch API (Informer pattern).

Replaces polling-based pod status checks with a single Watch connection
that receives real-time push notifications from the K8s API server.

Before (polling): 200 sandboxes × ~15 polls each = 3000 API calls
After  (watch):   1 LIST + 1 Watch connection = 2 API calls total

The watcher maintains a local cache of pod statuses and provides
asyncio Events for waiters to block on until their pod is ready.
"""

import asyncio
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from kubernetes import client, config, watch
from kubernetes.client import ApiException

from orchard_env.orchestrator.settings import settings

logger = logging.getLogger(__name__)


@dataclass
class PodInfo:
    """Cached pod status information."""

    name: str
    sandbox_id: str
    phase: str = "Pending"
    ready: bool = False
    status: str = "pending"  # ready, pending, unschedulable, failed, not_found
    message: str = ""
    pod_ip: str = ""  # Pod IP for direct agent communication
    updated_at: float = field(default_factory=time.time)


class PodWatcher:
    """Watches pod events in the sandbox namespace using K8s Watch API.

    Provides:
    - Real-time pod status cache (no API calls to check status)
    - asyncio.Event-based notification for waiting on pod readiness
    - Automatic reconnection on watch failures
    """

    def __init__(self):
        self._cache: dict[str, PodInfo] = {}  # pod_name -> PodInfo
        self._ready_events: dict[str, asyncio.Event] = {}  # sandbox_id -> Event
        self._failed_sandboxes: set[str] = set()  # sandbox_ids that failed
        self._deleted_sandboxes: set[str] = (
            set()
        )  # sandbox_ids with confirmed DELETED events
        self._lock = asyncio.Lock()
        self._watch_task: asyncio.Task | None = None
        self._running = False
        self._callbacks: list[Callable] = []  # on_ready callbacks

        # K8s configuration
        if settings.in_cluster:
            config.load_incluster_config()
        else:
            config.load_kube_config()

        self._configuration = client.Configuration.get_default_copy()
        self._namespace = settings.sandbox_namespace

    async def start(self):
        """Start the pod watcher background task."""
        if self._running:
            return
        self._running = True
        self._watch_task = asyncio.create_task(self._watch_loop())
        logger.info(f"PodWatcher started for namespace {self._namespace}")

    async def stop(self):
        """Stop the pod watcher."""
        self._running = False
        if self._watch_task:
            self._watch_task.cancel()
            try:
                await self._watch_task
            except asyncio.CancelledError:
                pass
            self._watch_task = None
        logger.info("PodWatcher stopped")

    def get_pod_status(self, sandbox_id: str) -> dict | None:
        """Get cached pod status (zero API calls).

        Returns:
            Dict with keys: ready, phase, status, message, pod_ip
            or None if pod is not in cache.
        """
        pod_name = f"sandbox-{sandbox_id}"
        info = self._cache.get(pod_name)
        if info is None:
            return None
        return {
            "ready": info.ready,
            "phase": info.phase,
            "status": info.status,
            "message": info.message,
            "pod_ip": info.pod_ip,
        }

    def get_pod_ip(self, sandbox_id: str) -> str | None:
        """Get cached pod IP for direct agent communication.

        Returns:
            Pod IP string, or None if not available.
        """
        pod_name = f"sandbox-{sandbox_id}"
        info = self._cache.get(pod_name)
        if info and info.pod_ip:
            return info.pod_ip
        return None

    async def get_pod_ip_with_fallback(self, sandbox_id: str) -> str | None:
        """Get pod IP with K8s API fallback on cache miss.

        First checks local cache. If not found, queries the K8s API
        directly and writes the result back to cache for future lookups.

        Returns:
            Pod IP string, or None if pod doesn't exist or has no IP.
        """
        # Fast path: check cache first
        ip = self.get_pod_ip(sandbox_id)
        if ip:
            return ip

        # Cache miss — query K8s API directly
        pod_name = f"sandbox-{sandbox_id}"
        try:
            api_client = client.ApiClient(self._configuration)
            core_v1 = client.CoreV1Api(api_client)
            try:
                pod = await asyncio.to_thread(
                    core_v1.read_namespaced_pod,
                    name=pod_name,
                    namespace=self._namespace,
                    _request_timeout=(
                        settings.k8s_connect_timeout,
                        settings.k8s_api_timeout,
                    ),
                )
            finally:
                try:
                    api_client.close()
                except Exception:
                    pass

            if pod.status and pod.status.pod_ip:
                # Write back to cache using existing event processing logic
                self._process_pod_event("MODIFIED", pod)
                logger.info(
                    f"PodWatcher cache miss for {sandbox_id}, "
                    f"fetched from K8s API: pod_ip={pod.status.pod_ip}"
                )
                return pod.status.pod_ip

            logger.warning(f"Pod {pod_name} exists but has no IP yet")
            return None

        except ApiException as e:
            if e.status == 404:
                logger.warning(f"Pod {pod_name} not found in K8s API")
                return None
            logger.error(f"K8s API error looking up pod {pod_name}: {e}")
            return None
        except Exception as e:
            logger.warning(f"Failed to fetch pod IP from K8s API for {pod_name}: {e}")
            return None

    async def wait_for_ready(
        self,
        sandbox_id: str,
        timeout: float = 3600,
    ) -> dict:
        """Wait until a pod becomes ready, failed, or timeout.

        Uses asyncio.Event instead of polling — wakes up instantly
        when the Watch receives a status change event.

        Returns:
            Dict with keys: ready, phase, status, message

        Raises:
            TimeoutError if pod doesn't become ready within timeout.
        """
        # Loop until ready, failed, or timeout.
        # The event fires on ANY status change (e.g. Pending→Pending with new
        # message), so we must re-check and keep waiting if not terminal.
        deadline = asyncio.get_event_loop().time() + timeout
        event = self._get_or_create_event(sandbox_id)

        while True:
            # Check if pod was confirmed deleted
            if sandbox_id in self._deleted_sandboxes:
                return {
                    "ready": False,
                    "phase": "Unknown",
                    "status": "not_found",
                    "message": "Pod disappeared",
                }

            # Check current cache
            status = self.get_pod_status(sandbox_id)
            if status and status["ready"]:
                return status
            if status and status["status"] in ("failed", "not_found"):
                return status
            if sandbox_id in self._failed_sandboxes:
                return status or {
                    "ready": False,
                    "phase": "Failed",
                    "status": "failed",
                    "message": "Pod failed",
                }

            remaining = deadline - asyncio.get_event_loop().time()
            if remaining <= 0:
                raise TimeoutError(
                    f"Sandbox {sandbox_id} did not become ready within {timeout}s"
                )

            # Wait for next status change notification
            try:
                await asyncio.wait_for(event.wait(), timeout=remaining)
            except TimeoutError:
                # One last check before raising
                status = self.get_pod_status(sandbox_id)
                if status and status["ready"]:
                    return status
                raise TimeoutError(
                    f"Sandbox {sandbox_id} did not become ready within {timeout}s"
                )

            # Event fired — clear it so we can wait again on next iteration
            event.clear()

            # Check if pod was confirmed deleted (DELETED event received)
            if sandbox_id in self._deleted_sandboxes:
                return {
                    "ready": False,
                    "phase": "Unknown",
                    "status": "not_found",
                    "message": "Pod disappeared",
                }

            # status=None means this replica's PodWatcher hasn't observed
            # the pod yet (e.g. request routed to a different replica than
            # the one that created the pod).  Keep waiting — the ADDED
            # event will arrive shortly.

    def _get_or_create_event(self, sandbox_id: str) -> asyncio.Event:
        """Get or create an asyncio.Event for a sandbox."""
        if sandbox_id not in self._ready_events:
            self._ready_events[sandbox_id] = asyncio.Event()
        return self._ready_events[sandbox_id]

    def _notify_waiters(self, sandbox_id: str):
        """Notify any waiters that a sandbox status has changed."""
        event = self._ready_events.get(sandbox_id)
        if event:
            event.set()

    def remove_sandbox(self, sandbox_id: str):
        """Remove a sandbox from cache and events (called on delete)."""
        pod_name = f"sandbox-{sandbox_id}"
        self._cache.pop(pod_name, None)
        self._deleted_sandboxes.add(sandbox_id)
        event = self._ready_events.pop(sandbox_id, None)
        if event:
            event.set()  # Unblock any waiters
        self._failed_sandboxes.discard(sandbox_id)

    def prepare_sandbox(self, sandbox_id: str):
        """Clear stale state before creating a new pod with this sandbox ID."""
        pod_name = f"sandbox-{sandbox_id}"
        self._cache.pop(pod_name, None)
        event = self._ready_events.pop(sandbox_id, None)
        if event:
            event.set()
        self._failed_sandboxes.discard(sandbox_id)
        self._deleted_sandboxes.discard(sandbox_id)

    async def _watch_loop(self):
        """Main watch loop with automatic reconnection."""
        while self._running:
            try:
                await self._do_watch()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                if self._running:
                    logger.warning(
                        f"PodWatcher connection lost, reconnecting in 2s: {e}"
                    )
                    await asyncio.sleep(2)

    async def _do_watch(self):
        """Run a single watch session (LIST + WATCH).

        The K8s Watch API is a blocking HTTP call, so we run it in a thread
        to avoid blocking the asyncio event loop.
        """
        # Initial LIST to populate cache
        api_client = client.ApiClient(self._configuration)
        core_v1 = client.CoreV1Api(api_client)

        try:
            # List all pods with sandbox label
            pod_list = await asyncio.to_thread(
                core_v1.list_namespaced_pod,
                namespace=self._namespace,
                label_selector="app=sandbox",
                _request_timeout=(
                    settings.k8s_connect_timeout,
                    settings.k8s_api_timeout,
                ),
            )
        except Exception as e:
            logger.error(f"PodWatcher LIST failed: {e}")
            raise

        # Populate cache from LIST response
        resource_version = pod_list.metadata.resource_version
        for pod in pod_list.items:
            self._process_pod_event("MODIFIED", pod)
            sandbox_id = self._extract_sandbox_id(pod)
            if sandbox_id:
                self._notify_waiters(sandbox_id)

        logger.info(
            f"PodWatcher initial LIST: {len(pod_list.items)} pods cached, "
            f"resource_version={resource_version}"
        )

        # Start WATCH from the resource_version returned by LIST
        w = watch.Watch()

        try:
            # Run the blocking watch stream in a thread
            # timeout_seconds=300 means the API server will close the connection
            # after 5 minutes, then we reconnect with a fresh LIST
            stream = w.stream(
                core_v1.list_namespaced_pod,
                namespace=self._namespace,
                label_selector="app=sandbox",
                resource_version=resource_version,
                timeout_seconds=300,
                _request_timeout=(
                    settings.k8s_connect_timeout,
                    310,
                ),  # slightly longer than timeout_seconds
            )

            # Process events in a thread to avoid blocking
            # Pass the running loop so the thread can use call_soon_threadsafe
            loop = asyncio.get_running_loop()
            await asyncio.to_thread(self._consume_watch_stream, stream, w, loop)

        except ApiException as e:
            if e.status == 410:
                # 410 Gone — resource_version too old, need full re-list
                logger.info("PodWatcher received 410 Gone, will re-list")
            else:
                raise
        finally:
            w.stop()
            try:
                api_client.close()
            except Exception:
                pass

    def _consume_watch_stream(self, stream, w, loop):
        """Consume watch events (runs in thread).

        This is intentionally synchronous — it runs in a thread via to_thread.
        We use call_soon_threadsafe to notify asyncio Events from this thread.
        The loop is passed from the calling coroutine since get_event_loop()
        is not reliable from a background thread.
        """
        for event in stream:
            if not self._running:
                w.stop()
                return

            event_type = event.get("type", "")
            pod = event.get("object")

            if pod is None or not hasattr(pod, "metadata"):
                continue

            sandbox_id = self._extract_sandbox_id(pod)
            if sandbox_id is None:
                continue

            self._process_pod_event(event_type, pod)

            # Notify asyncio waiters from this thread on EVERY event
            # (wait_for_ready loops and rechecks, so intermediate events are fine)
            # Must use call_soon_threadsafe since asyncio.Event is not thread-safe
            if loop.is_running():
                loop.call_soon_threadsafe(self._notify_waiters, sandbox_id)

    def _extract_sandbox_id(self, pod) -> str | None:
        """Extract sandbox_id from pod metadata."""
        if pod.metadata and pod.metadata.labels:
            sid = pod.metadata.labels.get("sandbox-id")
            if sid:
                return sid
        # Fallback: parse from pod name
        if (
            pod.metadata
            and pod.metadata.name
            and pod.metadata.name.startswith("sandbox-")
        ):
            return pod.metadata.name[len("sandbox-") :]
        return None

    def _process_pod_event(self, event_type: str, pod):
        """Process a pod event and update cache."""
        if not pod.metadata or not pod.metadata.name:
            return

        pod_name = pod.metadata.name
        sandbox_id = self._extract_sandbox_id(pod)
        if sandbox_id is None:
            return

        if event_type == "DELETED":
            self._cache.pop(pod_name, None)
            self._failed_sandboxes.discard(sandbox_id)
            self._deleted_sandboxes.add(sandbox_id)
            return

        # ADDED/MODIFIED — pod exists, clear any previous deleted marker
        self._deleted_sandboxes.discard(sandbox_id)

        # Parse pod status
        phase = (pod.status.phase if pod.status else None) or "Unknown"
        ready = False
        status = "pending"
        message = ""
        pod_ip = ""

        # Extract Pod IP
        if pod.status and pod.status.pod_ip:
            pod_ip = pod.status.pod_ip

        if phase == "Running" and pod.status.container_statuses:
            all_ready = all(
                cs.ready for cs in pod.status.container_statuses if cs is not None
            )
            if all_ready:
                ready = True
                status = "ready"
            else:
                status = "pending"
                message = "Containers not ready"

        elif phase in ("Failed", "Unknown"):
            status = "failed"
            if pod.status and pod.status.container_statuses:
                for cs in pod.status.container_statuses:
                    if cs and cs.state and cs.state.terminated:
                        message = cs.state.terminated.reason or ""
                        break
            self._failed_sandboxes.add(sandbox_id)

        elif phase == "Pending":
            # Check for unschedulable
            if pod.status and pod.status.conditions:
                for cond in pod.status.conditions:
                    if cond.type == "PodScheduled" and cond.status == "False":
                        if cond.reason == "Unschedulable":
                            status = "unschedulable"
                            message = cond.message or "No nodes available"
                            break

        elif phase == "Succeeded":
            # Pod completed (unusual for sandboxes but handle it)
            status = "failed"
            message = "Pod exited"
            self._failed_sandboxes.add(sandbox_id)

        # Update cache
        info = PodInfo(
            name=pod_name,
            sandbox_id=sandbox_id,
            phase=phase,
            ready=ready,
            status=status,
            message=message,
            pod_ip=pod_ip,
            updated_at=time.time(),
        )
        old = self._cache.get(pod_name)
        self._cache[pod_name] = info

        # Log status changes
        old_status = old.status if old else None
        if old_status != status:
            logger.info(
                f"PodWatcher {event_type} {sandbox_id}: {old_status or 'new'}→{status} "
                f"(phase={phase}, ready={ready})"
            )

        # NOTE: Do NOT call _notify_waiters here — this method may be
        # called from a worker thread. Callers are responsible for
        # notification with proper thread safety (call_soon_threadsafe).
        return ready or status in ("failed", "not_found")
