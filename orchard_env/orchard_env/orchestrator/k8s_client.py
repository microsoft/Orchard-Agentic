"""Kubernetes client wrapper."""

import asyncio
import logging
import random
import re

import urllib3
from kubernetes import client, config
from kubernetes.client import ApiException
from kubernetes.stream import stream

from orchard_env.orchestrator.settings import settings

logger = logging.getLogger(__name__)


class K8sClient:
    """Kubernetes client wrapper."""

    # Transient error status codes that should be retried
    RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}

    # Pod phases that no longer consume resources
    TERMINAL_POD_PHASES = ("Succeeded", "Failed")

    def __init__(self):
        """Initialize Kubernetes client."""
        if settings.in_cluster:
            config.load_incluster_config()
            logger.info("Loaded in-cluster Kubernetes configuration")
        else:
            config.load_kube_config()
            logger.info("Loaded kubeconfig configuration")

        self.configuration = client.Configuration.get_default_copy()

        # Set connection pool size and timeouts to handle high concurrency
        pool_size = settings.k8s_api_pool_size
        self.configuration.connection_pool_maxsize = pool_size
        # Disable SSL verification retries at urllib3 level — we handle retries ourselves
        self.configuration.retries = urllib3.util.retry.Retry(total=0)

        # Shared API client for CRUD operations (connection pool reuse)
        self._shared_api_client = client.ApiClient(self.configuration)
        self._core_v1 = client.CoreV1Api(self._shared_api_client)
        self._networking_v1 = client.NetworkingV1Api(self._shared_api_client)
        self._apps_v1 = client.AppsV1Api(self._shared_api_client)

        # Semaphore to throttle concurrent K8s API calls
        self._api_semaphore = asyncio.Semaphore(settings.k8s_api_concurrency)
        # Separate semaphore for exec WebSocket connections (higher limit, long-lived)
        self._exec_semaphore = asyncio.Semaphore(settings.k8s_exec_concurrency)

        logger.info(
            f"K8s client initialized, API server: {self.configuration.host}, "
            f"pool_size={pool_size}, concurrency={settings.k8s_api_concurrency}, "
            f"exec_concurrency={settings.k8s_exec_concurrency}"
        )

    def _get_core_v1_api(self) -> client.CoreV1Api:
        """Get the shared CoreV1Api instance for CRUD operations."""
        return self._core_v1

    def _get_fresh_core_v1_api(self) -> client.CoreV1Api:
        """Create a fresh CoreV1Api for streaming/exec operations.

        Each exec call gets its own ApiClient because WebSocket connections
        are long-lived and would exhaust a shared connection pool.  The
        _exec_semaphore already limits concurrency, so the number of
        simultaneous TLS handshakes is bounded.
        """
        return client.CoreV1Api(client.ApiClient(self.configuration))

    def _get_networking_v1_api(self) -> client.NetworkingV1Api:
        """Get the shared NetworkingV1Api instance."""
        return self._networking_v1

    def _get_apps_v1_api(self) -> client.AppsV1Api:
        """Get the shared AppsV1Api instance."""
        return self._apps_v1

    async def _k8s_call(self, func, *args, **kwargs):
        """Execute a K8s API call with semaphore throttling, timeout, and retry.

        Wraps the synchronous K8s API call in asyncio.to_thread with:
        - Semaphore to limit concurrent calls
        - Configurable request timeout
        - Retry with exponential backoff for transient errors

        The semaphore is acquired PER ATTEMPT and released between retries.
        This prevents a single failing request from monopolizing a semaphore
        slot for minutes (connect_timeout × retries + backoff). The exponential
        backoff between attempts naturally prevents retry storms.
        """
        connect_timeout = settings.k8s_connect_timeout
        read_timeout = settings.k8s_api_timeout
        max_retries = settings.k8s_api_retries

        # Add _request_timeout if not already specified
        # Use (connect_timeout, read_timeout) tuple so TCP connect fails fast
        # while allowing adequate time for the API server to respond.
        if "_request_timeout" not in kwargs:
            kwargs["_request_timeout"] = (connect_timeout, read_timeout)

        for attempt in range(max_retries):
            try:
                async with self._api_semaphore:
                    return await asyncio.to_thread(func, *args, **kwargs)
            except ApiException as e:
                if (
                    e.status in self.RETRYABLE_STATUS_CODES
                    and attempt < max_retries - 1
                ):
                    wait = (2**attempt) + random.uniform(0, 1)
                    logger.warning(
                        f"K8s API call {func.__name__} returned {e.status}, "
                        f"retrying in {wait:.1f}s (attempt {attempt + 1}/{max_retries})"
                    )
                    await asyncio.sleep(wait)
                    continue
                raise
            except urllib3.exceptions.TimeoutError:
                if attempt < max_retries - 1:
                    wait = (2**attempt) + random.uniform(0, 1)
                    logger.warning(
                        f"K8s API call {func.__name__} timed out, "
                        f"retrying in {wait:.1f}s (attempt {attempt + 1}/{max_retries})"
                    )
                    await asyncio.sleep(wait)
                    continue
                raise
            except Exception as e:
                # urllib3 / http.client connection errors wrapped by kubernetes client
                err_str = str(e)
                is_transient = any(
                    keyword in err_str
                    for keyword in (
                        "Connection timed out",
                        "Max retries exceeded",
                        "RemoteDisconnected",
                        "Connection aborted",
                        "Connection refused",
                        "Connection reset",
                        "BrokenPipeError",
                        "ConnectionResetError",
                    )
                )
                if is_transient and attempt < max_retries - 1:
                    wait = (2**attempt) + random.uniform(0, 1)
                    logger.warning(
                        f"K8s API call {func.__name__} connection error, "
                        f"retrying in {wait:.1f}s (attempt {attempt + 1}/{max_retries}): {e}"
                    )
                    await asyncio.sleep(wait)
                    continue
                raise

    async def create_namespace(
        self, name: str, labels: dict[str, str] | None = None
    ) -> None:
        """Create a namespace."""
        namespace = client.V1Namespace(
            metadata=client.V1ObjectMeta(name=name, labels=labels or {})
        )

        try:
            core_v1 = self._get_core_v1_api()
            await self._k8s_call(core_v1.create_namespace, body=namespace)
            logger.info(f"Created namespace: {name}")
        except ApiException as e:
            if e.status == 409:
                logger.warning(f"Namespace {name} already exists")
            else:
                raise

    async def delete_namespace(self, name: str) -> None:
        """Delete a namespace."""
        try:
            core_v1 = self._get_core_v1_api()
            await self._k8s_call(
                core_v1.delete_namespace,
                name=name,
                body=client.V1DeleteOptions(
                    grace_period_seconds=0, propagation_policy="Background"
                ),
            )
            logger.info(f"Deleted namespace: {name}")
        except ApiException as e:
            if e.status == 404:
                logger.warning(f"Namespace {name} not found")
            else:
                raise

    async def namespace_exists(self, name: str) -> bool:
        """Check if namespace exists."""
        try:
            core_v1 = self._get_core_v1_api()
            await self._k8s_call(core_v1.read_namespace, name=name)
            return True
        except ApiException as e:
            if e.status == 404:
                return False
            raise

    async def create_network_policy(
        self,
        name: str,
        namespace: str,
        block_egress: bool = True,
        pod_labels: dict[str, str] | None = None,
    ) -> None:
        """Create a network policy.

        Args:
            name: Policy name
            namespace: Namespace
            block_egress: Whether to block egress
            pod_labels: If provided, policy targets only pods with these labels.
                       If None, targets all pods in the namespace.
        """
        # Pod selector: specific pod or all pods
        if pod_labels:
            pod_selector = client.V1LabelSelector(match_labels=pod_labels)
        else:
            pod_selector = client.V1LabelSelector()

        policy_spec = client.V1NetworkPolicySpec(
            pod_selector=pod_selector,
            policy_types=["Egress"],
        )

        if block_egress:
            # Empty egress list = deny all
            policy_spec.egress = []
        else:
            # Allow all egress
            policy_spec.egress = [client.V1NetworkPolicyEgressRule()]

        policy = client.V1NetworkPolicy(
            metadata=client.V1ObjectMeta(name=name), spec=policy_spec
        )

        try:
            networking_v1 = self._get_networking_v1_api()
            await self._k8s_call(
                networking_v1.create_namespaced_network_policy,
                namespace=namespace,
                body=policy,
            )
            logger.info(f"Created network policy: {name} in {namespace}")
        except ApiException as e:
            if e.status == 409:
                logger.warning(f"Network policy {name} already exists")
            else:
                raise

    async def delete_network_policy(self, name: str, namespace: str) -> None:
        """Delete a network policy."""
        try:
            networking_v1 = self._get_networking_v1_api()
            await self._k8s_call(
                networking_v1.delete_namespaced_network_policy,
                name=name,
                namespace=namespace,
            )
            logger.info(f"Deleted network policy: {name} in {namespace}")
        except ApiException as e:
            if e.status == 404:
                logger.warning(f"Network policy {name} not found")
            else:
                raise

    async def list_sandbox_pods(self, namespace: str) -> set | None:
        """List all sandbox pod IDs in a namespace.

        Returns set of sandbox IDs extracted from the 'sandbox-id' label,
        or None if the API call failed (callers must handle None to avoid
        mistakenly treating all pods as orphaned/missing).
        """
        try:
            core_v1 = self._get_core_v1_api()
            pods = await self._k8s_call(
                core_v1.list_namespaced_pod,
                namespace=namespace,
                label_selector="app=sandbox",
            )
            return {
                pod.metadata.labels.get("sandbox-id")
                for pod in pods.items
                if pod.metadata.labels and pod.metadata.labels.get("sandbox-id")
            }
        except ApiException as e:
            if e.status == 404:
                return set()
            logger.error(f"Error listing sandbox pods: {e}")
            return None

    @staticmethod
    def _build_startup_command(tools_mount_path: str | None) -> str:
        """Build the sandbox container's entrypoint script.

        Starts the in-pod agent and keeps the container alive. When the sandbox
        tools (`codex` / `claude` / `pi` / `opencode` / `hermes`) are mounted,
        they are published through three complementary paths, because different
        entry points into the container resolve commands differently:

        1. Small shims in /usr/local/bin — the only mechanism that also covers
           `kubectl exec`, which starts from the IMAGE's PATH and therefore never
           sees anything this script exports. Guarded so a tool already provided
           by the user's image is never shadowed.
        2. An exported PATH — inherited by the agent and everything it spawns.
        3. /etc/profile.d — for `bash --login`, which re-reads /etc/profile and
           would otherwise drop the exported PATH.

        The tools directory is APPENDED to PATH — never prepended — so any
        toolchain already present in the user's image keeps priority.
        """
        agent_start = "/opt/sandbox-agent/start.sh & sleep infinity"
        if not tools_mount_path:
            return agent_start

        # NOTE: the shim loop must run BEFORE PATH is extended, otherwise
        # `command -v` would find our own tools and skip every shim.
        # The shims exec an absolute path rather than symlinking, so they do not
        # depend on `readlink` being present in the user's image.
        return (
            f'TOOLS="{tools_mount_path}"; '
            'if [ -d "$TOOLS/bin" ]; then '
            "  mkdir -p /usr/local/bin 2>/dev/null; "
            "  for t in codex claude pi opencode hermes; do "
            '    command -v "$t" >/dev/null 2>&1 && continue; '
            '    { printf \'#!/bin/sh\\nexec "%s/bin/%s" "$@"\\n\' "$TOOLS" "$t" '
            '        > "/usr/local/bin/$t" && chmod 0755 "/usr/local/bin/$t"; } '
            "      2>/dev/null || true; "
            "  done; "
            '  PATH="$PATH:$TOOLS/bin"; export PATH; '
            '  if [ -f "$TOOLS/profile.sh" ]; then '
            "    mkdir -p /etc/profile.d 2>/dev/null && "
            '    ln -sf "$TOOLS/profile.sh" /etc/profile.d/sandbox-tools.sh 2>/dev/null; '
            "  fi; "
            "fi; "
            f"{agent_start}"
        )

    async def create_pod(
        self,
        name: str,
        namespace: str,
        image: str,
        command: list[str] | None = None,
        cpu: str = "4",
        memory: str = "16Gi",
        node_selector: dict[str, str] | None = None,
        working_dir: str = "/workspace",
        sandbox_id: str | None = None,
    ) -> None:
        """Create a pod.

        Args:
            sandbox_id: If provided, used as the 'sandbox-id' label value.
                       Otherwise defaults to the pod name.
        """
        resources = client.V1ResourceRequirements(
            requests={"cpu": cpu, "memory": memory},
            limits={"cpu": cpu, "memory": memory},
        )

        # Agent probes — pod is "Ready" only when the in-pod agent responds to
        # /health.  The startupProbe covers the agent boot window after the
        # container starts (readiness/liveness are suppressed while it runs),
        # avoiding early "connection refused" false not-ready.  The readiness
        # probe is relaxed so a momentarily busy agent (CPU pressure on a
        # packed node) is not flapped to not-ready on a single slow reply.
        agent_port = settings.agent_port
        startup_probe = client.V1Probe(
            http_get=client.V1HTTPGetAction(
                path="/health",
                port=agent_port,
            ),
            period_seconds=settings.agent_startup_period_seconds,
            timeout_seconds=settings.agent_startup_timeout_seconds,
            failure_threshold=settings.agent_startup_failure_threshold,
        )
        readiness_probe = client.V1Probe(
            http_get=client.V1HTTPGetAction(
                path="/health",
                port=agent_port,
            ),
            period_seconds=settings.agent_readiness_period_seconds,
            timeout_seconds=settings.agent_readiness_timeout_seconds,
            failure_threshold=settings.agent_readiness_failure_threshold,
            success_threshold=1,
        )

        # Shared volume: init container copies the agent here, main container mounts it.
        agent_volume_name = "sandbox-agent"
        agent_volume = client.V1Volume(
            name=agent_volume_name,
            empty_dir=client.V1EmptyDirVolumeSource(),
        )
        agent_volume_mount = client.V1VolumeMount(
            name=agent_volume_name,
            mount_path="/opt/sandbox-agent",
        )

        # Init container: copies bundled Python + agent to the shared volume
        init_container = client.V1Container(
            name="agent-injector",
            image=settings.agent_injector_image,
            image_pull_policy="Always",
            volume_mounts=[
                client.V1VolumeMount(
                    name=agent_volume_name,
                    mount_path="/agent-volume",
                ),
            ],
        )

        init_containers = [init_container]
        volumes = [agent_volume]
        container_volume_mounts = [agent_volume_mount]

        # Sandbox tools (`codex` / `claude`). Both are self-contained native
        # binaries, so they work in ANY user image with no install step and no
        # network access inside the sandbox.
        tools_mount_path = settings.sandbox_tools_mount_path
        tools_enabled = settings.enable_sandbox_tools
        if tools_enabled:
            tools_volume_name = "sandbox-tools"
            # An `image:` volume exposes the WHOLE image filesystem, so the
            # payload must be selected with a subPath. The initcontainer variant
            # copies the payload to the volume root instead, so it needs none.
            tools_sub_path = None
            if settings.sandbox_tools_volume_mode == "image":
                # Read-only OCI image mount: no per-pod copy, the kubelet pulls
                # the image once per node. Requires k8s >= 1.33.
                volumes.append(
                    client.V1Volume(
                        name=tools_volume_name,
                        image=client.V1ImageVolumeSource(
                            reference=settings.sandbox_tools_image,
                            pull_policy="IfNotPresent",
                        ),
                    )
                )
                tools_sub_path = settings.sandbox_tools_mount_path.lstrip("/")
            else:
                # Portable fallback: an init container copies the payload into a
                # shared emptyDir. Costs ~600 MB of ephemeral disk per pod.
                volumes.append(
                    client.V1Volume(
                        name=tools_volume_name,
                        empty_dir=client.V1EmptyDirVolumeSource(),
                    )
                )
                init_containers.append(
                    client.V1Container(
                        name="tools-injector",
                        image=settings.sandbox_tools_image,
                        image_pull_policy="IfNotPresent",
                        volume_mounts=[
                            client.V1VolumeMount(
                                name=tools_volume_name,
                                mount_path="/tools-volume",
                            ),
                        ],
                    )
                )
            container_volume_mounts.append(
                client.V1VolumeMount(
                    name=tools_volume_name,
                    mount_path=tools_mount_path,
                    sub_path=tools_sub_path,
                    read_only=True,
                )
            )

        container = client.V1Container(
            name="sandbox",
            image=image,
            command=command
            or [
                "sh",
                "-c",
                self._build_startup_command(
                    tools_mount_path if tools_enabled else None
                ),
            ],
            resources=resources,
            working_dir=working_dir,
            image_pull_policy="IfNotPresent",
            # Override any AGENT_PORT baked into an arbitrary user image. The
            # agent port is part of Orchard's trusted control plane and must
            # match the port reserved from service exposure.
            env=[
                client.V1EnvVar(name="AGENT_PORT", value=str(agent_port)),
            ],
            ports=[client.V1ContainerPort(container_port=agent_port, name="agent")],
            startup_probe=startup_probe,
            readiness_probe=readiness_probe,
            volume_mounts=container_volume_mounts,
        )

        # Add toleration for sandbox node taint
        tolerations = [
            client.V1Toleration(
                key="workload", operator="Equal", value="sandbox", effect="NoSchedule"
            )
        ]

        pod_spec = client.V1PodSpec(
            init_containers=init_containers,
            containers=[container],
            volumes=volumes,
            restart_policy="Never",
            node_selector=node_selector or {},
            tolerations=tolerations,
        )

        pod = client.V1Pod(
            metadata=client.V1ObjectMeta(
                name=name, labels={"app": "sandbox", "sandbox-id": sandbox_id or name}
            ),
            spec=pod_spec,
        )

        try:
            core_v1 = self._get_core_v1_api()
            await self._k8s_call(
                core_v1.create_namespaced_pod, namespace=namespace, body=pod
            )
            logger.info(f"Created pod: {name} in {namespace}")
        except ApiException as e:
            if e.status == 409:
                logger.warning(f"Pod {name} already exists")
            else:
                raise

    async def wait_pod_ready(
        self, name: str, namespace: str, timeout: int = 300
    ) -> bool:
        """Wait for pod to be ready."""
        start_time = asyncio.get_event_loop().time()

        while True:
            try:
                core_v1 = self._get_core_v1_api()
                pod = await self._k8s_call(
                    core_v1.read_namespaced_pod, name=name, namespace=namespace
                )

                if pod.status.phase == "Running":
                    # Check if all containers are ready
                    if pod.status.container_statuses:
                        all_ready = all(
                            cs.ready for cs in pod.status.container_statuses
                        )
                        if all_ready:
                            logger.info(f"Pod {name} is ready")
                            return True

                elif pod.status.phase in ["Failed", "Unknown"]:
                    logger.error(f"Pod {name} is in {pod.status.phase} state")
                    return False

            except ApiException as e:
                if e.status == 404:
                    logger.error(f"Pod {name} not found")
                    return False
                raise

            elapsed = asyncio.get_event_loop().time() - start_time
            if elapsed > timeout:
                logger.error(f"Timeout waiting for pod {name} to be ready")
                return False

            await asyncio.sleep(2)

    async def get_pod(self, name: str, namespace: str) -> client.V1Pod | None:
        """Get a pod by name and namespace.

        Returns:
            V1Pod object or None if not found.
        """
        try:
            core_v1 = self._get_core_v1_api()
            pod = await self._k8s_call(
                core_v1.read_namespaced_pod, name=name, namespace=namespace
            )
            return pod
        except ApiException as e:
            if e.status == 404:
                return None
            raise

    async def has_network_policy(self, namespace: str, name: str) -> bool:
        """Check if a network policy exists in namespace.

        Returns:
            True if network policy exists, False otherwise.
        """
        try:
            networking_v1 = self._get_networking_v1_api()
            await self._k8s_call(
                networking_v1.read_namespaced_network_policy,
                name=name,
                namespace=namespace,
            )
            return True
        except ApiException as e:
            if e.status == 404:
                return False
            raise

    async def check_pod_ready(self, name: str, namespace: str) -> bool:
        """Check if pod is ready (non-blocking, single check).

        Returns:
            True if pod is running and all containers ready, False otherwise.
        """
        try:
            core_v1 = self._get_core_v1_api()
            pod = await self._k8s_call(
                core_v1.read_namespaced_pod, name=name, namespace=namespace
            )

            if pod.status.phase == "Running":
                if pod.status.container_statuses:
                    return all(cs.ready for cs in pod.status.container_statuses)
            return False

        except ApiException as e:
            if e.status == 404:
                return False
            raise

    async def check_pod_status(self, name: str, namespace: str) -> dict:
        """Check pod status with scheduling details (non-blocking, single check).

        Returns:
            Dict with keys: ready (bool), phase (str), status (str), message (str)
            status is one of: ready, pending, unschedulable, failed, not_found
        """
        try:
            core_v1 = self._get_core_v1_api()
            pod = await self._k8s_call(
                core_v1.read_namespaced_pod, name=name, namespace=namespace
            )

            phase = pod.status.phase or "Unknown"

            if phase == "Running":
                if pod.status.container_statuses:
                    all_ready = all(cs.ready for cs in pod.status.container_statuses)
                    if all_ready:
                        return {
                            "ready": True,
                            "phase": phase,
                            "status": "ready",
                            "message": "",
                        }
                return {
                    "ready": False,
                    "phase": phase,
                    "status": "pending",
                    "message": "Containers not ready",
                }

            if phase in ("Failed", "Unknown"):
                message = ""
                if pod.status.container_statuses:
                    for cs in pod.status.container_statuses:
                        if cs.state and cs.state.terminated:
                            message = cs.state.terminated.reason or ""
                return {
                    "ready": False,
                    "phase": phase,
                    "status": "failed",
                    "message": message,
                }

            # Pending phase — check if unschedulable
            if pod.status.conditions:
                for cond in pod.status.conditions:
                    if cond.type == "PodScheduled" and cond.status == "False":
                        if cond.reason == "Unschedulable":
                            return {
                                "ready": False,
                                "phase": phase,
                                "status": "unschedulable",
                                "message": cond.message or "No nodes available",
                            }

            return {"ready": False, "phase": phase, "status": "pending", "message": ""}

        except ApiException as e:
            if e.status == 404:
                return {
                    "ready": False,
                    "phase": "NotFound",
                    "status": "not_found",
                    "message": "Pod not found",
                }
            raise

    async def delete_pod(
        self, name: str, namespace: str, grace_period_seconds: int | None = None
    ) -> None:
        """Delete a pod.

        Args:
            grace_period_seconds: Override the pod's default termination
                grace period. Use 0 for immediate termination (SIGKILL).
        """
        try:
            delete_opts = client.V1DeleteOptions()
            if grace_period_seconds is not None:
                delete_opts.grace_period_seconds = grace_period_seconds
            core_v1 = self._get_core_v1_api()
            await self._k8s_call(
                core_v1.delete_namespaced_pod,
                name=name,
                namespace=namespace,
                body=delete_opts,
            )
            logger.info(f"Deleted pod: {name} in {namespace}")
        except ApiException as e:
            if e.status == 404:
                logger.warning(f"Pod {name} not found")
            else:
                raise

    async def exec_command(
        self,
        pod_name: str,
        namespace: str,
        command: list[str],
        timeout: int | None = 300,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        login_shell: bool = False,
    ) -> tuple[str, str, int]:
        """
        Execute a command in a pod and return stdout, stderr, and exit code.

        Uses the stderr channel exit code marker to reliably get the exit code.
        Implements robust timeout handling with process cleanup.

        Args:
            timeout: Timeout in seconds. If None, no timeout is enforced (wait indefinitely).
                    If 0 or negative, uses default timeout of 300s.
            login_shell: If True, use 'bash -lc' (login shell) instead of 'bash -c'.
                        Login shell sources /etc/profile and ~/.bash_profile.
        """
        # Validate and normalize timeout
        if timeout is None:
            # None means no timeout - wait indefinitely
            use_timeout = False
            timeout_seconds = 3600  # Still use a large value for k8s request timeout
            logger.info(
                f"Executing command in {pod_name} with no timeout (indefinite wait)"
            )
        elif timeout <= 0:
            # Invalid timeout - use default
            timeout_seconds = 300
            use_timeout = True
            logger.warning(f"Invalid timeout {timeout}, using default 300s")
        else:
            timeout_seconds = timeout
            use_timeout = True

        # Build the shell command
        shell_command_parts = []

        # Change directory if needed
        if cwd:
            shell_command_parts.append(f"cd {cwd}")

        # Set environment variables if needed
        if env:
            for key, value in env.items():
                shell_command_parts.append(f"export {key}={value}")

        # Add the actual command
        if isinstance(command, list):
            command = " ".join(command)

        # Escape single quotes in the command for proper shell quoting
        # Replace ' with '\'' to safely embed in single-quoted string
        command_escaped = command.replace("'", "'\\''")

        # Determine shell flag: -lc for login shell, -c for regular
        shell_flag = "-lc" if login_shell else "-c"

        # Wrap command with timeout if enabled
        if use_timeout:
            # Use timeout command with SIGTERM, then SIGKILL after 5s grace period
            shell_command_parts.append(
                f"timeout --kill-after=5s {timeout_seconds}s bash {shell_flag} '{command_escaped}'"
            )
            timeout_with_buffer = timeout_seconds + 10
        else:
            # No timeout wrapping - run command directly
            shell_command_parts.append(f"bash {shell_flag} '{command_escaped}'")
            timeout_with_buffer = timeout_seconds

        # Join with && and add exit code capture
        full_shell_command = " && ".join(shell_command_parts)
        full_shell_command += "; echo __EXIT_CODE__: $? >&2"

        # Always use bash -c for the outer wrapper to avoid double login shell overhead.
        # The inner command already uses the appropriate shell_flag (bash -lc or bash -c).
        wrapped_command = ["bash", "-c", full_shell_command]

        if use_timeout:
            logger.info(
                f"Executing command in {pod_name} with {timeout_seconds}s timeout: {wrapped_command}"
            )
        else:
            logger.info(
                f"Executing command in {pod_name} with no timeout: {wrapped_command}"
            )

        try:
            # Use asyncio.wait_for to enforce timeout at Python level (if timeout enabled)
            if use_timeout:
                result = await asyncio.wait_for(
                    self._do_exec(
                        pod_name, namespace, wrapped_command, timeout_with_buffer
                    ),
                    timeout=timeout_with_buffer,
                )
            else:
                # No timeout enforcement at Python level
                result = await self._do_exec(
                    pod_name, namespace, wrapped_command, timeout_seconds
                )
            return result

        except TimeoutError:
            logger.error(f"Command timed out in {pod_name} after {timeout_seconds}s")
            # Try to kill any remaining processes (best effort)
            await self._cleanup_processes(pod_name, namespace)
            raise

        except Exception as e:
            logger.error(f"Error executing command in {pod_name}: {e}")
            raise

    def _do_exec_sync(
        self,
        pod_name: str,
        namespace: str,
        wrapped_command: list[str],
        timeout: int,
    ) -> tuple[str, str, int]:
        """Synchronous method to execute command via Kubernetes stream.

        This runs entirely in a thread to avoid blocking the asyncio event loop.
        The previous implementation only wrapped the stream() call in to_thread
        but left the blocking resp.update(timeout=1) read loop in the event loop,
        which serialized all concurrent exec operations.

        Uses a short _request_timeout for the WebSocket connect handshake
        (not the command execution) so we fail fast if the API server is overloaded.
        The resp.update(timeout=1) loop handles reading with its own polling timeout.
        """
        core_v1 = self._get_fresh_core_v1_api()
        # Use short connect timeout for WebSocket handshake, not the full command timeout.
        # After connect, resp.update(timeout=1) polls for data with its own timeout.
        connect_timeout = settings.k8s_connect_timeout
        resp = stream(
            core_v1.connect_get_namespaced_pod_exec,
            pod_name,
            namespace,
            command=wrapped_command,
            container="sandbox",
            stderr=True,
            stdin=False,
            stdout=True,
            tty=False,
            _preload_content=False,
            _request_timeout=connect_timeout,
        )

        stdout_data = []
        stderr_data = []

        # Read from the stream until it closes
        while resp.is_open():
            resp.update(timeout=1)

            if resp.peek_stdout():
                stdout_data.append(resp.read_stdout())

            if resp.peek_stderr():
                stderr_data.append(resp.read_stderr())

        # Ensure we read any remaining data after stream closes
        while resp.peek_stdout():
            stdout_data.append(resp.read_stdout())

        while resp.peek_stderr():
            stderr_data.append(resp.read_stderr())

        # Close the response
        resp.close()

        stdout = "".join(stdout_data)
        stderr = "".join(stderr_data)

        # Extract exit code from stderr
        exit_code = 0
        match = re.search(r"__EXIT_CODE__: (\d+)", stderr)
        if match:
            exit_code = int(match.group(1))
            # Remove the exit code marker from stderr
            stderr = re.sub(r"__EXIT_CODE__: \d+\n?", "", stderr)

        # Check if command was killed by timeout (exit code 124 for timeout command)
        if exit_code == 124:
            logger.warning(f"Command in {pod_name} was terminated by timeout")
            stderr = f"Command timed out and was terminated\n{stderr}"

        logger.info(
            f"Command completed in {pod_name}: exit_code={exit_code}, "
            f"stdout_len={len(stdout)}, stderr_len={len(stderr)}"
        )

        return stdout, stderr, exit_code

    async def _do_exec(
        self,
        pod_name: str,
        namespace: str,
        wrapped_command: list[str],
        timeout: int,
    ) -> tuple[str, str, int]:
        """Internal method to execute command via Kubernetes stream.

        Runs the entire stream creation + read loop in a thread to avoid
        blocking the asyncio event loop during concurrent operations.
        Uses exec semaphore to limit concurrent WebSocket connections and
        retries on connection timeouts.

        The semaphore is acquired PER ATTEMPT and released between retries.
        This prevents a single failing exec from monopolizing a semaphore
        slot during backoff sleep, leaving capacity for other execs.
        """
        max_retries = settings.k8s_exec_retries
        for attempt in range(max_retries):
            try:
                async with self._exec_semaphore:
                    return await asyncio.to_thread(
                        self._do_exec_sync,
                        pod_name,
                        namespace,
                        wrapped_command,
                        timeout,
                    )
            except ApiException as e:
                err_str = str(e)
                is_transient = (
                    e.status == 0
                    and any(
                        kw in err_str
                        for kw in (
                            "Connection timed out",
                            "RemoteDisconnected",
                            "Connection aborted",
                            "Connection reset",
                        )
                    )
                ) or e.status in self.RETRYABLE_STATUS_CODES
                if is_transient and attempt < max_retries - 1:
                    wait = (2**attempt) + random.uniform(0, 1)
                    logger.warning(
                        f"Exec connection to {pod_name} failed (attempt {attempt + 1}/{max_retries}), "
                        f"retrying in {wait:.1f}s: {e.reason}"
                    )
                    await asyncio.sleep(wait)
                    continue
                raise
            except (TimeoutError, ConnectionError, OSError) as e:
                if attempt < max_retries - 1:
                    wait = (2**attempt) + random.uniform(0, 1)
                    logger.warning(
                        f"Exec connection to {pod_name} failed (attempt {attempt + 1}/{max_retries}), "
                        f"retrying in {wait:.1f}s: {e}"
                    )
                    await asyncio.sleep(wait)
                    continue
                raise

    async def _cleanup_processes(
        self,
        pod_name: str,
        namespace: str,
    ) -> None:
        """Best-effort cleanup of orphaned processes after timeout."""
        try:
            logger.info(f"Attempting to cleanup processes in {pod_name}")
            # Kill any bash processes that might be hanging
            cleanup_cmd = ["bash", "-c", "pkill -9 -f 'bash -c' || true"]
            core_v1 = self._get_fresh_core_v1_api()
            await asyncio.to_thread(
                stream,
                core_v1.connect_get_namespaced_pod_exec,
                pod_name,
                namespace,
                command=cleanup_cmd,
                container="sandbox",
                stderr=False,
                stdin=False,
                stdout=False,
                tty=False,
            )
            logger.info(f"Cleanup attempted for {pod_name}")
        except Exception as e:
            logger.warning(f"Failed to cleanup processes in {pod_name}: {e}")

    async def get_namespace_creation_time(self, name: str) -> float | None:
        """Get namespace creation timestamp."""
        try:
            core_v1 = self._get_core_v1_api()
            namespace = await self._k8s_call(core_v1.read_namespace, name=name)
            if namespace.metadata.creation_timestamp:
                return namespace.metadata.creation_timestamp.timestamp()
            return None
        except ApiException as e:
            if e.status == 404:
                return None
            raise

    async def list_namespaces_with_prefix(self, prefix: str) -> list[str]:
        """List all namespaces with a given prefix."""
        try:
            core_v1 = self._get_core_v1_api()
            namespaces = await self._k8s_call(core_v1.list_namespace)
            return [
                ns.metadata.name
                for ns in namespaces.items
                if ns.metadata.name.startswith(prefix)
            ]
        except Exception as e:
            logger.error(f"Error listing namespaces: {e}")
            return []

    async def upload_file(
        self,
        pod_name: str,
        namespace: str,
        file_content: bytes,
        remote_path: str,
        container: str = "sandbox",
    ) -> None:
        """
        Upload file content to a pod.

        Uses base64 encoding to safely transfer binary data.

        Args:
            pod_name: Name of the pod
            namespace: Namespace of the pod
            file_content: File content as bytes
            remote_path: Destination path in the pod
            container: Container name
        """
        import base64

        # Encode file content to base64
        encoded_content = base64.b64encode(file_content).decode("utf-8")

        # Create directory if needed and write file
        # Use printf to avoid issues with special characters
        remote_dir = "/".join(remote_path.rsplit("/", 1)[:-1]) or "/"

        # Build command to create dir and decode base64 to file
        command = [
            "bash",
            "-c",
            f"mkdir -p '{remote_dir}' && echo '{encoded_content}' | base64 -d > '{remote_path}'",
        ]

        core_v1 = self._get_fresh_core_v1_api()

        try:
            async with self._exec_semaphore:
                await asyncio.to_thread(
                    stream,
                    core_v1.connect_get_namespaced_pod_exec,
                    pod_name,
                    namespace,
                    command=command,
                    container=container,
                    stderr=True,
                    stdin=False,
                    stdout=True,
                    tty=False,
                )
            logger.info(f"Uploaded file to {remote_path} in {pod_name}")
        except Exception as e:
            logger.error(f"Failed to upload file to {pod_name}: {e}")
            raise

    async def download_file(
        self,
        pod_name: str,
        namespace: str,
        remote_path: str,
        container: str = "sandbox",
    ) -> bytes:
        """
        Download file content from a pod.

        Uses base64 encoding to safely transfer binary data.

        Args:
            pod_name: Name of the pod
            namespace: Namespace of the pod
            remote_path: Source path in the pod
            container: Container name

        Returns:
            File content as bytes
        """
        import base64

        # Read file and encode to base64
        command = ["bash", "-c", f"base64 '{remote_path}'"]

        core_v1 = self._get_fresh_core_v1_api()

        try:
            async with self._exec_semaphore:
                resp = await asyncio.to_thread(
                    stream,
                    core_v1.connect_get_namespaced_pod_exec,
                    pod_name,
                    namespace,
                    command=command,
                    container=container,
                    stderr=True,
                    stdin=False,
                    stdout=True,
                    tty=False,
                )

            # Decode base64 response
            content = base64.b64decode(resp.strip())
            logger.info(
                f"Downloaded file from {remote_path} in {pod_name}, size: {len(content)} bytes"
            )
            return content

        except Exception as e:
            logger.error(f"Failed to download file from {pod_name}: {e}")
            raise

    async def list_files(
        self,
        pod_name: str,
        namespace: str,
        remote_path: str,
        container: str = "sandbox",
    ) -> list[dict[str, str]]:
        """
        List files in a directory in the pod.

        Args:
            pod_name: Name of the pod
            namespace: Namespace of the pod
            remote_path: Directory path in the pod
            container: Container name

        Returns:
            List of file info dicts with name, type, size, modified
        """
        # Use ls with specific format for parsing
        command = [
            "bash",
            "-c",
            f"ls -la --time-style=+%Y-%m-%dT%H:%M:%S '{remote_path}' 2>/dev/null || echo 'ERROR: Path not found'",
        ]

        core_v1 = self._get_fresh_core_v1_api()

        try:
            async with self._exec_semaphore:
                resp = await asyncio.to_thread(
                    stream,
                    core_v1.connect_get_namespaced_pod_exec,
                    pod_name,
                    namespace,
                    command=command,
                    container=container,
                    stderr=True,
                    stdin=False,
                    stdout=True,
                    tty=False,
                )

            if "ERROR:" in resp:
                raise FileNotFoundError(f"Path not found: {remote_path}")

            files = []
            for line in resp.strip().split("\n"):
                # Skip header and total lines
                if line.startswith("total") or not line.strip():
                    continue

                parts = line.split()
                if len(parts) >= 8:
                    perms = parts[0]
                    size = parts[4]
                    modified = parts[5]
                    name = " ".join(parts[6:])

                    # Skip . and ..
                    if name in [".", ".."]:
                        continue

                    file_type = "directory" if perms.startswith("d") else "file"
                    files.append(
                        {
                            "name": name,
                            "type": file_type,
                            "size": size,
                            "modified": modified,
                        }
                    )

            return files

        except Exception as e:
            logger.error(f"Failed to list files in {pod_name}: {e}")
            raise

    @staticmethod
    def _parse_cpu(value: str) -> float:
        """Parse a Kubernetes CPU quantity to number of cores (float).

        Handles plain integers/floats and milli-cpu notation (e.g. "500m").
        """
        if value.endswith("m"):
            return float(value[:-1]) / 1000.0
        return float(value)

    @staticmethod
    def _parse_memory(value: str) -> int:
        """Parse a Kubernetes memory quantity to bytes (int).

        Supports Ki, Mi, Gi, Ti and k, M, G, T suffixes as well as plain
        byte values.
        """
        units = {
            "Ki": 1024,
            "Mi": 1024**2,
            "Gi": 1024**3,
            "Ti": 1024**4,
            "k": 1000,
            "M": 1000**2,
            "G": 1000**3,
            "T": 1000**4,
        }
        for suffix, multiplier in units.items():
            if value.endswith(suffix):
                return int(float(value[: -len(suffix)]) * multiplier)
        return int(value)

    async def get_cluster_resources(self) -> dict:
        """Return cluster-wide resource summary.

        Aggregates:
        * Per-node capacity and allocatable CPU / memory.
        * Total requested CPU / memory across all pods in the sandbox
          namespace.
        * Available (allocatable minus requested) resources.
        """
        core_v1 = self._get_core_v1_api()

        # --- Nodes ---
        nodes_resp = await self._k8s_call(core_v1.list_node)
        nodes = []
        total_capacity_cpu = 0.0
        total_capacity_memory = 0
        total_allocatable_cpu = 0.0
        total_allocatable_memory = 0

        for node in nodes_resp.items:
            cap = node.status.capacity or {}
            alloc = node.status.allocatable or {}

            cap_cpu = self._parse_cpu(cap.get("cpu", "0"))
            cap_mem = self._parse_memory(cap.get("memory", "0"))
            alloc_cpu = self._parse_cpu(alloc.get("cpu", "0"))
            alloc_mem = self._parse_memory(alloc.get("memory", "0"))

            total_capacity_cpu += cap_cpu
            total_capacity_memory += cap_mem
            total_allocatable_cpu += alloc_cpu
            total_allocatable_memory += alloc_mem

            nodes.append(
                {
                    "name": node.metadata.name,
                    "capacity": {"cpu": cap_cpu, "memory_bytes": cap_mem},
                    "allocatable": {"cpu": alloc_cpu, "memory_bytes": alloc_mem},
                }
            )

        # --- Pods (sandbox namespace) ---
        sandbox_ns = settings.sandbox_namespace
        try:
            pods_resp = await self._k8s_call(
                core_v1.list_namespaced_pod,
                namespace=sandbox_ns,
            )
            pods = pods_resp.items
        except ApiException as e:
            if e.status == 404:
                pods = []
            else:
                raise

        total_requested_cpu = 0.0
        total_requested_memory = 0
        pod_resources: list[dict] = []

        for pod in pods:
            phase = pod.status.phase if pod.status else "Unknown"
            # Skip completed / failed pods that no longer consume resources
            if phase in self.TERMINAL_POD_PHASES:
                continue
            pod_cpu = 0.0
            pod_mem = 0
            for container in pod.spec.containers or []:
                requests = {}
                if container.resources and container.resources.requests:
                    requests = container.resources.requests
                pod_cpu += self._parse_cpu(requests.get("cpu", "0"))
                pod_mem += self._parse_memory(requests.get("memory", "0"))
            total_requested_cpu += pod_cpu
            total_requested_memory += pod_mem
            pod_resources.append(
                {
                    "name": pod.metadata.name,
                    "namespace": sandbox_ns,
                    "phase": phase,
                    "cpu_request": pod_cpu,
                    "memory_request_bytes": pod_mem,
                }
            )

        available_cpu = max(total_allocatable_cpu - total_requested_cpu, 0.0)
        available_memory = max(total_allocatable_memory - total_requested_memory, 0)

        return {
            "nodes": nodes,
            "total_capacity": {
                "cpu": total_capacity_cpu,
                "memory_bytes": total_capacity_memory,
            },
            "total_allocatable": {
                "cpu": total_allocatable_cpu,
                "memory_bytes": total_allocatable_memory,
            },
            "total_requested": {
                "cpu": total_requested_cpu,
                "memory_bytes": total_requested_memory,
            },
            "available": {
                "cpu": available_cpu,
                "memory_bytes": available_memory,
            },
            "sandbox_pods": pod_resources,
        }
