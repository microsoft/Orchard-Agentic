"""Configuration settings for the orchestrator."""

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    """Application settings."""

    # Service settings
    service_name: str = "sandbox-orchestrator"
    host: str = "0.0.0.0"
    port: int = 8000

    # API Key Authentication
    api_keys: str | None = None  # Comma-separated list of valid API keys
    require_api_key: bool = True  # Set to False to disable auth (for internal use)

    # Kubernetes settings
    in_cluster: bool = True
    namespace_prefix: str = "sbx-"
    orchestrator_namespace: str = "orchestrator"
    sandbox_namespace: str = "sandbox-pods"  # Shared namespace for all sandbox pods

    # Sandbox settings
    default_cpu: str = "4"
    default_memory: str = "16Gi"
    default_working_dir: str = "/workspace"
    default_block_network: bool = True
    sandbox_node_selector: dict = {"workload": "sandbox"}

    # Execution settings
    max_concurrent_execs: int = 400
    default_timeout_seconds: int = 300

    # K8s API throttling
    k8s_api_concurrency: int = 100  # Max concurrent K8s API calls per replica
    k8s_connect_timeout: int = 10  # TCP connect timeout for K8s API calls (seconds)
    k8s_api_timeout: int = 30  # Read timeout for K8s API calls (seconds)
    k8s_api_retries: int = (
        3  # Retry transient K8s API errors (keep low to release slots fast)
    )
    k8s_api_pool_size: int = 100  # urllib3 connection pool size for K8s API
    k8s_exec_concurrency: int = (
        200  # Max concurrent exec WebSocket connections per replica
    )
    k8s_exec_retries: int = 3  # Retry exec connection timeouts
    max_concurrent_creates: int = 50  # Max concurrent sandbox creates per replica

    # Agent settings (in-pod HTTP agent for exec / file operations)
    agent_port: int = 9090  # Port the sandbox agent listens on
    agent_connect_timeout: int = 5  # TCP connect timeout to agent (seconds)
    agent_pool_size: int = 500  # aiohttp connection pool size for agent calls
    agent_injector_image: str = (
        "sandbox-agent-injector:latest"  # Init container image that injects agent into any sandbox
    )

    # Sandbox tools — the `codex` and `claude` CLIs, made available in EVERY
    # sandbox regardless of the user's base image. Both ship as self-contained
    # native binaries, so nothing is installed inside the sandbox and no network
    # access is required there.
    #
    # Credentials are deliberately NOT baked in; supply them per call, e.g.
    #   sandbox.exec("codex exec ...", env={"OPENAI_API_KEY": "..."})
    enable_sandbox_tools: bool = True  # Set False to omit the CLIs entirely
    sandbox_tools_image: str = "sandbox-tools:latest"
    sandbox_tools_mount_path: str = (
        "/opt/sandbox-tools"  # Must match the path baked into the image
    )
    # How the payload reaches the sandbox:
    #   "image"         — mount the image read-only via an `image:` volume source.
    #                     No per-pod copy; the kubelet pulls once per node.
    #                     Requires k8s >= 1.33 and containerd >= 2.0.
    #   "initcontainer" — portable fallback: an init container copies the payload
    #                     into a shared emptyDir (~600 MB of ephemeral disk per pod).
    sandbox_tools_volume_mode: str = "image"

    # Sandbox service endpoints — proxy HTTP/WebSocket traffic to a user
    # service running inside a sandbox (an OpenEnv environment server, an MCP
    # server, a dev server). Disabled by default: it deliberately opens a hole
    # in the sandbox boundary, so an operator has to ask for it.
    enable_service_endpoints: bool = False
    # Extra ports that may never be exposed, on top of agent_port which is
    # always refused. Comma- or space-separated.
    service_reserved_ports: str = ""
    # Maximum number of ports a single sandbox may expose at once.
    max_services_per_sandbox: int = 8
    # Lifetime of a capability token. Short by default: the URL is a bearer
    # credential, and callers can mint a new one whenever they need it.
    service_token_ttl_seconds: int = 3600
    # HMAC key for capability tokens. Required whenever service endpoints are
    # enabled; fail closed rather than minting process-local or derived keys.
    service_token_secret: str | None = None
    # Periodically refresh liveness for the full lifetime of an active HTTP
    # stream or WebSocket, not merely when it is opened.
    service_traffic_refreshes_heartbeat: bool = True
    service_active_heartbeat_interval_seconds: int = 60
    # Wildcard origin template for untrusted sandbox traffic, e.g.
    # "https://{subdomain}.sandboxes.example.net". Required when the feature is
    # enabled. Each capability gets a different hostname, which is the browser
    # security boundary.
    service_public_base_url: str | None = None
    # Development-only escape hatch. Production service origins must use TLS.
    service_allow_insecure_http: bool = False
    # Proxy connection pool and timeouts.
    service_proxy_pool_size: int = 200
    service_proxy_pool_limit_per_host: int = 20
    service_proxy_connect_timeout: int = 5
    service_proxy_handshake_timeout_seconds: int = 15
    service_proxy_timeout_seconds: int = 300
    service_proxy_max_request_bytes: int = 16 * 1024 * 1024
    service_proxy_max_message_bytes: int = 16 * 1024 * 1024

    # Sandbox pod probes for the in-pod agent (/health on agent_port).
    # startupProbe covers the agent boot window AFTER the container starts;
    # while it runs, readiness/liveness are suppressed so an agent that is
    # still importing/binding is not flapped as not-ready.
    agent_startup_period_seconds: int = 2  # how often to poll /health during boot
    agent_startup_timeout_seconds: int = 3  # per-probe HTTP timeout during boot
    agent_startup_failure_threshold: int = 30  # 30 * 2s = 60s agent-boot budget
    # readinessProbe runs after startup succeeds. Relaxed timeouts so a busy
    # agent (CPU pressure on a packed node) is not flapped to not-ready on a
    # single slow reply.
    agent_readiness_period_seconds: int = 5  # was 2
    agent_readiness_timeout_seconds: int = 5  # was 2
    agent_readiness_failure_threshold: int = 3

    # Live readiness gating for exec dispatch (Plan A): consult PodWatcher's
    # live in-memory cache before dispatching, instead of trusting the latched
    # sandbox.ready flag (which only ever flips True, never back to False).
    # This reflects the agent's CURRENT reachability and costs zero K8s calls.
    agent_ready_wait_seconds: int = 15  # max wait for agent to become ready before exec
    agent_ready_cache_miss_k8s_fallback: bool = (
        True  # on cache miss, do one K8s status check
    )

    # Exec connection-failure retry budget.  Wide enough to ride out a cold
    # image pull (median ~19s, max ~55s) + agent startup before giving up.
    # Only connection failures are retried; command timeouts are NOT.
    exec_connect_retry_window: int = 45  # total seconds to keep retrying agent connect
    exec_connect_backoff_cap: float = 8.0  # max single backoff between attempts

    # TTL settings
    sandbox_ttl_hours: int = 2
    orphan_job_ttl_hours: int = 1
    cleanup_interval_seconds: int = 300  # 5 minutes
    pending_timeout_buffer_seconds: int = (
        600  # Extra buffer beyond creation_timeout for pending sandboxes
    )

    # Heartbeat settings
    heartbeat_timeout_seconds: int = 180  # 3 minutes without heartbeat = dead
    heartbeat_cleanup_enabled: bool = True  # Enable heartbeat-based cleanup

    # Redis settings (for multi-replica state sharing)
    redis_url: str = "redis://redis-service.orchestrator.svc.cluster.local:6379/0"
    redis_password: str | None = None
    redis_require_auth: bool = True
    use_redis: bool = True  # Set to False to use in-memory store (single replica only)

    # Logging
    log_level: str = "INFO"
    log_format: str = "json"

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"

    def get_api_keys_set(self) -> set:
        """Get the set of valid API keys.

        Supports keys separated by commas, spaces, or newlines.
        """
        if not self.api_keys:
            return set()
        # Split by comma, space, or newline to handle all common formats
        import re

        return set(
            key.strip() for key in re.split(r"[,\s]+", self.api_keys) if key.strip()
        )


settings = Settings()
