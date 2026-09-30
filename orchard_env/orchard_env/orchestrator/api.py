"""FastAPI application and routes."""

import asyncio
import hmac
import logging
import time
from contextlib import asynccontextmanager
from urllib.parse import unquote_plus, urljoin, urlsplit

import aiohttp
from fastapi import (
    Depends,
    FastAPI,
    HTTPException,
    Request,
    Security,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.security import APIKeyHeader
from pydantic import BaseModel, Field

from orchard_env.orchestrator.agent_client import AgentClient
from orchard_env.orchestrator.exec_manager import ExecManager
from orchard_env.orchestrator.job_store import JobStore
from orchard_env.orchestrator.k8s_client import K8sClient
from orchard_env.orchestrator.pod_watcher import PodWatcher
from orchard_env.orchestrator.redis_job_store import RedisJobStore
from orchard_env.orchestrator.sandbox_manager import SandboxManager
from orchard_env.orchestrator.service_proxy import (
    ServicePortError,
    ServiceProxyClient,
    ServiceRequestTooLargeError,
    build_service_url,
    filter_request_headers,
    filter_response_headers,
    filter_websocket_headers,
    validate_port,
)
from orchard_env.orchestrator.service_tokens import (
    ServiceTokenError,
    mint_token,
    verify_token,
)
from orchard_env.orchestrator.settings import settings
from orchard_env.orchestrator.utils import (
    generate_request_id,
    generate_sandbox_id,
    request_id_var,
    setup_logging,
)

logger = logging.getLogger(__name__)

# API Key security scheme
api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)


async def verify_api_key(api_key: str = Security(api_key_header)) -> str:
    """Verify the API key from request header."""
    if not settings.require_api_key:
        return "no-auth"

    valid_keys = settings.get_api_keys_set()
    if not valid_keys:
        logger.warning("No API keys configured but auth is required")
        raise HTTPException(
            status_code=500, detail="Server misconfiguration: no API keys configured"
        )

    if not api_key:
        raise HTTPException(
            status_code=401, detail="Missing API key. Please provide X-API-Key header."
        )

    if api_key not in valid_keys:
        raise HTTPException(status_code=403, detail="Invalid API key")

    return api_key


# Global managers
k8s_client: K8sClient
sandbox_manager: SandboxManager
job_store: JobStore
exec_manager: ExecManager
pod_watcher: PodWatcher
agent_client: AgentClient
service_proxy_client: ServiceProxyClient
cleanup_task: asyncio.Task | None = None


async def cleanup_loop():
    """Background task for cleaning up expired resources."""
    while True:
        try:
            await asyncio.sleep(settings.cleanup_interval_seconds)

            # Clean up expired sandboxes
            await sandbox_manager.cleanup_expired_sandboxes()

            # Clean up old jobs
            await job_store.cleanup_old_jobs(settings.orphan_job_ttl_hours)

            # Reconcile sandboxes with Kubernetes
            await sandbox_manager.reconcile_sandboxes()

        except Exception as e:
            logger.error(f"Error in cleanup loop: {e}", exc_info=True)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan manager."""
    global k8s_client, sandbox_manager, job_store, exec_manager, pod_watcher, agent_client, service_proxy_client, cleanup_task

    # Setup logging
    setup_logging()
    logger.info("Starting Sandbox Orchestrator")

    # Increase default thread pool size for concurrent K8s operations.
    # Each exec runs stream+read in a thread; we need extra headroom
    # beyond max_concurrent_execs for non-exec operations (create/delete pods,
    # check pod status, etc.) that also use asyncio.to_thread.
    import concurrent.futures

    loop = asyncio.get_running_loop()
    thread_pool_size = settings.max_concurrent_execs + 50  # extra headroom
    loop.set_default_executor(
        concurrent.futures.ThreadPoolExecutor(max_workers=thread_pool_size)
    )

    # Initialize managers
    k8s_client = K8sClient()
    sandbox_manager = SandboxManager(k8s_client)

    # Ensure the shared sandbox namespace exists
    await sandbox_manager.ensure_sandbox_namespace()

    # Use Redis-based job store for multi-replica support
    if settings.use_redis:
        logger.info("Using Redis-based job store for multi-replica support")
        job_store = RedisJobStore()
        await job_store.connect()
    else:
        logger.info("Using in-memory job store (single replica mode)")
        job_store = JobStore()

    exec_manager = ExecManager(k8s_client, sandbox_manager, job_store)

    # Agent client for direct pod communication (exec / file ops)
    agent_client = AgentClient()

    # Proxy client for user services running inside sandboxes. Constructed
    # unconditionally (it is inert until a request arrives) so the routes do
    # not have to guard against a missing client.
    service_proxy_client = ServiceProxyClient()
    if settings.enable_service_endpoints:
        logger.info("Sandbox service endpoints are enabled")

    # Start pod watcher (Watch/Informer pattern)
    pod_watcher = PodWatcher()
    await pod_watcher.start()
    sandbox_manager.set_pod_watcher(pod_watcher)
    logger.info("Started PodWatcher (Watch/Informer)")

    # Start cleanup task
    cleanup_task = asyncio.create_task(cleanup_loop())
    logger.info("Started cleanup background task")

    yield

    # Cleanup
    if agent_client:
        await agent_client.close()

    if service_proxy_client:
        await service_proxy_client.close()

    if pod_watcher:
        await pod_watcher.stop()

    if cleanup_task:
        cleanup_task.cancel()
        try:
            await cleanup_task
        except asyncio.CancelledError:
            pass

    logger.info("Shutting down Sandbox Orchestrator")


def _configured_service_base_url() -> str:
    """Return the validated wildcard origin template for service traffic."""
    value = (settings.service_public_base_url or "").rstrip("/")
    if not value:
        raise HTTPException(
            status_code=503,
            detail="SERVICE_PUBLIC_BASE_URL is required for service endpoints",
        )

    if value.count("{subdomain}") != 1:
        raise HTTPException(
            status_code=503,
            detail=("SERVICE_PUBLIC_BASE_URL must contain one {subdomain} placeholder"),
        )

    parsed = urlsplit(value.replace("{subdomain}", "probe"))
    if (
        not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
    ):
        raise HTTPException(
            status_code=503,
            detail="SERVICE_PUBLIC_BASE_URL must be an origin without path or credentials",
        )
    if parsed.scheme != "https" and not (
        settings.service_allow_insecure_http and parsed.scheme == "http"
    ):
        raise HTTPException(
            status_code=503,
            detail=(
                "SERVICE_PUBLIC_BASE_URL must use https "
                "(or explicitly enable SERVICE_ALLOW_INSECURE_HTTP for development)"
            ),
        )
    return value


def _authority(value: str, scheme: str) -> tuple[str, int]:
    """Normalise a Host header or URL authority for comparison."""
    parsed = urlsplit(value if "://" in value else f"//{value}", scheme=scheme)
    host = (parsed.hostname or "").lower()
    port = parsed.port or (443 if scheme == "https" else 80)
    return host, port


class ServiceOriginIsolationMiddleware:
    """Keep hostile service content off the management API origin.

    The configured service origin serves only ``/s/...`` routes. Conversely,
    capability routes are refused on every other origin. This prevents sandbox
    HTML, cookies, or service workers from sharing an origin with management
    endpoints.
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if settings.enable_service_endpoints is not True or scope["type"] not in {
            "http",
            "websocket",
        }:
            await self.app(scope, receive, send)
            return

        try:
            service_url = _configured_service_base_url()
        except HTTPException:
            service_url = ""

        path = scope.get("path", "")
        is_service_path = path.startswith("/s/")
        if not service_url and not is_service_path:
            # Keep management endpoints reachable so they can return a clear
            # configuration error instead of making the whole API disappear.
            await self.app(scope, receive, send)
            return
        headers = {
            key.decode("latin-1").lower(): value.decode("latin-1")
            for key, value in scope.get("headers", [])
        }
        request_host = _authority(
            headers.get("host", ""),
            (
                urlsplit(service_url.replace("{subdomain}", "probe")).scheme
                if service_url
                else "https"
            ),
        )
        allowed = False
        if service_url and is_service_path:
            parts = path.split("/", 3)
            token = parts[2] if len(parts) > 2 else ""
            expected_url = urlsplit(build_service_url(service_url, token))
            allowed = request_host == _authority(
                expected_url.netloc, expected_url.scheme
            )
        elif service_url:
            probe = urlsplit(service_url.replace("{subdomain}", "probe"))
            suffix = (probe.hostname or "")[len("probe") :]
            is_service_hostname = bool(suffix) and request_host[0].endswith(suffix)
            allowed = not is_service_hostname
        if allowed:
            await self.app(scope, receive, send)
            return

        if scope["type"] == "websocket":
            await send({"type": "websocket.close", "code": 4404})
            return

        response = JSONResponse({"detail": "Not found"}, status_code=404)
        await response(scope, receive, send)


app = FastAPI(
    title="Sandbox Orchestrator",
    description="Azure AKS-based sandbox orchestration service",
    version="0.1.0",
    lifespan=lifespan,
)
app.add_middleware(ServiceOriginIsolationMiddleware)


@app.middleware("http")
async def add_request_id(request: Request, call_next):
    """Add request ID to all requests."""
    request_id = generate_request_id()
    request_id_var.set(request_id)

    response = await call_next(request)
    response.headers["X-Request-ID"] = request_id
    return response


@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    """Global exception handler."""
    logger.error(f"Unhandled exception: {exc}", exc_info=True)
    return JSONResponse(
        status_code=500,
        content={
            "error": "Internal server error",
            "detail": str(exc),
            "request_id": request_id_var.get(""),
        },
    )


# API Models


class CreateSandboxRequest(BaseModel):
    """Request to create a sandbox."""

    image: str = Field(..., description="Container image to use")
    block_network: bool = Field(
        default=True, description="Whether to block network egress"
    )
    sandbox_id: str | None = Field(
        default=None, description="Optional custom sandbox ID"
    )
    cpu: str | None = Field(
        default=None,
        description="CPU request/limit (e.g., '4', '2000m'). Defaults to 4 cores.",
    )
    memory: str | None = Field(
        default=None,
        description="Memory request/limit (e.g., '16Gi', '8Gi'). Defaults to 16Gi.",
    )
    timeout: int | None = Field(
        default=None,
        description="Timeout in seconds for sandbox to become ready. Defaults to 3600.",
    )


class CreateSandboxResponse(BaseModel):
    """Response for sandbox creation."""

    sandbox_id: str
    namespace: str
    image: str
    block_network: bool
    cpu: str
    memory: str
    timeout: int
    status: str = "pending"  # pending, ready, failed


class ExecRequest(BaseModel):
    """Request to execute a command."""

    command: str | list[str] = Field(..., description="Command to execute")
    timeout_seconds: int | None = Field(
        default=None, description="Execution timeout in seconds"
    )
    cwd: str | None = Field(default=None, description="Working directory")
    env: dict[str, str] | None = Field(
        default=None, description="Environment variables"
    )
    login_shell: bool = Field(
        default=False,
        description="Use login shell (bash -lc) instead of regular shell (bash -c)",
    )
    wait: bool = Field(
        default=False,
        description="If true, block until job completes and return full result inline",
    )


class ExecResponse(BaseModel):
    """Response for command execution submission."""

    job_id: str
    status: str = "queued"


class JobResponse(BaseModel):
    """Response for job status query."""

    job_id: str
    sandbox_id: str
    command: str
    status: str
    stdout: str = ""
    stderr: str = ""
    exit_code: int | None = None
    created_at: float
    started_at: float | None = None
    completed_at: float | None = None
    error: str | None = None


class ApplyPatchRequest(BaseModel):
    """Request to apply a git patch."""

    patch: str = Field(..., description="Unified diff patch content")
    timeout_seconds: int = Field(default=30, description="Timeout in seconds")


class ApplyPatchResponse(BaseModel):
    """Response for patch application."""

    success: bool
    stdout: str = ""
    stderr: str = ""
    exit_code: int | None = None
    error: str | None = None


# API Routes


@app.get("/")
async def root():
    """Root endpoint."""
    return {"service": settings.service_name, "version": "0.1.0", "status": "running"}


@app.get("/health")
async def health():
    """Health check endpoint."""
    return {"status": "healthy"}


@app.get("/resources")
async def get_resources(api_key: str = Depends(verify_api_key)):
    """Return cluster resource summary.

    Lists all requested CPU/memory resources and available resources in
    the Kubernetes cluster.  Useful for capacity planning and monitoring.
    """
    try:
        data = await k8s_client.get_cluster_resources()
        return {"status": "ok", **data}
    except Exception as e:
        logger.error(f"Error fetching cluster resources: {e}", exc_info=True)
        raise HTTPException(
            status_code=500,
            detail=f"Failed to fetch cluster resources: {e}",
        )


@app.post("/sandboxes", response_model=CreateSandboxResponse)
async def create_sandbox(
    request: CreateSandboxRequest, api_key: str = Depends(verify_api_key)
):
    """Create a new sandbox.

    Returns immediately after creating the pod. Client should poll
    GET /sandboxes/{sandbox_id} to check when it's ready.
    This allows proper cancellation - if client cancels, they can
    send DELETE to cleanup the pending sandbox.
    """
    sandbox_id = request.sandbox_id or generate_sandbox_id()
    timeout = request.timeout or 3600

    try:
        logger.info(f"Creating sandbox {sandbox_id} with image {request.image}")

        sandbox = await sandbox_manager.create_sandbox(
            sandbox_id=sandbox_id,
            image=request.image,
            block_network=request.block_network,
            cpu=request.cpu,
            memory=request.memory,
            wait_ready=False,  # Don't wait for pod to be ready
            timeout=timeout,
        )

        return CreateSandboxResponse(
            sandbox_id=sandbox.sandbox_id,
            namespace=sandbox.namespace,
            image=sandbox.image,
            block_network=sandbox.block_network,
            cpu=sandbox.cpu,
            memory=sandbox.memory,
            timeout=timeout,
            status="pending" if not sandbox.ready else "ready",
        )

    except asyncio.CancelledError:
        # Client disconnected - try cleanup
        logger.warning(f"Client disconnected during sandbox creation {sandbox_id}")
        try:
            await sandbox_manager.delete_sandbox(sandbox_id)
        except Exception:
            pass
        raise

    except Exception as e:
        logger.error(f"Error creating sandbox: {e}", exc_info=True)
        # Return 503 for transient K8s API errors so clients can retry
        err_str = str(e)
        if any(
            keyword in err_str
            for keyword in [
                "Connection timed out",
                "Max retries exceeded",
                "timed out",
                "Too Many Requests",
                "429",
                "503",
                "502",
                "RemoteDisconnected",
                "Connection aborted",
                "Connection refused",
                "Connection reset",
            ]
        ):
            raise HTTPException(
                status_code=503,
                detail=f"Service temporarily overloaded, please retry: {e}",
                headers={"Retry-After": "5"},
            )
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/sandboxes/{sandbox_id}/exec")
async def exec_command(
    sandbox_id: str, request: ExecRequest, api_key: str = Depends(verify_api_key)
):
    """Execute a command in a sandbox.

    If wait=True, blocks until the job completes and returns full result.
    If wait=False (default), returns immediately with job_id for polling.
    """
    try:
        job_id = await exec_manager.submit_exec(
            sandbox_id=sandbox_id,
            command=request.command,
            timeout_seconds=request.timeout_seconds,
            cwd=request.cwd,
            env=request.env,
            login_shell=request.login_shell,
        )

        if not request.wait:
            return ExecResponse(job_id=job_id)

        # Server-side wait: block until job completes
        exec_timeout = request.timeout_seconds or settings.default_timeout_seconds
        job = await job_store.wait_for_completion(job_id, timeout=exec_timeout + 30)

        if not job:
            return ExecResponse(job_id=job_id)

        return JobResponse(
            job_id=job.job_id,
            sandbox_id=job.sandbox_id,
            command=job.command,
            status=job.status.value,
            stdout=job.stdout,
            stderr=job.stderr,
            exit_code=job.exit_code,
            created_at=job.created_at,
            started_at=job.started_at,
            completed_at=job.completed_at,
            error=job.error,
        )

    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        logger.error(f"Error submitting exec: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/jobs/{job_id}", response_model=JobResponse)
async def get_job(job_id: str, api_key: str = Depends(verify_api_key)):
    """Get job status and results."""
    job = await job_store.get_job(job_id)

    if not job:
        raise HTTPException(status_code=404, detail=f"Job {job_id} not found")

    return JobResponse(
        job_id=job.job_id,
        sandbox_id=job.sandbox_id,
        command=job.command,
        status=job.status.value,
        stdout=job.stdout,
        stderr=job.stderr,
        exit_code=job.exit_code,
        created_at=job.created_at,
        started_at=job.started_at,
        completed_at=job.completed_at,
        error=job.error,
    )


@app.get("/jobs/{job_id}/wait")
async def wait_job(
    job_id: str, timeout: int = 300, api_key: str = Depends(verify_api_key)
):
    """Wait for a job to complete (server-side wait).

    Blocks until the job finishes or timeout. Eliminates client-side polling.
    The exec task runs on this same replica, so the local asyncio.Event
    fires instantly when the job completes — zero polling overhead.
    """
    job = await job_store.wait_for_completion(job_id, timeout=timeout)

    if not job:
        raise HTTPException(status_code=404, detail=f"Job {job_id} not found")

    return JobResponse(
        job_id=job.job_id,
        sandbox_id=job.sandbox_id,
        command=job.command,
        status=job.status.value,
        stdout=job.stdout,
        stderr=job.stderr,
        exit_code=job.exit_code,
        created_at=job.created_at,
        started_at=job.started_at,
        completed_at=job.completed_at,
        error=job.error,
    )


@app.post("/sandboxes/{sandbox_id}/apply_patch", response_model=ApplyPatchResponse)
async def apply_patch(
    sandbox_id: str, request: ApplyPatchRequest, api_key: str = Depends(verify_api_key)
):
    """Apply a git patch in the sandbox."""
    try:
        result = await exec_manager.apply_patch(
            sandbox_id=sandbox_id,
            patch=request.patch,
            timeout_seconds=request.timeout_seconds,
        )

        return ApplyPatchResponse(**result)

    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        logger.error(f"Error applying patch: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/sandboxes/{sandbox_id}/heartbeat")
async def sandbox_heartbeat(sandbox_id: str, api_key: str = Depends(verify_api_key)):
    """Send heartbeat for a sandbox to keep it alive.

    Clients should send heartbeats periodically (e.g., every 60s).
    Sandboxes that don't receive heartbeats within the configured timeout
    will be cleaned up automatically.
    """
    success = await sandbox_manager.heartbeat(sandbox_id)
    if not success:
        raise HTTPException(status_code=404, detail=f"Sandbox {sandbox_id} not found")
    return {"status": "ok", "sandbox_id": sandbox_id}


@app.delete("/sandboxes/{sandbox_id}")
async def delete_sandbox(sandbox_id: str, api_key: str = Depends(verify_api_key)):
    """Delete a sandbox."""
    try:
        await sandbox_manager.delete_sandbox(sandbox_id)
        return {"status": "deleted", "sandbox_id": sandbox_id}

    except Exception as e:
        logger.error(f"Error deleting sandbox: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/sandboxes/{sandbox_id}")
async def get_sandbox(sandbox_id: str, api_key: str = Depends(verify_api_key)):
    """Get sandbox information.

    Uses PodWatcher cache for instant status checks (zero K8s API calls).
    Falls back to direct K8s API call if not in cache.
    """
    sandbox = await sandbox_manager.get_sandbox(sandbox_id)
    if not sandbox:
        raise HTTPException(status_code=404, detail=f"Sandbox {sandbox_id} not found")

    # Try cached status from PodWatcher first (zero API calls)
    pod_status = pod_watcher.get_pod_status(sandbox_id)

    if pod_status is None:
        # Not in watch cache yet — fall back to direct check
        sandbox = await sandbox_manager.check_sandbox_ready(sandbox_id)
        if not sandbox:
            raise HTTPException(
                status_code=404, detail=f"Sandbox {sandbox_id} not found"
            )
        pod_status = getattr(sandbox, "_pod_status", None) or {}
    else:
        # Update sandbox ready state from cache if needed
        if pod_status["ready"] and not sandbox.ready:
            sandbox.ready = True
            await sandbox_manager._update_sandbox(sandbox_id, {"ready": True})

    status = pod_status.get("status", "ready" if sandbox.ready else "pending")

    return {
        "sandbox_id": sandbox.sandbox_id,
        "namespace": sandbox.namespace,
        "image": sandbox.image,
        "pod_name": sandbox.pod_name,
        "block_network": sandbox.block_network,
        "cpu": sandbox.cpu,
        "memory": sandbox.memory,
        "created_at": sandbox.created_at,
        "ready": sandbox.ready or pod_status.get("ready", False),
        "status": status,
        "status_message": pod_status.get("message", ""),
    }


@app.get("/sandboxes/{sandbox_id}/wait")
async def wait_sandbox_ready(
    sandbox_id: str, timeout: int = 3600, api_key: str = Depends(verify_api_key)
):
    """Wait for a sandbox to become ready (server-side wait).

    Instead of the client polling GET /sandboxes/{id} every 2 seconds,
    this endpoint blocks until the pod is ready, failed, or timeout.
    Uses PodWatcher events — zero polling, instant notification.

    Args:
        timeout: Maximum seconds to wait (default: 3600)

    Returns:
        Sandbox info with ready=True, or error status.
    """
    sandbox = await sandbox_manager.get_sandbox(sandbox_id)
    if not sandbox:
        raise HTTPException(status_code=404, detail=f"Sandbox {sandbox_id} not found")

    try:
        pod_status = await pod_watcher.wait_for_ready(sandbox_id, timeout=timeout)
    except TimeoutError as e:
        raise HTTPException(status_code=408, detail=str(e))

    if pod_status["ready"] and not sandbox.ready:
        sandbox.ready = True
        await sandbox_manager._update_sandbox(sandbox_id, {"ready": True})

    status = pod_status.get("status", "ready" if pod_status["ready"] else "pending")

    return {
        "sandbox_id": sandbox.sandbox_id,
        "namespace": sandbox.namespace,
        "image": sandbox.image,
        "pod_name": sandbox.pod_name,
        "block_network": sandbox.block_network,
        "cpu": sandbox.cpu,
        "memory": sandbox.memory,
        "created_at": sandbox.created_at,
        "ready": pod_status["ready"],
        "status": status,
        "status_message": pod_status.get("message", ""),
    }


# File Operations Models


class UploadFileRequest(BaseModel):
    """Request to upload a file."""

    path: str = Field(..., description="Destination path in the sandbox")
    content: str = Field(..., description="Base64 encoded file content")


class UploadFileResponse(BaseModel):
    """Response for file upload."""

    success: bool
    path: str
    size: int


class DownloadFileResponse(BaseModel):
    """Response for file download."""

    path: str
    content: str  # Base64 encoded
    size: int


class FileInfo(BaseModel):
    """A single entry returned by the file-listing endpoint."""

    name: str
    type: str  # "file" or "directory"
    size: int
    modified: str | None = None


class ListFilesResponse(BaseModel):
    """Response for listing files."""

    path: str
    files: list[FileInfo]


# File Operations Routes


@app.post("/sandboxes/{sandbox_id}/files", response_model=UploadFileResponse)
async def upload_file(
    sandbox_id: str, request: UploadFileRequest, api_key: str = Depends(verify_api_key)
):
    """Upload a file to the sandbox.

    The content should be base64 encoded.
    """
    import base64

    sandbox = await sandbox_manager.get_sandbox(sandbox_id)
    if not sandbox:
        raise HTTPException(status_code=404, detail=f"Sandbox {sandbox_id} not found")

    if not sandbox.ready:
        raise HTTPException(
            status_code=400, detail=f"Sandbox {sandbox_id} is not ready"
        )

    try:
        # Decode base64 content
        file_content = base64.b64decode(request.content)

        # Get pod IP and upload via agent
        pod_ip = await sandbox_manager.get_pod_ip(sandbox_id)
        if not pod_ip:
            raise HTTPException(
                status_code=503, detail=f"No pod IP available for sandbox {sandbox_id}"
            )

        await agent_client.upload_file(
            pod_ip=pod_ip,
            path=request.path,
            content_b64=request.content,
        )

        return UploadFileResponse(
            success=True,
            path=request.path,
            size=len(file_content),
        )

    except Exception as e:
        logger.error(f"Error uploading file: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/sandboxes/{sandbox_id}/files")
async def download_file(
    sandbox_id: str, path: str, api_key: str = Depends(verify_api_key)
):
    """Download a file from the sandbox.

    Returns base64 encoded content.
    """

    sandbox = await sandbox_manager.get_sandbox(sandbox_id)
    if not sandbox:
        raise HTTPException(status_code=404, detail=f"Sandbox {sandbox_id} not found")

    if not sandbox.ready:
        raise HTTPException(
            status_code=400, detail=f"Sandbox {sandbox_id} is not ready"
        )

    try:
        # Get pod IP and download via agent
        pod_ip = await sandbox_manager.get_pod_ip(sandbox_id)
        if not pod_ip:
            raise HTTPException(
                status_code=503, detail=f"No pod IP available for sandbox {sandbox_id}"
            )

        result = await agent_client.download_file(pod_ip=pod_ip, path=path)

        return DownloadFileResponse(
            path=path,
            content=result["content"],
            size=result.get("size", 0),
        )

    except FileNotFoundError:
        raise HTTPException(status_code=404, detail=f"File not found: {path}")
    except Exception as e:
        logger.error(f"Error downloading file: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/sandboxes/{sandbox_id}/files/list", response_model=ListFilesResponse)
async def list_files(
    sandbox_id: str, path: str = "/workspace", api_key: str = Depends(verify_api_key)
):
    """List files in a directory in the sandbox."""
    sandbox = await sandbox_manager.get_sandbox(sandbox_id)
    if not sandbox:
        raise HTTPException(status_code=404, detail=f"Sandbox {sandbox_id} not found")

    if not sandbox.ready:
        raise HTTPException(
            status_code=400, detail=f"Sandbox {sandbox_id} is not ready"
        )

    try:
        # Get pod IP and list via agent
        pod_ip = await sandbox_manager.get_pod_ip(sandbox_id)
        if not pod_ip:
            raise HTTPException(
                status_code=503, detail=f"No pod IP available for sandbox {sandbox_id}"
            )

        files = await agent_client.list_files(pod_ip=pod_ip, path=path)

        return ListFilesResponse(
            path=path,
            files=files,
        )

    except FileNotFoundError:
        raise HTTPException(status_code=404, detail=f"Path not found: {path}")
    except Exception as e:
        logger.error(f"Error listing files: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


# ---------------------------------------------------------------------------
# Streaming PTY exec (WebSocket proxy to agent)
# ---------------------------------------------------------------------------
#
# Client connects to:
#   WS /sandboxes/{sandbox_id}/exec/pty?api_key=<key>
# We forward verbatim (text frames in both directions) to the agent's
# WS /exec/pty endpoint on the pod IP.  No frame parsing — the wire
# protocol is the agent's contract; the orchestrator is a dumb pipe.
#
# Auth: WebSocket can't use APIKeyHeader; we accept either the
# ``api_key`` query parameter or the ``Sec-WebSocket-Protocol`` header.


def _verify_ws_api_key(ws: WebSocket) -> bool:
    """Return True if the WS connection is authorized."""
    if not settings.require_api_key:
        return True
    valid_keys = settings.get_api_keys_set()
    if not valid_keys:
        return False
    key = ws.query_params.get("api_key")
    if not key:
        # Also accept Sec-WebSocket-Protocol: api_key.<key> for clients
        # that prefer not to put secrets in URLs.
        proto = ws.headers.get("sec-websocket-protocol", "")
        for p in [x.strip() for x in proto.split(",")]:
            if p.startswith("api_key."):
                key = p[len("api_key.") :]
                break
    return bool(key) and key in valid_keys


@app.websocket("/sandboxes/{sandbox_id}/exec/pty")
async def exec_pty_ws(ws: WebSocket, sandbox_id: str) -> None:
    """Proxy a PTY exec WebSocket to the sandbox agent.

    Forwards text frames in both directions.  Closes both sides on
    either disconnect.
    """
    # Auth
    if not _verify_ws_api_key(ws):
        await ws.close(code=4401)  # custom: unauthorized
        return

    # Resolve sandbox + pod IP BEFORE accepting, so we can reject cleanly.
    sandbox = await sandbox_manager.get_sandbox(sandbox_id)
    if not sandbox:
        await ws.close(code=4404)
        return
    if not sandbox.ready:
        await ws.close(code=4409)  # conflict: not ready
        return
    pod_ip = await sandbox_manager.get_pod_ip(sandbox_id)
    if not pod_ip:
        await ws.close(code=4503)
        return

    await ws.accept()

    try:
        agent_ws = await agent_client.open_pty_ws(pod_ip)
    except Exception as e:
        logger.error(f"Failed to open agent WS for {sandbox_id}: {e}")
        try:
            await ws.send_text(f'{{"type":"error","message":"agent unreachable: {e}"}}')
        except Exception:
            pass
        await ws.close(code=4503)
        return

    async def _c2a() -> None:
        """Forward frames client → agent."""
        try:
            while True:
                msg = await ws.receive_text()
                await agent_ws.send_str(msg)
        except WebSocketDisconnect:
            pass
        except Exception as e:
            logger.warning(f"c2a forward error: {e}")
        finally:
            await agent_ws.close()

    async def _a2c() -> None:
        """Forward frames agent → client."""
        try:
            async for msg in agent_ws:
                if msg.type == aiohttp.WSMsgType.TEXT:
                    await ws.send_text(msg.data)
                elif msg.type == aiohttp.WSMsgType.BINARY:
                    await ws.send_bytes(msg.data)
                elif msg.type in (
                    aiohttp.WSMsgType.CLOSE,
                    aiohttp.WSMsgType.CLOSED,
                    aiohttp.WSMsgType.ERROR,
                ):
                    break
        except Exception as e:
            logger.warning(f"a2c forward error: {e}")
        finally:
            try:
                await ws.close()
            except Exception:
                pass

    c2a = asyncio.create_task(_c2a())
    a2c = asyncio.create_task(_a2c())
    done, pending = await asyncio.wait({c2a, a2c}, return_when=asyncio.FIRST_COMPLETED)
    for t in pending:
        t.cancel()
    try:
        await agent_ws.close()
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Sandbox service endpoints (HTTP + WebSocket proxy to a user service)
# ---------------------------------------------------------------------------
#
# Exec and file I/O cover agents that *run commands*. A growing class of
# workloads instead speaks a protocol to a long-running server inside the
# sandbox: an OpenEnv environment server, an MCP server, a dev server, an
# evaluation endpoint. Those need a reachable URL, not a shell.
#
# An operator opens a port explicitly:
#
#     POST /sandboxes/{id}/services      {"port": 8000}
#       -> {"url": "https://host/s/<token>", "expires_at": ...}
#
# and traffic then flows through that URL:
#
#     ANY /s/{token}/{path}      HTTP, including streaming responses
#     WS  /s/{token}/{path}      WebSocket, frames forwarded verbatim
#
# The token rides in the path so a client which appends its own suffix — an
# OpenEnv EnvClient appending ``/ws`` — produces a working URL. It is a bearer
# credential and may appear in ingress request-target logs; operators must
# redact ``/s/*`` and use short TTLs.
#
# Kubernetes note: the pod spec is unchanged. ``containerPort`` is
# informational, so any process listening on 0.0.0.0 inside the pod is already
# reachable at the pod IP. The gap this closes is reachability from *outside*
# the cluster, not inside it.


class CreateServiceRequest(BaseModel):
    """Request to expose a service port running inside a sandbox."""

    port: int = Field(..., description="Port the service listens on inside the sandbox")
    ttl_seconds: int | None = Field(
        default=None,
        gt=0,
        description="Lifetime of the returned URL. Defaults to SERVICE_TOKEN_TTL_SECONDS.",
    )
    wait_ready: bool = Field(
        default=False,
        description=(
            "Block until the service answers health_path. Orchard's own "
            "readiness only covers the in-pod agent, so a sandbox can be ready "
            "while the service inside it is still starting."
        ),
    )
    health_path: str = Field(
        default="/health", description="Path polled when wait_ready is true"
    )
    ready_timeout: int = Field(
        default=60,
        gt=0,
        description="Seconds to wait for the service when wait_ready is true",
    )


class ServiceResponse(BaseModel):
    """An exposed service endpoint."""

    sandbox_id: str
    port: int
    url: str
    expires_at: float


class ServiceListResponse(BaseModel):
    """Ports currently exposed for a sandbox."""

    sandbox_id: str
    ports: list[int]


def _require_service_endpoints_enabled() -> None:
    if settings.enable_service_endpoints is not True:
        raise HTTPException(
            status_code=404,
            detail=(
                "Sandbox service endpoints are disabled. Set "
                "ENABLE_SERVICE_ENDPOINTS=true on the orchestrator to enable them."
            ),
        )
    _configured_service_base_url()
    if not settings.service_token_secret:
        raise HTTPException(
            status_code=503,
            detail="SERVICE_TOKEN_SECRET is required for service endpoints",
        )


@app.post("/sandboxes/{sandbox_id}/services", response_model=ServiceResponse)
async def create_service(
    sandbox_id: str,
    body: CreateServiceRequest,
    api_key: str = Depends(verify_api_key),
):
    """Expose a port inside a sandbox and return a self-authenticating URL."""
    _require_service_endpoints_enabled()

    try:
        port = validate_port(body.port)
    except ServicePortError as e:
        raise HTTPException(status_code=400, detail=str(e))

    sandbox = await sandbox_manager.get_sandbox(sandbox_id)
    if not sandbox:
        raise HTTPException(status_code=404, detail=f"Sandbox {sandbox_id} not found")

    # Probe before changing exposure state. A failed readiness check must not
    # reactivate a previously revoked URL or leave a half-created endpoint.
    if body.wait_ready:
        pod_ip = await sandbox_manager.get_current_pod_ip(sandbox_id)
        if not pod_ip:
            raise HTTPException(
                status_code=503, detail=f"Pod IP not available for sandbox {sandbox_id}"
            )
        deadline = asyncio.get_running_loop().time() + body.ready_timeout
        while True:
            if await service_proxy_client.probe(pod_ip, port, body.health_path):
                break
            if asyncio.get_running_loop().time() >= deadline:
                raise HTTPException(
                    status_code=408,
                    detail=(
                        f"Service on port {port} did not answer {body.health_path} "
                        f"within {body.ready_timeout}s"
                    ),
                )
            await asyncio.sleep(1)

    try:
        exposure = await sandbox_manager.expose_service(sandbox_id, port)
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))
    if exposure is None:
        raise HTTPException(status_code=404, detail=f"Sandbox {sandbox_id} not found")
    generation, _created = exposure

    token, expires_at = mint_token(sandbox_id, port, generation, body.ttl_seconds)
    # The token is a bearer credential: return it, never log it.
    logger.info(f"Issued service endpoint for sandbox {sandbox_id} port {port}")
    return ServiceResponse(
        sandbox_id=sandbox_id,
        port=port,
        url=build_service_url(_configured_service_base_url(), token),
        expires_at=expires_at,
    )


@app.get("/sandboxes/{sandbox_id}/services", response_model=ServiceListResponse)
async def list_services(sandbox_id: str, api_key: str = Depends(verify_api_key)):
    """List the ports a sandbox currently exposes."""
    _require_service_endpoints_enabled()

    ports = await sandbox_manager.list_services(sandbox_id)
    if ports is None:
        raise HTTPException(status_code=404, detail=f"Sandbox {sandbox_id} not found")
    return ServiceListResponse(sandbox_id=sandbox_id, ports=ports)


@app.delete("/sandboxes/{sandbox_id}/services/{port}")
async def delete_service(
    sandbox_id: str, port: int, api_key: str = Depends(verify_api_key)
):
    """Revoke a previously exposed port.

    Takes effect immediately, including for tokens that have not yet expired.
    """
    _require_service_endpoints_enabled()

    revoked = await sandbox_manager.revoke_service(sandbox_id, port)
    if not revoked:
        raise HTTPException(
            status_code=404,
            detail=f"Port {port} is not exposed for sandbox {sandbox_id}",
        )
    return {"status": "revoked", "sandbox_id": sandbox_id, "port": port}


async def _resolve_service_target(token: str) -> tuple[str, int, str, str, int]:
    """Validate a capability token and resolve it to a live pod.

    Returns ``(sandbox_id, port, pod_ip)``.

    Every hop is re-checked on every request: the signature, the expiry, the
    sandbox's continued existence, and the port's continued presence in the
    allowlist. That last check is what makes revocation immediate.
    """
    try:
        sandbox_id, port, generation, expires_at = verify_token(token)
    except ServiceTokenError as e:
        # Deliberately terse: do not tell a prober which part failed.
        raise HTTPException(status_code=403, detail=str(e))

    sandbox = await sandbox_manager.get_sandbox(sandbox_id)
    if not sandbox:
        raise HTTPException(status_code=404, detail="Sandbox not found")

    active_generation = await sandbox_manager.get_service_generation(sandbox_id, port)
    if not active_generation or not hmac.compare_digest(active_generation, generation):
        raise HTTPException(status_code=403, detail="Port is not exposed")

    # Service capabilities cross a trust boundary; use an authoritative K8s
    # read rather than a cache that may outlive a reused custom sandbox ID.
    pod_ip = await sandbox_manager.get_current_pod_ip(sandbox_id)
    if not pod_ip:
        raise HTTPException(status_code=503, detail="Pod IP not available")

    if settings.service_traffic_refreshes_heartbeat:
        # An actively used service should not be reaped by heartbeat cleanup
        # just because the client drives it over the proxy instead of calling
        # POST /heartbeat itself.
        await sandbox_manager.heartbeat(sandbox_id)

    return sandbox_id, port, pod_ip, generation, expires_at


def _raw_service_path(scope: dict, token: str) -> str:
    """Return the encoded suffix after ``/s/{token}`` from the ASGI scope."""
    raw_path = scope.get("raw_path") or scope.get("path", "").encode("ascii")
    prefix = f"/s/{token}".encode("ascii")
    if raw_path == prefix:
        return "/"
    if not raw_path.startswith(prefix + b"/"):
        raise HTTPException(status_code=400, detail="Malformed service path")
    return raw_path[len(prefix) :].decode("ascii")


def _raw_query_string(scope: dict) -> str:
    return scope.get("query_string", b"").decode("ascii")


def _safe_service_query_string(scope: dict) -> str:
    """Drop a management ``api_key`` while preserving every other raw field."""
    raw = _raw_query_string(scope)
    if not raw:
        return ""
    management_keys = settings.get_api_keys_set()
    kept: list[str] = []
    for field in raw.split("&"):
        key, separator, value = field.partition("=")
        if (
            unquote_plus(key).lower() == "api_key"
            and separator
            and unquote_plus(value) in management_keys
        ):
            continue
        kept.append(field)
    return "&".join(kept)


def _safe_service_protocols(ws: WebSocket) -> tuple[str, ...]:
    """Remove PTY management-auth subprotocols before forwarding."""
    management_keys = settings.get_api_keys_set()
    protocols: list[str] = []
    for protocol in ws.headers.get("sec-websocket-protocol", "").split(","):
        protocol = protocol.strip()
        if not protocol:
            continue
        if (
            protocol.startswith("api_key.")
            and protocol[len("api_key.") :] in management_keys
        ):
            continue
        protocols.append(protocol)
    return tuple(protocols)


def _rewrite_service_location(
    location: str,
    token: str,
    raw_path: str,
    pod_ip: str,
    port: int,
) -> str | None:
    """Map a same-service redirect back beneath the capability URL.

    Redirects to any other destination are refused so a client cannot carry
    ambient credentials from the service endpoint to an attacker-controlled
    origin.
    """
    upstream_current = f"http://{pod_ip}:{port}{raw_path}"
    resolved = urlsplit(urljoin(upstream_current, location))
    resolved_port = resolved.port or (443 if resolved.scheme == "https" else 80)
    if (
        resolved.scheme != "http"
        or resolved.hostname != pod_ip
        or resolved_port != port
    ):
        return None

    service_base = build_service_url(_configured_service_base_url(), token)
    rewritten = f"{service_base}{resolved.path or '/'}"
    if resolved.query:
        rewritten += f"?{resolved.query}"
    if resolved.fragment:
        rewritten += f"#{resolved.fragment}"
    return rewritten


def _service_response_headers(
    raw_headers,
    token: str,
    raw_path: str,
    pod_ip: str,
    port: int,
) -> list[tuple[str, str]]:
    headers = filter_response_headers(raw_headers)
    result: list[tuple[str, str]] = []
    for key, value in headers:
        if key.lower() != "location":
            result.append((key, value))
            continue
        rewritten = _rewrite_service_location(value, token, raw_path, pod_ip, port)
        if rewritten is None:
            raise HTTPException(
                status_code=502, detail="External service redirect blocked"
            )
        result.append((key, rewritten))
    return result


def _request_has_body(request: Request) -> bool:
    length = request.headers.get("content-length")
    if length:
        try:
            parsed_length = int(length)
            if parsed_length < 0:
                raise ValueError
            if parsed_length > settings.service_proxy_max_request_bytes:
                raise HTTPException(status_code=413, detail="Request body too large")
            return parsed_length > 0
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid Content-Length")
    return "transfer-encoding" in request.headers


async def _bounded_request_body(request: Request):
    total = 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > settings.service_proxy_max_request_bytes:
            raise ServiceRequestTooLargeError
        yield chunk


async def _maintain_service_session(
    sandbox_id: str,
    port: int,
    generation: str,
    expires_at: int,
    close_session=None,
) -> None:
    """Refresh liveness and terminate when the capability stops authorizing."""
    heartbeat_interval = max(
        0.01, float(settings.service_active_heartbeat_interval_seconds)
    )
    authorization_interval = min(heartbeat_interval, 5.0)
    next_heartbeat = 0.0
    try:
        while True:
            if not await _service_session_authorized(
                sandbox_id, port, generation, expires_at
            ):
                if close_session:
                    await close_session()
                return
            if settings.service_traffic_refreshes_heartbeat:
                now = asyncio.get_running_loop().time()
                if now >= next_heartbeat:
                    if not await sandbox_manager.heartbeat(sandbox_id):
                        if close_session:
                            await close_session()
                        return
                    next_heartbeat = now + heartbeat_interval
            await asyncio.sleep(authorization_interval)
    except asyncio.CancelledError:
        return
    except Exception as e:
        # Authorization state is fail-closed. A transient Redis failure should
        # terminate the capability session, not leave it running indefinitely.
        logger.warning(f"Service authorization watchdog failed: {e}")
        if close_session:
            await close_session()


async def _service_session_authorized(
    sandbox_id: str, port: int, generation: str, expires_at: int
) -> bool:
    if time.time() > expires_at:
        return False
    active_generation = await sandbox_manager.get_service_generation(sandbox_id, port)
    return bool(active_generation) and hmac.compare_digest(
        active_generation, generation
    )


def _start_service_session_watchdog(
    sandbox_id: str,
    port: int,
    generation: str,
    expires_at: int,
    close_session=None,
) -> asyncio.Task:
    return asyncio.create_task(
        _maintain_service_session(
            sandbox_id,
            port,
            generation,
            expires_at,
            close_session=close_session,
        )
    )


async def _stop_task(task: asyncio.Task | None) -> None:
    if not task:
        return
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


@app.api_route(
    "/s/{token}",
    methods=["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"],
)
async def proxy_service_http_root(token: str, request: Request):
    """Proxy the exact service base URL without a slash redirect."""
    return await proxy_service_http(token, "", request)


@app.api_route(
    "/s/{token}/{path:path}",
    methods=["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"],
)
async def proxy_service_http(token: str, path: str, request: Request):
    """Proxy an HTTP request to a service inside a sandbox.

    The URL is the credential, so no ``X-API-Key`` header is required — which
    is the whole point: clients that cannot set headers can still connect.
    """
    _require_service_endpoints_enabled()
    sandbox_id, port, pod_ip, generation, expires_at = await _resolve_service_target(
        token
    )

    body = _bounded_request_body(request) if _request_has_body(request) else None
    raw_path = _raw_service_path(request.scope, token)
    upstream = None
    authorization_lost = asyncio.Event()
    request_task = asyncio.create_task(
        service_proxy_client.request(
            method=request.method,
            pod_ip=pod_ip,
            port=port,
            raw_path=raw_path,
            raw_query_string=_safe_service_query_string(request.scope),
            headers=filter_request_headers(request.headers),
            body=body,
        )
    )

    async def _close_revoked_request() -> None:
        authorization_lost.set()
        if upstream is not None:
            upstream.close()
        elif not request_task.done():
            request_task.cancel()

    watchdog = _start_service_session_watchdog(
        sandbox_id,
        port,
        generation,
        expires_at,
        close_session=_close_revoked_request,
    )
    try:
        upstream = await request_task
        if not await _service_session_authorized(
            sandbox_id, port, generation, expires_at
        ):
            upstream.close()
            await _stop_task(watchdog)
            raise HTTPException(status_code=403, detail="Service capability expired")
    except asyncio.CancelledError:
        await _stop_task(watchdog)
        if authorization_lost.is_set():
            raise HTTPException(status_code=403, detail="Service capability expired")
        raise
    except ServiceRequestTooLargeError:
        await _stop_task(watchdog)
        raise HTTPException(status_code=413, detail="Request body too large")
    except TimeoutError:
        await _stop_task(watchdog)
        raise HTTPException(status_code=504, detail="Service request timed out")
    except aiohttp.ClientError as e:
        await _stop_task(watchdog)
        if isinstance(e.__cause__, ServiceRequestTooLargeError):
            raise HTTPException(status_code=413, detail="Request body too large")
        # The exception text carries the pod IP, which the caller has no
        # business seeing: keep it in the log, not the response.
        logger.warning(f"Service proxy upstream error on port {port}: {e}")
        raise HTTPException(status_code=502, detail="Service unreachable")
    except Exception:
        await _stop_task(watchdog)
        raise

    try:
        response_headers = _service_response_headers(
            upstream.raw_headers, token, raw_path, pod_ip, port
        )
    except HTTPException:
        upstream.release()
        await _stop_task(watchdog)
        raise

    async def _stream():
        try:
            async for chunk in upstream.content.iter_any():
                yield chunk
        finally:
            upstream.release()
            await _stop_task(watchdog)

    response = StreamingResponse(
        _stream(),
        status_code=upstream.status,
    )
    response.raw_headers = [
        (key.encode("latin-1"), value.encode("latin-1"))
        for key, value in response_headers
    ]
    return response


@app.websocket("/s/{token}/{path:path}")
async def proxy_service_ws(ws: WebSocket, token: str, path: str) -> None:
    """Proxy a WebSocket to a service inside a sandbox.

    Frames are forwarded verbatim in both directions and the upstream close
    code is propagated, so the orchestrator stays a dumb pipe — the same
    contract the PTY proxy already follows.
    """
    if settings.enable_service_endpoints is not True:
        await ws.close(code=4404)
        return

    try:
        sandbox_id, port, pod_ip, generation, expires_at = (
            await _resolve_service_target(token)
        )
    except HTTPException as e:
        # Resolve before accepting so a rejection is a clean handshake failure
        # rather than an accepted socket that immediately dies.
        await ws.close(code=4403 if e.status_code == 403 else 4404)
        return

    offered_protocols = _safe_service_protocols(ws)
    upstream = None
    accepted = False
    authorization_lost = asyncio.Event()
    open_task = asyncio.create_task(
        service_proxy_client.open_websocket(
            pod_ip=pod_ip,
            port=port,
            raw_path=_raw_service_path(ws.scope, token),
            raw_query_string=_safe_service_query_string(ws.scope),
            headers=filter_websocket_headers(ws.headers),
            protocols=offered_protocols,
            origin=ws.headers.get("origin"),
        )
    )

    async def _close_revoked_socket() -> None:
        authorization_lost.set()
        if accepted:
            await ws.close(code=4403)
        if upstream is not None:
            await upstream.close()
        elif not open_task.done():
            open_task.cancel()

    watchdog = _start_service_session_watchdog(
        sandbox_id,
        port,
        generation,
        expires_at,
        close_session=_close_revoked_socket,
    )
    try:
        upstream = await open_task
    except asyncio.CancelledError:
        await _stop_task(watchdog)
        if authorization_lost.is_set():
            await ws.close(code=4403)
            return
        raise
    except Exception as e:
        await _stop_task(watchdog)
        logger.warning(f"Failed to open upstream WS on port {port}: {e}")
        await ws.close(code=4503)
        return

    # The handshake may have taken long enough for the capability to expire or
    # be revoked. Revalidate before accepting the downstream socket.
    try:
        await _resolve_service_target(token)
    except HTTPException:
        await upstream.close()
        await _stop_task(watchdog)
        await ws.close(code=4403)
        return

    await ws.accept(subprotocol=upstream.protocol)
    accepted = True

    async def _client_to_service() -> None:
        try:
            while True:
                message = await ws.receive()
                if message["type"] == "websocket.disconnect":
                    break
                if message.get("text") is not None:
                    await upstream.send_str(message["text"])
                elif message.get("bytes") is not None:
                    await upstream.send_bytes(message["bytes"])
        except WebSocketDisconnect:
            pass
        except Exception as e:
            logger.warning(f"service ws client->service error: {e}")
        finally:
            await upstream.close()

    async def _service_to_client() -> None:
        try:
            async for message in upstream:
                if message.type == aiohttp.WSMsgType.TEXT:
                    await ws.send_text(message.data)
                elif message.type == aiohttp.WSMsgType.BINARY:
                    await ws.send_bytes(message.data)
                elif message.type in (
                    aiohttp.WSMsgType.CLOSE,
                    aiohttp.WSMsgType.CLOSED,
                    aiohttp.WSMsgType.ERROR,
                ):
                    break
        except Exception as e:
            logger.warning(f"service ws service->client error: {e}")
        finally:
            try:
                # Propagate the upstream close code so the client can tell a
                # normal shutdown from a protocol error.
                close_code = upstream.close_code or 1000
                await ws.close(code=close_code)
            except Exception:
                pass

    try:
        to_service = asyncio.create_task(_client_to_service())
        to_client = asyncio.create_task(_service_to_client())
        _done, pending = await asyncio.wait(
            {to_service, to_client}, return_when=asyncio.FIRST_COMPLETED
        )
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
    finally:
        try:
            await upstream.close()
        except Exception:
            pass
        await _stop_task(watchdog)


@app.websocket("/s/{token}")
async def proxy_service_ws_root(ws: WebSocket, token: str) -> None:
    """Proxy the exact WebSocket service base URL."""
    await proxy_service_ws(ws, token, "")
