"""Python client for Sandbox Orchestrator.

This module provides both sync (SandboxClient) and async (AsyncSandboxClient) classes for
creating and managing sandboxes. Commands are executed via an in-pod agent (FastAPI server
injected automatically via init container), bypassing the K8s API server for low-latency,
high-concurrency exec. Works with ANY container image.

Example usage (sync):
    from orchard_env.client.sandbox_client import SandboxClient

    with SandboxClient("http://localhost:8000") as client:
        with client.create_sandbox("ubuntu:22.04") as sandbox:
            result = sandbox.exec("echo 'hello'")
            print(result)

Example usage (async):
    from orchard_env.client.sandbox_client import AsyncSandboxClient

    async with AsyncSandboxClient("http://localhost:8000") as client:
        async with await client.create_sandbox("ubuntu:22.04") as sandbox:
            result = await sandbox.exec("echo 'hello'")
            print(result)
"""

import asyncio
import atexit
import os
import random
import signal
import threading
import time
import uuid
from http.client import RemoteDisconnected
from typing import TYPE_CHECKING, Union
from urllib.parse import urlsplit

import aiohttp
import requests
from requests.exceptions import ChunkedEncodingError, ConnectionError, Timeout

if TYPE_CHECKING:
    from orchard_env.client.process import AsyncContainerProcess, ContainerProcess


def _origin(url: str) -> tuple[str, str, int]:
    parsed = urlsplit(url)
    return (
        parsed.scheme.lower(),
        (parsed.hostname or "").lower(),
        parsed.port or (443 if parsed.scheme == "https" else 80),
    )


class _ScopedApiKeySession(requests.Session):
    """Attach the management key only to the configured orchestrator origin."""

    def __init__(self, base_url: str, api_key: str | None):
        super().__init__()
        self._management_origin = _origin(base_url)
        self._api_key = api_key

    def prepare_request(self, request):
        prepared = super().prepare_request(request)
        if self._api_key and _origin(prepared.url) == self._management_origin:
            prepared.headers["X-API-Key"] = self._api_key
        else:
            prepared.headers.pop("X-API-Key", None)
        return prepared

    def rebuild_auth(self, prepared_request, response):
        super().rebuild_auth(prepared_request, response)
        if self._api_key and _origin(prepared_request.url) == self._management_origin:
            prepared_request.headers["X-API-Key"] = self._api_key
        else:
            prepared_request.headers.pop("X-API-Key", None)


# Global registry for tracking sandboxes to cleanup on exit
# Store (base_url, sandbox_id, api_key) tuples instead of client references
_pending_cleanup: set[tuple] = set()
_cleanup_registered = False


def _register_cleanup():
    """Register cleanup handlers for program exit."""
    global _cleanup_registered
    if _cleanup_registered:
        return
    _cleanup_registered = True

    def cleanup_handler(*args):
        """Cleanup all pending sandboxes on exit."""
        if not _pending_cleanup:
            return

        print(f"[Cleanup] Deleting {len(_pending_cleanup)} sandbox(es) on exit...")
        for item in list(_pending_cleanup):
            try:
                base_url, sandbox_id = item[0], item[1]
                api_key = item[2] if len(item) > 2 else None
                headers = {"X-API-Key": api_key} if api_key else {}
                requests.delete(
                    f"{base_url}/sandboxes/{sandbox_id}", headers=headers, timeout=5
                )
                _pending_cleanup.discard(item)
            except Exception:
                pass  # Best effort cleanup

    # Register atexit handler
    atexit.register(cleanup_handler)

    # Register signal handlers for graceful shutdown
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            original_handler = signal.getsignal(sig)

            def signal_handler(signum, frame, orig=original_handler):
                cleanup_handler()
                if callable(orig) and orig not in (signal.SIG_IGN, signal.SIG_DFL):
                    orig(signum, frame)
                elif orig == signal.SIG_DFL:
                    signal.signal(signum, signal.SIG_DFL)
                    signal.raise_signal(signum)

            signal.signal(sig, signal_handler)
        except (OSError, ValueError):
            pass  # Can't set signal handler in some contexts


class JobResult:
    """Result of a job execution."""

    def __init__(self, data: dict):
        """Initialize from API response."""
        self.job_id: str = data["job_id"]
        self.sandbox_id: str = data["sandbox_id"]
        self.command: str = data["command"]
        self.status: str = data["status"]
        self.stdout: str = data.get("stdout", "")
        self.stderr: str = data.get("stderr", "")
        self.exit_code: int | None = data.get("exit_code")
        self.error: str | None = data.get("error")
        self.created_at: float = data["created_at"]
        self.started_at: float | None = data.get("started_at")
        self.completed_at: float | None = data.get("completed_at")

    @property
    def succeeded(self) -> bool:
        """Check if job succeeded."""
        return self.status == "succeeded"

    @property
    def failed(self) -> bool:
        """Check if job failed."""
        return self.status == "failed"

    @property
    def is_complete(self) -> bool:
        """Check if job is complete."""
        return self.status in ["succeeded", "failed"]

    def __repr__(self) -> str:
        return f"JobResult(status={self.status}, exit_code={self.exit_code})"


class ServiceEndpoint:
    """A URL for a service running inside a sandbox.

    Returned by :meth:`SandboxInstance.expose_service`. The URL carries its own
    credential, so it is a bearer token: anyone holding it can reach that port
    on that sandbox until ``expires_at``, or until the port is revoked.
    """

    def __init__(self, data: dict):
        self.sandbox_id: str = data.get("sandbox_id", "")
        self.port: int = data.get("port", 0)
        self.url: str = data.get("url", "")
        self.expires_at: float = data.get("expires_at", 0.0)

    @property
    def ws_url(self) -> str:
        """The same endpoint as a ``ws://``/``wss://`` URL."""
        if self.url.startswith("https://"):
            return "wss://" + self.url[len("https://") :]
        if self.url.startswith("http://"):
            return "ws://" + self.url[len("http://") :]
        return self.url

    def __repr__(self) -> str:
        # The URL is a credential, so it is deliberately not in the repr.
        return f"ServiceEndpoint(sandbox_id={self.sandbox_id}, port={self.port})"


class SandboxInstance:
    """A sandbox instance that supports command execution."""

    def __init__(self, client: "SandboxClient", sandbox_id: str, data: dict):
        """Initialize sandbox instance."""
        self._client = client
        self.sandbox_id = sandbox_id
        self.namespace = data.get("namespace")
        self.image = data.get("image")
        self.block_network = data.get("block_network")
        self._deleted = False
        self._heartbeat_thread: threading.Thread | None = None
        self._heartbeat_stop = threading.Event()

    def start_heartbeat(self, interval: int = 60) -> None:
        """Start sending periodic heartbeats to keep the sandbox alive.

        Args:
            interval: Seconds between heartbeats (default: 60)
        """
        if self._heartbeat_thread and self._heartbeat_thread.is_alive():
            return  # Already running

        self._heartbeat_stop.clear()

        def _heartbeat_loop():
            while not self._heartbeat_stop.wait(interval):
                if self._deleted:
                    break
                try:
                    self._client._request(
                        "POST", f"/sandboxes/{self.sandbox_id}/heartbeat"
                    )
                except Exception:
                    pass  # Best effort

        self._heartbeat_thread = threading.Thread(
            target=_heartbeat_loop, daemon=True, name=f"heartbeat-{self.sandbox_id[:8]}"
        )
        self._heartbeat_thread.start()

    def stop_heartbeat(self) -> None:
        """Stop the heartbeat background thread."""
        self._heartbeat_stop.set()
        if self._heartbeat_thread:
            self._heartbeat_thread.join(timeout=5)
            self._heartbeat_thread = None

    def heartbeat(self) -> None:
        """Send a single heartbeat now.

        :meth:`start_heartbeat` owns a background thread, which is the right
        shape for a long-lived session. Callers driving their own loop — an RL
        rollout that already ticks per step, or an adapter implementing another
        framework's heartbeat hook — want one call instead.
        """
        self._client._request("POST", f"/sandboxes/{self.sandbox_id}/heartbeat")

    def expose_service(
        self,
        port: int,
        ttl_seconds: int | None = None,
        wait_ready: bool = False,
        health_path: str = "/health",
        ready_timeout: int = 60,
    ) -> "ServiceEndpoint":
        """Expose a service running inside the sandbox and return its URL.

        The sandbox agent handles commands and files; this is for a *server*
        running in the sandbox that a client needs to speak a protocol to — an
        OpenEnv environment server, an MCP server, a dev server.

        The returned URL authenticates itself, so it works with clients that
        cannot attach an ``X-API-Key`` header (a raw WebSocket client, a
        browser). Treat it as a bearer credential: scope it with ``ttl_seconds``
        and keep it out of logs.

        Args:
            port: Port the service listens on inside the sandbox.
            ttl_seconds: Lifetime of the URL. Server default if omitted.
            wait_ready: Block until the service answers ``health_path``. The
                sandbox being ready only means the *agent* is up, so a server
                you just launched may still be starting.
            health_path: Path polled when ``wait_ready`` is true.
            ready_timeout: Seconds to wait when ``wait_ready`` is true.

        Returns:
            ServiceEndpoint: Carries ``url``, ``port``, and ``expires_at``.

        Example::

            sandbox.exec("nohup python -m http.server 8000 &")
            endpoint = sandbox.expose_service(8000, wait_ready=True, health_path="/")
            requests.get(endpoint.url)
        """
        payload = {
            "port": port,
            "wait_ready": wait_ready,
            "health_path": health_path,
            "ready_timeout": ready_timeout,
        }
        if ttl_seconds is not None:
            payload["ttl_seconds"] = ttl_seconds

        data = self._client._request(
            "POST", f"/sandboxes/{self.sandbox_id}/services", json=payload
        )
        return ServiceEndpoint(data)

    def list_services(self) -> list[int]:
        """Return the ports this sandbox currently exposes."""
        data = self._client._request("GET", f"/sandboxes/{self.sandbox_id}/services")
        return data.get("ports", [])

    def revoke_service(self, port: int) -> None:
        """Stop exposing *port*.

        Effective immediately, including for URLs that have not yet expired.
        """
        self._client._request("DELETE", f"/sandboxes/{self.sandbox_id}/services/{port}")

    def exec(
        self,
        command: str | list[str],
        timeout: int | None = None,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        wait: bool = True,
        poll_interval: float = 0.1,
        login_shell: bool = False,
        pty: bool = False,
        rows: int = 24,
        cols: int = 80,
    ) -> Union[JobResult, "ContainerProcess"]:
        """
        Execute a command in the sandbox.

        Args:
            command: Command to execute (string or list)
            timeout: Timeout in seconds
            cwd: Working directory
            env: Environment variables
            wait: If True, wait for job to complete
            poll_interval: Polling interval when waiting
            login_shell: If True, use login shell (bash -lc) instead of regular shell (bash -c)
            pty: If True, open a streaming PTY session and return a
                ``ContainerProcess`` handle (Modal-style).  ``wait`` /
                ``timeout`` / ``poll_interval`` are ignored — control the
                lifecycle through the handle.
            rows, cols: initial PTY window size (only when ``pty=True``).

        Returns:
            JobResult with execution results, or ContainerProcess when ``pty=True``.
        """
        if self._deleted:
            raise RuntimeError("Sandbox has been deleted")

        if pty:
            # Local import to avoid loading websocket-client unless used.
            from orchard_env.client.process import (
                ContainerProcess,
                _build_init_frame,
                _ws_url,
            )

            cmd_list = (
                command if isinstance(command, list) else ["bash", "-c", str(command)]
            )
            init = _build_init_frame(
                cmd_list,
                env=env,
                workdir=cwd,
                rows=rows,
                cols=cols,
                login_shell=login_shell,
            )
            url = _ws_url(self._client.base_url, self.sandbox_id, self._client.api_key)
            return ContainerProcess(url, init)

        exec_timeout = timeout or 300

        # Try server-side wait first (zero polling)
        try:
            response = self._client._request(
                "POST",
                f"/sandboxes/{self.sandbox_id}/exec",
                json={
                    "command": command,
                    "timeout_seconds": timeout,
                    "cwd": cwd,
                    "env": env,
                    "login_shell": login_shell,
                    "wait": True,
                },
                timeout=exec_timeout + 60,
            )

            # Only return if the job truly completed (succeeded or failed).
            # A "running" status means the server-side wait timed out and
            # the job is still executing — we must NOT treat it as complete.
            if response.get("status") in ("succeeded", "failed"):
                return JobResult(response)

            job_id = response["job_id"]
        except Exception:
            # Fall back to non-wait + polling
            response = self._client._request(
                "POST",
                f"/sandboxes/{self.sandbox_id}/exec",
                json={
                    "command": command,
                    "timeout_seconds": timeout,
                    "cwd": cwd,
                    "env": env,
                    "login_shell": login_shell,
                },
            )
            job_id = response["job_id"]

        if not wait:
            return JobResult(
                {
                    "job_id": job_id,
                    "sandbox_id": self.sandbox_id,
                    "command": str(command),
                    "status": "queued",
                    "created_at": time.time(),
                }
            )

        # Fallback: client-side polling
        deadline = time.monotonic() + exec_timeout + 60
        while True:
            result = self.get_job(job_id)

            if result.is_complete:
                return result

            if time.monotonic() >= deadline:
                raise TimeoutError(
                    f"Job {job_id} did not complete within {exec_timeout}s"
                )

            time.sleep(poll_interval)

    def apply_patch(self, patch: str, timeout: int = 30) -> dict:
        """
        Apply a git patch in the sandbox.

        Args:
            patch: Unified diff patch content
            timeout: Timeout in seconds

        Returns:
            Dictionary with success status and output
        """
        if self._deleted:
            raise RuntimeError("Sandbox has been deleted")

        response = self._client._request(
            "POST",
            f"/sandboxes/{self.sandbox_id}/apply_patch",
            json={
                "patch": patch,
                "timeout_seconds": timeout,
            },
        )

        return response

    def get_job(self, job_id: str) -> JobResult:
        """
        Get job status and results.

        Args:
            job_id: Job ID

        Returns:
            JobResult with execution results
        """
        response = self._client._request("GET", f"/jobs/{job_id}")
        return JobResult(response)

    def delete(self) -> None:
        """Delete the sandbox."""
        if self._deleted:
            return

        # Stop heartbeat
        self.stop_heartbeat()

        # Remove from tracking
        self._client._created_sandboxes.discard(self.sandbox_id)
        _pending_cleanup.discard(
            (self._client.base_url, self.sandbox_id, self._client.api_key)
        )

        self._client._request("DELETE", f"/sandboxes/{self.sandbox_id}")
        self._deleted = True

    def upload_file(self, local_path: str, remote_path: str) -> dict:
        """
        Upload a file to the sandbox.

        Args:
            local_path: Local file path to upload
            remote_path: Destination path in the sandbox

        Returns:
            Dictionary with success status and size
        """
        import base64

        if self._deleted:
            raise RuntimeError("Sandbox has been deleted")

        with open(local_path, "rb") as f:
            content = base64.b64encode(f.read()).decode("utf-8")

        return self._client._request(
            "POST",
            f"/sandboxes/{self.sandbox_id}/files",
            json={
                "path": remote_path,
                "content": content,
            },
        )

    def upload_content(self, content: bytes, remote_path: str) -> dict:
        """
        Upload content directly to the sandbox.

        Args:
            content: File content as bytes
            remote_path: Destination path in the sandbox

        Returns:
            Dictionary with success status and size
        """
        import base64

        if self._deleted:
            raise RuntimeError("Sandbox has been deleted")

        return self._client._request(
            "POST",
            f"/sandboxes/{self.sandbox_id}/files",
            json={
                "path": remote_path,
                "content": base64.b64encode(content).decode("utf-8"),
            },
        )

    def download_file(self, remote_path: str, local_path: str) -> int:
        """
        Download a file from the sandbox to local.

        Args:
            remote_path: Source path in the sandbox
            local_path: Local destination path

        Returns:
            Size of downloaded file in bytes
        """
        import base64

        if self._deleted:
            raise RuntimeError("Sandbox has been deleted")

        response = self._client._request(
            "GET", f"/sandboxes/{self.sandbox_id}/files", params={"path": remote_path}
        )

        content = base64.b64decode(response["content"])
        with open(local_path, "wb") as f:
            f.write(content)

        return len(content)

    def download_content(self, remote_path: str) -> bytes:
        """
        Download file content from the sandbox.

        Args:
            remote_path: Source path in the sandbox

        Returns:
            File content as bytes
        """
        import base64

        if self._deleted:
            raise RuntimeError("Sandbox has been deleted")

        response = self._client._request(
            "GET", f"/sandboxes/{self.sandbox_id}/files", params={"path": remote_path}
        )

        return base64.b64decode(response["content"])

    def list_files(self, remote_path: str = "/") -> list[dict]:
        """
        List files in a directory in the sandbox.

        Args:
            remote_path: Directory path in the sandbox (default: "/")

        Returns:
            List of file info dicts with name, type, size, modified
        """
        if self._deleted:
            raise RuntimeError("Sandbox has been deleted")

        response = self._client._request(
            "GET",
            f"/sandboxes/{self.sandbox_id}/files/list",
            params={"path": remote_path},
        )

        return response["files"]

    def check_env(self, verbose: bool = True) -> dict:
        """Check sandbox environment: bashrc loading, conda activation, PATH, python.

        Runs two probes (with and without login shell) to reliably detect whether
        bashrc was sourced, conda is activated, and which python/python3 are available.

        Detection: compares HISTCONTROL between login and bare shells. Standard
        bashrc sets HISTCONTROL=ignoredups:ignorespace, so if it differs the
        bashrc was sourced.

        Args:
            verbose: If True, print a summary to stdout (default: True)

        Returns:
            Dict with keys: bashrc_loaded, conda_available, conda_env,
            conda_prefix, path, bare_path, python, python3, python_version
        """
        probe_cmd = (
            'echo "__HAS_BASHRC=$( [ -f ~/.bashrc ] && echo yes || echo no )"; '
            'echo "__HISTCONTROL=${HISTCONTROL:-}"; '
            'echo "__HAS_CONDA=$(command -v conda >/dev/null 2>&1 && echo yes || echo no)"; '
            'echo "__CONDA_DEFAULT_ENV=${CONDA_DEFAULT_ENV:-}"; '
            'echo "__CONDA_PREFIX=${CONDA_PREFIX:-}"; '
            'echo "__CONDA_EXE=$(echo ${CONDA_EXE:-none})"; '
            'echo "__PATH=$PATH"; '
            'echo "__PYTHON=$(command -v python 2>/dev/null || echo none)"; '
            'echo "__PYTHON3=$(command -v python3 2>/dev/null || echo none)"; '
            'echo "__PYTHON_VERSION=$(python --version 2>/dev/null || echo none)"; '
            'echo "__BASHRC_CONTENT=$(cat ~/.bashrc 2>/dev/null || echo none)"'
        )

        def _parse_probe(stdout: str) -> dict:
            info = {}
            for line in (stdout or "").splitlines():
                if line.startswith("__"):
                    key, _, val = line.partition("=")
                    info[key.lstrip("_")] = val
            return info

        # Probe 1: bare shell (no bashrc) to get baseline
        bare_result = self.exec(
            'echo "__BARE_PATH=$PATH"; echo "__BARE_HISTCONTROL=${HISTCONTROL:-}"',
            timeout=10,
            login_shell=False,
        )
        bare_info = _parse_probe(bare_result.stdout)
        bare_path = bare_info.get("BARE_PATH", "")
        bare_histctl = bare_info.get("BARE_HISTCONTROL", "")

        # Probe 2: login shell to get full environment
        login_result = self.exec(probe_cmd, timeout=15, login_shell=True)
        info = _parse_probe(login_result.stdout)

        login_path = info.get("PATH", "")
        login_histctl = info.get("HISTCONTROL", "")
        has_bashrc = info.get("HAS_BASHRC", "no") == "yes"
        # bashrc is "loaded" if: file exists AND environment changed
        # (HISTCONTROL is set by virtually every default bashrc)
        bashrc_loaded = has_bashrc and (
            login_histctl != bare_histctl or login_path != bare_path
        )

        conda_exe = info.get("CONDA_EXE", "none")
        conda_available = "yes" in info.get("HAS_CONDA", "no")
        # conda function may exist but the binary may be broken
        # (e.g. conda Python package uninstalled). Check if CONDA_EXE works.
        conda_broken = False
        if conda_available and conda_exe != "none":
            test = self.exec(
                f"{conda_exe} --version 2>&1 || true",
                timeout=5,
                login_shell=False,
            )
            if "ModuleNotFoundError" in (test.stdout + test.stderr):
                conda_broken = True

        env_info = {
            "bashrc_loaded": bashrc_loaded,
            "has_bashrc": has_bashrc,
            "conda_available": conda_available and not conda_broken,
            "conda_broken": conda_broken,
            "conda_exe": conda_exe,
            "conda_env": info.get("CONDA_DEFAULT_ENV", "") or None,
            "conda_prefix": info.get("CONDA_PREFIX", "") or None,
            "path": login_path,
            "bare_path": bare_path,
            "python": info.get("PYTHON", "none"),
            "python3": info.get("PYTHON3", "none"),
            "python_version": info.get("PYTHON_VERSION", "none"),
            "bashrc_content": info.get("BASHRC_CONTENT", "none"),
        }

        if verbose:
            sid = self.sandbox_id[:12]
            print(f"[{sid}] === Environment Check ===")
            print(f"  ~/.bashrc exists: {'yes' if has_bashrc else 'no'}")
            print(f"  bashrc loaded   : {'yes' if bashrc_loaded else 'NO'}")
            if conda_broken:
                print(f"  conda available : BROKEN (module missing in {conda_exe})")
            else:
                print(f"  conda available : {'yes' if conda_available else 'no'}")
            if conda_available and not conda_broken:
                print(f"  conda env       : {env_info['conda_env'] or '(none)'}")
                print(f"  conda prefix    : {env_info['conda_prefix'] or '(none)'}")
            print(f"  conda exe       : {conda_exe}")
            print(f"  python          : {env_info['python']}")
            print(f"  python3         : {env_info['python3']}")
            print(f"  python version  : {env_info['python_version']}")
            print(
                f"  bashrc content  : {env_info['bashrc_content'][:120]}{'...' if len(env_info['bashrc_content']) > 120 else ''}"
            )
            print(
                f"  PATH (login)    : {login_path[:120]}{'...' if len(login_path) > 120 else ''}"
            )
            if login_path != bare_path:
                print(
                    f"  PATH (bare)     : {bare_path[:120]}{'...' if len(bare_path) > 120 else ''}"
                )

        return env_info

    def __enter__(self):
        """Context manager entry."""
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        """Context manager exit - cleanup sandbox."""
        self.delete()

    def __repr__(self) -> str:
        status = "deleted" if self._deleted else "active"
        return f"SandboxInstance(id={self.sandbox_id}, status={status})"


class SandboxClient:
    """Sandbox client for creating and managing sandbox instances.

    Automatically tracks all created sandboxes and cleans them up on program exit.
    """

    def __init__(
        self,
        base_url: str | None = None,
        timeout: int = 1200,
        auto_cleanup: bool = True,
        api_key: str | None = None,
        prefix: str | None = None,
    ):
        """
        Initialize sandbox client.

        Args:
            base_url: Base URL of the orchestrator service. If not provided, falls back to
                      SANDBOX_BASE_URL environment variable, then defaults to http://localhost:8000.
            timeout: Default request timeout in seconds (default: 1200s = 20 minutes)
            auto_cleanup: If True, automatically cleanup sandboxes on program exit (default: True)
            api_key: API key for authentication. If not provided, falls back to
                     SANDBOX_API_KEY environment variable.
            prefix: Optional prefix for sandbox IDs. If not provided, falls back to
                    SANDBOX_PREFIX environment variable. When set, all sandbox IDs created
                    by this client will be prefixed with this value (e.g., "myapp-abc123").
        """
        # Use provided base_url or fall back to environment variable
        self.base_url = (
            base_url or os.environ.get("SANDBOX_BASE_URL") or "http://localhost:8000"
        ).rstrip("/")
        self.timeout = timeout
        # Use provided api_key or fall back to environment variable
        self.api_key = api_key or os.environ.get("SANDBOX_API_KEY")
        self.session = _ScopedApiKeySession(self.base_url, self.api_key)
        # Use provided prefix or fall back to environment variable
        self.prefix = prefix or os.environ.get("SANDBOX_PREFIX")
        self.max_retries = 3
        self.retry_delay = 1  # Initial delay in seconds
        self._created_sandboxes: set[str] = set()  # Track created sandbox IDs
        self._auto_cleanup = auto_cleanup

        if auto_cleanup:
            _register_cleanup()

    def _request(self, method: str, path: str, **kwargs) -> dict:
        """Make HTTP request with automatic retry on connection failures."""
        url = f"{self.base_url}{path}"
        kwargs.setdefault("timeout", self.timeout)

        # Retryable errors
        retryable_exceptions = (
            ConnectionError,
            Timeout,
            ChunkedEncodingError,
            RemoteDisconnected,
        )

        last_exception = None

        for attempt in range(self.max_retries):
            try:
                response = self.session.request(method, url, **kwargs)
                # Retry on 503 Service Unavailable (server overloaded)
                if response.status_code == 503 and attempt < self.max_retries - 1:
                    retry_after = response.headers.get("Retry-After", "5")
                    try:
                        wait_time = float(retry_after) + random.uniform(0, 2)
                    except ValueError:
                        wait_time = 5 + random.uniform(0, 2)
                    print(
                        f"[Retry {attempt + 1}/{self.max_retries}] {method} {path}: 503 Service Unavailable"
                    )
                    print(f"  Waiting {wait_time:.1f}s before retry...")
                    time.sleep(wait_time)
                    continue
                response.raise_for_status()
                return response.json()

            except retryable_exceptions as e:
                last_exception = e
                if attempt < self.max_retries - 1:
                    # Exponential backoff: 1s, 2s, 4s
                    wait_time = self.retry_delay * (2**attempt)
                    print(
                        f"[Retry {attempt + 1}/{self.max_retries}] {method} {path} failed: {type(e).__name__}"
                    )
                    print(f"  Waiting {wait_time}s before retry...")
                    time.sleep(wait_time)
                else:
                    # Last attempt failed
                    raise

            except Exception:
                # Non-retryable exceptions - raise immediately
                raise

        # Should not reach here, but just in case
        if last_exception:
            raise last_exception

    def create_sandbox(
        self,
        image: str,
        block_network: bool = True,
        sandbox_id: str | None = None,
        cpu: str | None = None,
        memory: str | None = None,
        timeout: int | None = None,
        wait_ready: bool = True,
        poll_interval: float = 1.0,
    ) -> SandboxInstance:
        """
        Create a new sandbox.

        Uses server-side wait (/wait endpoint) instead of client-side polling.
        Falls back to polling if /wait endpoint is unavailable.

        Args:
            image: Container image to use
            block_network: Whether to block network egress
            sandbox_id: Optional custom sandbox ID
            cpu: CPU request/limit (e.g., "4", "8", "2000m")
            memory: Memory request/limit (e.g., "16Gi", "32Gi")
            timeout: Timeout in seconds for sandbox to become ready (default: 3600)
            wait_ready: If True, wait until sandbox is ready (default: True)
            poll_interval: Seconds between status checks (only used in fallback polling)
        Returns:
            SandboxInstance
        """
        payload = {
            "image": image,
            "block_network": block_network,
        }

        # Apply prefix to sandbox_id if configured
        if self.prefix:
            if sandbox_id and sandbox_id.startswith(f"{self.prefix}-"):
                pass  # Already prefixed
            else:
                sandbox_id = f"{self.prefix}-{sandbox_id or uuid.uuid4().hex[:8]}"

        if sandbox_id:
            payload["sandbox_id"] = sandbox_id
        if cpu:
            payload["cpu"] = cpu
        if memory:
            payload["memory"] = memory
        if timeout:
            payload["timeout"] = timeout

        # Track the sandbox_id for cleanup on cancellation
        created_sandbox_id = None
        server_timeout = timeout or 3600

        try:
            # Create sandbox (returns immediately with pending status)
            response = self._request("POST", "/sandboxes", json=payload)
            created_sandbox_id = response["sandbox_id"]
            server_timeout = response.get("timeout", server_timeout)

            # Track this sandbox for cleanup on exit
            self._created_sandboxes.add(created_sandbox_id)
            if self._auto_cleanup:
                _pending_cleanup.add((self.base_url, created_sandbox_id, self.api_key))

            if not wait_ready:
                # Return immediately without waiting
                return SandboxInstance(
                    client=self, sandbox_id=created_sandbox_id, data=response
                )

            # Try server-side wait first (zero polling, instant notification)
            try:
                wait_response = self._request(
                    "GET",
                    f"/sandboxes/{created_sandbox_id}/wait",
                    params={"timeout": server_timeout},
                )

                if wait_response.get("ready") or wait_response.get("status") == "ready":
                    return SandboxInstance(
                        client=self, sandbox_id=created_sandbox_id, data=wait_response
                    )

                status = wait_response.get("status", "")
                if status == "failed":
                    msg = wait_response.get("status_message", "Pod failed")
                    raise RuntimeError(f"Sandbox {created_sandbox_id} failed: {msg}")

                raise RuntimeError(
                    f"Sandbox {created_sandbox_id} wait returned non-ready: "
                    f"status={status}, message={wait_response.get('status_message', '')}"
                )

            except requests.exceptions.HTTPError as e:
                if e.response is not None and e.response.status_code == 408:
                    raise TimeoutError(
                        f"Sandbox {created_sandbox_id} did not become ready within {server_timeout}s"
                    )
                if e.response is not None and e.response.status_code in (404, 405):
                    pass  # /wait endpoint not available — fall back to polling
                else:
                    raise

            # Fallback: client-side polling (for older server versions)
            start_time = time.time()
            while True:
                elapsed = time.time() - start_time
                if elapsed > server_timeout:
                    raise TimeoutError(
                        f"Sandbox {created_sandbox_id} did not become ready within {server_timeout}s"
                    )

                status_response = self._request(
                    "GET", f"/sandboxes/{created_sandbox_id}"
                )

                if (
                    status_response.get("ready")
                    or status_response.get("status") == "ready"
                ):
                    return SandboxInstance(
                        client=self, sandbox_id=created_sandbox_id, data=status_response
                    )

                # Fail fast if pod failed (not recoverable)
                status = status_response.get("status", "")
                if status == "failed":
                    msg = status_response.get("status_message", "Pod failed")
                    raise RuntimeError(f"Sandbox {created_sandbox_id} failed: {msg}")

                # Log scheduling issues but keep waiting (autoscaler may add nodes)
                if status == "unschedulable" and int(elapsed) % 30 == 0:
                    msg = status_response.get("status_message", "")
                    print(
                        f"[{created_sandbox_id}] Waiting for node... ({int(elapsed)}s elapsed): {msg[:100]}"
                    )

                time.sleep(poll_interval)

        except TimeoutError:
            # Timeout waiting for sandbox — cleanup immediately
            if created_sandbox_id:
                print(f"[Cleanup] Timeout, deleting sandbox {created_sandbox_id}...")
                self.delete_sandbox(created_sandbox_id)
            raise
        except (KeyboardInterrupt, SystemExit):
            # User cancelled - cleanup the sandbox
            if created_sandbox_id:
                print(
                    f"[Cleanup] Cancellation detected, deleting sandbox {created_sandbox_id}..."
                )
                self.delete_sandbox(created_sandbox_id)
            raise

    def delete_sandbox(self, sandbox_id: str) -> None:
        """
        Delete a sandbox by ID.

        Args:
            sandbox_id: Sandbox ID to delete
        """
        # Remove from tracking
        self._created_sandboxes.discard(sandbox_id)
        _pending_cleanup.discard((self.base_url, sandbox_id, self.api_key))
        try:
            self._request("DELETE", f"/sandboxes/{sandbox_id}")
        except Exception:
            pass  # Ignore errors during cleanup

    def get_sandbox(self, sandbox_id: str) -> SandboxInstance:
        """
        Get an existing sandbox by ID.

        Args:
            sandbox_id: Sandbox ID

        Returns:
            SandboxInstance
        """
        response = self._request("GET", f"/sandboxes/{sandbox_id}")
        return SandboxInstance(
            client=self, sandbox_id=response["sandbox_id"], data=response
        )

    def health(self) -> dict:
        """Check service health."""
        return self._request("GET", "/health")

    def close(self) -> None:
        """Close the session and release resources."""
        self.session.close()

    def __enter__(self):
        """Context manager entry."""
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        """Context manager exit - close session."""
        self.close()

    def __repr__(self) -> str:
        return f"SandboxClient(base_url={self.base_url})"


# Alias for backward compatibility
Sandbox = SandboxClient


# =============================================================================
# Async Client Implementation
# =============================================================================


class AsyncSandboxInstance:
    """An async sandbox instance that supports command execution."""

    def __init__(self, client: "AsyncSandboxClient", sandbox_id: str, data: dict):
        """Initialize sandbox instance."""
        self._client = client
        self.sandbox_id = sandbox_id
        self.namespace = data.get("namespace")
        self.image = data.get("image")
        self.block_network = data.get("block_network")
        self._deleted = False
        self._heartbeat_task: asyncio.Task | None = None

    def start_heartbeat(self, interval: int = 60) -> None:
        """Start sending periodic heartbeats to keep the sandbox alive.

        Args:
            interval: Seconds between heartbeats (default: 60)
        """
        if self._heartbeat_task and not self._heartbeat_task.done():
            return  # Already running

        async def _heartbeat_loop():
            while not self._deleted:
                await asyncio.sleep(interval)
                if self._deleted:
                    break
                try:
                    await self._client._request(
                        "POST", f"/sandboxes/{self.sandbox_id}/heartbeat"
                    )
                except asyncio.CancelledError:
                    break
                except Exception:
                    pass  # Best effort

        self._heartbeat_task = asyncio.ensure_future(_heartbeat_loop())

    def stop_heartbeat(self) -> None:
        """Stop the heartbeat background task."""
        if self._heartbeat_task and not self._heartbeat_task.done():
            self._heartbeat_task.cancel()
            self._heartbeat_task = None

    async def heartbeat(self) -> None:
        """Send a single heartbeat now.

        The async counterpart to :meth:`SandboxInstance.heartbeat`; see there
        for when to prefer this over :meth:`start_heartbeat`.
        """
        await self._client._request("POST", f"/sandboxes/{self.sandbox_id}/heartbeat")

    async def expose_service(
        self,
        port: int,
        ttl_seconds: int | None = None,
        wait_ready: bool = False,
        health_path: str = "/health",
        ready_timeout: int = 60,
    ) -> "ServiceEndpoint":
        """Expose a service running inside the sandbox and return its URL.

        The async counterpart to :meth:`SandboxInstance.expose_service`; see
        there for the full description and the bearer-credential caveat.
        """
        payload = {
            "port": port,
            "wait_ready": wait_ready,
            "health_path": health_path,
            "ready_timeout": ready_timeout,
        }
        if ttl_seconds is not None:
            payload["ttl_seconds"] = ttl_seconds

        data = await self._client._request(
            "POST", f"/sandboxes/{self.sandbox_id}/services", json=payload
        )
        return ServiceEndpoint(data)

    async def list_services(self) -> list[int]:
        """Return the ports this sandbox currently exposes."""
        data = await self._client._request(
            "GET", f"/sandboxes/{self.sandbox_id}/services"
        )
        return data.get("ports", [])

    async def revoke_service(self, port: int) -> None:
        """Stop exposing *port*.

        Effective immediately, including for URLs that have not yet expired.
        """
        await self._client._request(
            "DELETE", f"/sandboxes/{self.sandbox_id}/services/{port}"
        )

    async def exec(
        self,
        command: str | list[str],
        timeout: int | None = None,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        wait: bool = True,
        poll_interval: float = 0.1,
        login_shell: bool = False,
        pty: bool = False,
        rows: int = 24,
        cols: int = 80,
    ) -> Union[JobResult, "AsyncContainerProcess"]:
        """
        Execute a command in the sandbox.

        Args:
            command: Command to execute (string or list)
            timeout: Timeout in seconds
            cwd: Working directory
            env: Environment variables
            wait: If True, wait for job to complete
            poll_interval: Polling interval when waiting
            login_shell: If True, use login shell (bash -lc) instead of regular shell (bash -c)
            pty: If True, open a streaming PTY session and return an
                ``AsyncContainerProcess`` handle (Modal-style).
            rows, cols: initial PTY window size (only when ``pty=True``).

        Returns:
            JobResult, or AsyncContainerProcess when ``pty=True``.
        """
        if self._deleted:
            raise RuntimeError("Sandbox has been deleted")

        if pty:
            from orchard_env.client.process import (
                AsyncContainerProcess,
                _build_init_frame,
                _ws_url,
            )

            cmd_list = (
                command if isinstance(command, list) else ["bash", "-c", str(command)]
            )
            init = _build_init_frame(
                cmd_list,
                env=env,
                workdir=cwd,
                rows=rows,
                cols=cols,
                login_shell=login_shell,
            )
            url = _ws_url(self._client.base_url, self.sandbox_id, self._client.api_key)
            # Reuse the client's aiohttp session so we don't open a new TCP pool.
            session = await self._client._get_session()
            return await AsyncContainerProcess.connect(url, init, session=session)

        exec_timeout = timeout or 300

        # Try server-side wait first (zero polling)
        try:
            server_wait_timeout = aiohttp.ClientTimeout(
                total=exec_timeout + 60,
                sock_connect=30,
                sock_read=exec_timeout + 60,
            )
            response = await self._client._request(
                "POST",
                f"/sandboxes/{self.sandbox_id}/exec",
                json={
                    "command": command,
                    "timeout_seconds": timeout,
                    "cwd": cwd,
                    "env": env,
                    "login_shell": login_shell,
                    "wait": True,
                },
                timeout=server_wait_timeout,
            )

            # Only return if the job truly completed (succeeded or failed).
            # A "running" status means the server-side wait timed out and
            # the job is still executing — we must NOT treat it as complete.
            if response.get("status") in ("succeeded", "failed"):
                return JobResult(response)

            # Fallback: server returned only job_id (old server version)
            job_id = response["job_id"]
        except Exception:
            # If wait=True request fails, fall back to non-wait + polling
            response = await self._client._request(
                "POST",
                f"/sandboxes/{self.sandbox_id}/exec",
                json={
                    "command": command,
                    "timeout_seconds": timeout,
                    "cwd": cwd,
                    "env": env,
                    "login_shell": login_shell,
                },
            )
            job_id = response["job_id"]

        if not wait:
            # Return immediately with queued status
            return JobResult(
                {
                    "job_id": job_id,
                    "sandbox_id": self.sandbox_id,
                    "command": str(command),
                    "status": "queued",
                    "created_at": time.time(),
                }
            )

        # Fallback: client-side polling (for older server versions)
        deadline = asyncio.get_running_loop().time() + exec_timeout + 60
        while True:
            result = await self.get_job(job_id)

            if result.is_complete:
                return result

            if asyncio.get_running_loop().time() >= deadline:
                raise TimeoutError(
                    f"Job {job_id} did not complete within {exec_timeout}s"
                )

            await asyncio.sleep(poll_interval)

    async def apply_patch(self, patch: str, timeout: int = 30) -> dict:
        """
        Apply a git patch in the sandbox.

        Args:
            patch: Unified diff patch content
            timeout: Timeout in seconds

        Returns:
            Dictionary with success status and output
        """
        if self._deleted:
            raise RuntimeError("Sandbox has been deleted")

        response = await self._client._request(
            "POST",
            f"/sandboxes/{self.sandbox_id}/apply_patch",
            json={
                "patch": patch,
                "timeout_seconds": timeout,
            },
        )

        return response

    async def get_job(self, job_id: str) -> JobResult:
        """
        Get job status and results.

        Args:
            job_id: Job ID

        Returns:
            JobResult with execution results
        """
        response = await self._client._request("GET", f"/jobs/{job_id}")
        return JobResult(response)

    async def delete(self) -> None:
        """Delete the sandbox (async version)."""
        if self._deleted:
            return

        # Stop heartbeat
        self.stop_heartbeat()

        # Remove from tracking
        self._client._created_sandboxes.discard(self.sandbox_id)
        _pending_cleanup.discard(
            (self._client.base_url, self.sandbox_id, self._client.api_key)
        )

        await self._client._request("DELETE", f"/sandboxes/{self.sandbox_id}")
        self._deleted = True

    def delete_sync(self) -> None:
        """Delete the sandbox synchronously (safe to call when event loop is closed).

        Use this method in __del__ or cleanup handlers where async is not available.
        """
        if self._deleted:
            return

        # Stop heartbeat
        self.stop_heartbeat()

        # Remove from tracking
        self._client._created_sandboxes.discard(self.sandbox_id)
        _pending_cleanup.discard(
            (self._client.base_url, self.sandbox_id, self._client.api_key)
        )

        try:
            headers = (
                {"X-API-Key": self._client.api_key} if self._client.api_key else {}
            )
            requests.delete(
                f"{self._client.base_url}/sandboxes/{self.sandbox_id}",
                headers=headers,
                timeout=10,
            )
        except Exception:
            pass  # Best effort cleanup

        self._deleted = True

    async def upload_file(self, local_path: str, remote_path: str) -> dict:
        """
        Upload a file to the sandbox.

        Args:
            local_path: Local file path to upload
            remote_path: Destination path in the sandbox

        Returns:
            Dictionary with success status and size
        """
        import base64

        if self._deleted:
            raise RuntimeError("Sandbox has been deleted")

        with open(local_path, "rb") as f:
            content = base64.b64encode(f.read()).decode("utf-8")

        return await self._client._request(
            "POST",
            f"/sandboxes/{self.sandbox_id}/files",
            json={
                "path": remote_path,
                "content": content,
            },
        )

    async def upload_content(self, content: bytes, remote_path: str) -> dict:
        """
        Upload content directly to the sandbox.

        Args:
            content: File content as bytes
            remote_path: Destination path in the sandbox

        Returns:
            Dictionary with success status and size
        """
        import base64

        if self._deleted:
            raise RuntimeError("Sandbox has been deleted")

        return await self._client._request(
            "POST",
            f"/sandboxes/{self.sandbox_id}/files",
            json={
                "path": remote_path,
                "content": base64.b64encode(content).decode("utf-8"),
            },
        )

    async def download_file(self, remote_path: str, local_path: str) -> int:
        """
        Download a file from the sandbox to local.

        Args:
            remote_path: Source path in the sandbox
            local_path: Local destination path

        Returns:
            Size of downloaded file in bytes
        """
        import base64

        if self._deleted:
            raise RuntimeError("Sandbox has been deleted")

        response = await self._client._request(
            "GET", f"/sandboxes/{self.sandbox_id}/files", params={"path": remote_path}
        )

        content = base64.b64decode(response["content"])
        with open(local_path, "wb") as f:
            f.write(content)

        return len(content)

    async def download_content(self, remote_path: str) -> bytes:
        """
        Download file content from the sandbox.

        Args:
            remote_path: Source path in the sandbox

        Returns:
            File content as bytes
        """
        import base64

        if self._deleted:
            raise RuntimeError("Sandbox has been deleted")

        response = await self._client._request(
            "GET", f"/sandboxes/{self.sandbox_id}/files", params={"path": remote_path}
        )

        return base64.b64decode(response["content"])

    async def list_files(self, remote_path: str = "/") -> list[dict]:
        """
        List files in a directory in the sandbox.

        Args:
            remote_path: Directory path in the sandbox (default: "/")

        Returns:
            List of file info dicts with name, type, size, modified
        """
        if self._deleted:
            raise RuntimeError("Sandbox has been deleted")

        response = await self._client._request(
            "GET",
            f"/sandboxes/{self.sandbox_id}/files/list",
            params={"path": remote_path},
        )

        return response["files"]

    async def check_env(self, verbose: bool = True) -> dict:
        """Check sandbox environment: bashrc loading, conda activation, PATH, python.

        Runs two probes (with and without login shell) to reliably detect whether
        bashrc was sourced, conda is activated, and which python/python3 are available.

        Detection: compares HISTCONTROL between login and bare shells. Standard
        bashrc sets HISTCONTROL=ignoredups:ignorespace, so if it differs the
        bashrc was sourced.

        Args:
            verbose: If True, print a summary to stdout (default: True)

        Returns:
            Dict with keys: bashrc_loaded, conda_available, conda_env,
            conda_prefix, path, bare_path, python, python3, python_version
        """
        probe_cmd = (
            'echo "__HAS_BASHRC=$( [ -f ~/.bashrc ] && echo yes || echo no )"; '
            'echo "__HISTCONTROL=${HISTCONTROL:-}"; '
            'echo "__HAS_CONDA=$(command -v conda >/dev/null 2>&1 && echo yes || echo no)"; '
            'echo "__CONDA_DEFAULT_ENV=${CONDA_DEFAULT_ENV:-}"; '
            'echo "__CONDA_PREFIX=${CONDA_PREFIX:-}"; '
            'echo "__CONDA_EXE=$(echo ${CONDA_EXE:-none})"; '
            'echo "__PATH=$PATH"; '
            'echo "__PYTHON=$(command -v python 2>/dev/null || echo none)"; '
            'echo "__PYTHON3=$(command -v python3 2>/dev/null || echo none)"; '
            'echo "__PYTHON_VERSION=$(python --version 2>/dev/null || echo none)"'
        )

        def _parse_probe(stdout: str) -> dict:
            info = {}
            for line in (stdout or "").splitlines():
                if line.startswith("__"):
                    key, _, val = line.partition("=")
                    info[key.lstrip("_")] = val
            return info

        # Probe 1: bare shell (no bashrc) to get baseline
        bare_result = await self.exec(
            'echo "__BARE_PATH=$PATH"; echo "__BARE_HISTCONTROL=${HISTCONTROL:-}"',
            timeout=10,
            login_shell=False,
        )
        bare_info = _parse_probe(bare_result.stdout)
        bare_path = bare_info.get("BARE_PATH", "")
        bare_histctl = bare_info.get("BARE_HISTCONTROL", "")

        # Probe 2: login shell to get full environment
        login_result = await self.exec(probe_cmd, timeout=15, login_shell=True)
        info = _parse_probe(login_result.stdout)

        login_path = info.get("PATH", "")
        login_histctl = info.get("HISTCONTROL", "")
        has_bashrc = info.get("HAS_BASHRC", "no") == "yes"
        # bashrc is "loaded" if: file exists AND environment changed
        # (HISTCONTROL is set by virtually every default bashrc)
        bashrc_loaded = has_bashrc and (
            login_histctl != bare_histctl or login_path != bare_path
        )

        conda_exe = info.get("CONDA_EXE", "none")
        conda_available = "yes" in info.get("HAS_CONDA", "no")
        # conda function may exist but the binary may be broken
        # (e.g. conda Python package uninstalled). Check if CONDA_EXE works.
        conda_broken = False
        if conda_available and conda_exe != "none":
            test = await self.exec(
                f"{conda_exe} --version 2>&1 || true",
                timeout=5,
                login_shell=False,
            )
            if "ModuleNotFoundError" in (test.stdout + test.stderr):
                conda_broken = True

        env_info = {
            "bashrc_loaded": bashrc_loaded,
            "has_bashrc": has_bashrc,
            "conda_available": conda_available and not conda_broken,
            "conda_broken": conda_broken,
            "conda_exe": conda_exe,
            "conda_env": info.get("CONDA_DEFAULT_ENV", "") or None,
            "conda_prefix": info.get("CONDA_PREFIX", "") or None,
            "path": login_path,
            "bare_path": bare_path,
            "python": info.get("PYTHON", "none"),
            "python3": info.get("PYTHON3", "none"),
            "python_version": info.get("PYTHON_VERSION", "none"),
        }

        if verbose:
            sid = self.sandbox_id[:12]
            print(f"[{sid}] === Environment Check ===")
            print(f"  ~/.bashrc exists: {'yes' if has_bashrc else 'no'}")
            print(f"  bashrc loaded   : {'yes' if bashrc_loaded else 'NO'}")
            if conda_broken:
                print(f"  conda available : BROKEN (module missing in {conda_exe})")
            else:
                print(f"  conda available : {'yes' if conda_available else 'no'}")
            if conda_available and not conda_broken:
                print(f"  conda env       : {env_info['conda_env'] or '(none)'}")
                print(f"  conda prefix    : {env_info['conda_prefix'] or '(none)'}")
            print(f"  conda exe       : {conda_exe}")
            print(f"  python          : {env_info['python']}")
            print(f"  python3         : {env_info['python3']}")
            print(f"  python version  : {env_info['python_version']}")
            print(
                f"  PATH (login)    : {login_path[:120]}{'...' if len(login_path) > 120 else ''}"
            )
            if login_path != bare_path:
                print(
                    f"  PATH (bare)     : {bare_path[:120]}{'...' if len(bare_path) > 120 else ''}"
                )

        return env_info

    async def __aenter__(self):
        """Async context manager entry."""
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """Async context manager exit - cleanup sandbox."""
        try:
            await self.delete()
        except Exception:
            # Ignore errors during cleanup (e.g., event loop already closed)
            pass

    def __repr__(self) -> str:
        status = "deleted" if self._deleted else "active"
        return f"AsyncSandboxInstance(id={self.sandbox_id}, status={status})"


class AsyncSandboxClient:
    """Async sandbox client for creating and managing sandbox instances.

    Automatically tracks all created sandboxes and cleans them up on program exit.
    """

    def __init__(
        self,
        base_url: str | None = None,
        timeout: int = 1200,
        auto_cleanup: bool = True,
        api_key: str | None = None,
        prefix: str | None = None,
    ):
        """
        Initialize async sandbox client.

        Args:
            base_url: Base URL of the orchestrator service. If not provided, falls back to
                      SANDBOX_BASE_URL environment variable, then defaults to http://localhost:8000.
            timeout: Default request timeout in seconds (default: 1200s = 20 minutes)
            auto_cleanup: If True, automatically cleanup sandboxes on program exit (default: True)
            api_key: API key for authentication. If not provided, falls back to
                     SANDBOX_API_KEY environment variable.
            prefix: Optional prefix for sandbox IDs. If not provided, falls back to
                    SANDBOX_PREFIX environment variable. When set, all sandbox IDs created
                    by this client will be prefixed with this value (e.g., "myapp-abc123").
        """
        # Use provided base_url or fall back to environment variable
        self.base_url = (
            base_url or os.environ.get("SANDBOX_BASE_URL") or "http://localhost:8000"
        ).rstrip("/")
        self.timeout = timeout
        # Use provided api_key or fall back to environment variable
        self.api_key = api_key or os.environ.get("SANDBOX_API_KEY")
        # Use provided prefix or fall back to environment variable
        self.prefix = prefix or os.environ.get("SANDBOX_PREFIX")
        self._session: aiohttp.ClientSession | None = None
        self.max_retries = 3
        self.retry_delay = 1  # Initial delay in seconds
        self._created_sandboxes: set[str] = set()  # Track created sandbox IDs
        self._auto_cleanup = auto_cleanup

        if auto_cleanup:
            _register_cleanup()

    async def _get_session(self) -> aiohttp.ClientSession:
        """Get or create aiohttp session."""
        if self._session is None or self._session.closed:
            # Check if event loop is running/valid before creating session
            try:
                loop = asyncio.get_running_loop()
                if loop.is_closed():
                    raise RuntimeError("Event loop is closed, cannot create session")
            except RuntimeError:
                # No running event loop - this will fail anyway
                raise RuntimeError(
                    "No running event loop, cannot create aiohttp session"
                )

            timeout_config = aiohttp.ClientTimeout(
                total=self.timeout,
                sock_connect=30,  # 30s to establish TCP connection
                sock_read=self.timeout,  # read can be long for polling
            )
            connector = aiohttp.TCPConnector(
                limit=200,  # Max 200 concurrent connections
                limit_per_host=200,  # All go to same host
                keepalive_timeout=30,
                enable_cleanup_closed=True,
            )
            headers = {}
            if self.api_key:
                headers["X-API-Key"] = self.api_key
            self._session = aiohttp.ClientSession(
                timeout=timeout_config, connector=connector, headers=headers
            )
        return self._session

    async def _request(self, method: str, path: str, **kwargs) -> dict:
        """Make async HTTP request with automatic retry on connection failures."""
        url = f"{self.base_url}{path}"
        session = await self._get_session()

        # Retryable errors (connection-level only, NOT HTTP response errors)
        retryable_exceptions = (
            aiohttp.ClientConnectionError,
            aiohttp.ServerDisconnectedError,
            asyncio.TimeoutError,
        )

        last_exception = None

        for attempt in range(self.max_retries):
            try:
                async with session.request(method, url, **kwargs) as response:
                    # Retry on 503 Service Unavailable (server overloaded)
                    if response.status == 503 and attempt < self.max_retries - 1:
                        retry_after = response.headers.get("Retry-After", "5")
                        try:
                            wait_time = float(retry_after) + random.uniform(0, 2)
                        except ValueError:
                            wait_time = 5 + random.uniform(0, 2)
                        print(
                            f"[Retry {attempt + 1}/{self.max_retries}] {method} {path}: 503 Service Unavailable"
                        )
                        print(f"  Waiting {wait_time:.1f}s before retry...")
                        await asyncio.sleep(wait_time)
                        continue
                    response.raise_for_status()
                    return await response.json()

            except retryable_exceptions as e:
                last_exception = e
                if attempt < self.max_retries - 1:
                    # Exponential backoff with jitter to avoid thundering herd
                    base_wait = self.retry_delay * (2**attempt)
                    jitter = random.uniform(0, base_wait * 0.5)
                    wait_time = base_wait + jitter
                    print(
                        f"[Retry {attempt + 1}/{self.max_retries}] {method} {path} failed: {type(e).__name__}"
                    )
                    print(f"  Waiting {wait_time:.1f}s before retry...")
                    await asyncio.sleep(wait_time)
                else:
                    # Last attempt failed
                    raise

            except Exception:
                # Non-retryable exceptions - raise immediately
                raise

        # Should not reach here, but just in case
        if last_exception:
            raise last_exception

    async def create_sandbox(
        self,
        image: str,
        block_network: bool = True,
        sandbox_id: str | None = None,
        cpu: str | None = None,
        memory: str | None = None,
        timeout: int | None = None,
        wait_ready: bool = True,
        poll_interval: float = 2.0,
    ) -> AsyncSandboxInstance:
        """
        Create a new sandbox.

        Uses server-side wait (/wait endpoint) instead of client-side polling.
        This eliminates thousands of polling API calls at scale.
        Falls back to client-side polling if the /wait endpoint is unavailable.

        Args:
            image: Container image to use
            block_network: Whether to block network egress
            sandbox_id: Optional custom sandbox ID
            cpu: CPU request/limit (e.g., "4", "8", "2000m")
            memory: Memory request/limit (e.g., "16Gi", "32Gi")
            timeout: Timeout in seconds for sandbox to become ready (default: 3600)
            wait_ready: If True, wait until sandbox is ready (default: True)
            poll_interval: Seconds between status checks (only used in fallback polling)

        Returns:
            AsyncSandboxInstance
        """
        payload = {
            "image": image,
            "block_network": block_network,
        }

        # Apply prefix to sandbox_id if configured
        if self.prefix:
            if sandbox_id and sandbox_id.startswith(f"{self.prefix}-"):
                pass  # Already prefixed
            else:
                sandbox_id = f"{self.prefix}-{sandbox_id or uuid.uuid4().hex[:8]}"

        if sandbox_id:
            payload["sandbox_id"] = sandbox_id
        if cpu:
            payload["cpu"] = cpu
        if memory:
            payload["memory"] = memory
        if timeout:
            payload["timeout"] = timeout

        # Track the sandbox_id for cleanup on cancellation
        created_sandbox_id = None
        server_timeout = timeout or 3600

        try:
            # Create sandbox (returns immediately with pending status)
            response = await self._request("POST", "/sandboxes", json=payload)
            created_sandbox_id = response["sandbox_id"]
            server_timeout = response.get("timeout", server_timeout)

            # Track this sandbox for cleanup on exit
            self._created_sandboxes.add(created_sandbox_id)
            if self._auto_cleanup:
                _pending_cleanup.add((self.base_url, created_sandbox_id, self.api_key))

            if not wait_ready:
                # Return immediately without waiting
                return AsyncSandboxInstance(
                    client=self, sandbox_id=created_sandbox_id, data=response
                )

            # Try server-side wait first (zero polling, instant notification)
            try:
                # Use a long timeout for the wait request since it blocks server-side
                wait_timeout = aiohttp.ClientTimeout(
                    total=server_timeout + 30,  # slightly longer than server timeout
                    sock_connect=30,
                    sock_read=server_timeout + 30,
                )
                wait_response = await self._request(
                    "GET",
                    f"/sandboxes/{created_sandbox_id}/wait",
                    params={"timeout": server_timeout},
                    timeout=wait_timeout,
                )

                if wait_response.get("ready") or wait_response.get("status") == "ready":
                    return AsyncSandboxInstance(
                        client=self, sandbox_id=created_sandbox_id, data=wait_response
                    )

                # Pod failed
                status = wait_response.get("status", "")
                if status == "failed":
                    msg = wait_response.get("status_message", "Pod failed")
                    raise RuntimeError(f"Sandbox {created_sandbox_id} failed: {msg}")

                # Other non-ready status — shouldn't happen but handle gracefully
                raise RuntimeError(
                    f"Sandbox {created_sandbox_id} wait returned non-ready: "
                    f"status={status}, message={wait_response.get('status_message', '')}"
                )

            except aiohttp.ClientResponseError as e:
                if e.status == 408:
                    # Server-side timeout
                    raise TimeoutError(
                        f"Sandbox {created_sandbox_id} did not become ready within {server_timeout}s"
                    )
                if e.status in (404, 405):
                    # /wait endpoint not available — fall back to polling
                    pass
                else:
                    raise
            except TimeoutError:
                raise TimeoutError(
                    f"Sandbox {created_sandbox_id} did not become ready within {server_timeout}s"
                )

            # Fallback: client-side polling (for older server versions)
            start_time = asyncio.get_event_loop().time()
            while True:
                elapsed = asyncio.get_event_loop().time() - start_time
                if elapsed > server_timeout:
                    raise TimeoutError(
                        f"Sandbox {created_sandbox_id} did not become ready within {server_timeout}s"
                    )

                status_response = await self._request(
                    "GET", f"/sandboxes/{created_sandbox_id}"
                )

                if (
                    status_response.get("ready")
                    or status_response.get("status") == "ready"
                ):
                    return AsyncSandboxInstance(
                        client=self, sandbox_id=created_sandbox_id, data=status_response
                    )

                # Fail fast if pod failed (not recoverable)
                status = status_response.get("status", "")
                if status == "failed":
                    msg = status_response.get("status_message", "Pod failed")
                    raise RuntimeError(f"Sandbox {created_sandbox_id} failed: {msg}")

                # Log scheduling issues but keep waiting (autoscaler may add nodes)
                if status == "unschedulable" and int(elapsed) % 30 == 0:
                    msg = status_response.get("status_message", "")
                    print(
                        f"[{created_sandbox_id}] Waiting for node... ({int(elapsed)}s elapsed): {msg[:100]}"
                    )

                await asyncio.sleep(poll_interval)

        except TimeoutError:
            # Timeout waiting for sandbox — cleanup immediately
            if created_sandbox_id:
                print(f"[Cleanup] Timeout, deleting sandbox {created_sandbox_id}...")
                await self.delete_sandbox(created_sandbox_id)
            raise
        except asyncio.CancelledError:
            # Request was cancelled - cleanup the sandbox
            if created_sandbox_id:
                print(
                    f"[Cleanup] Cancellation detected, deleting sandbox {created_sandbox_id}..."
                )
                await self.delete_sandbox(created_sandbox_id)
            raise

    async def delete_sandbox(self, sandbox_id: str) -> None:
        """
        Delete a sandbox by ID.

        Args:
            sandbox_id: Sandbox ID to delete
        """
        # Remove from tracking
        self._created_sandboxes.discard(sandbox_id)
        _pending_cleanup.discard((self.base_url, sandbox_id, self.api_key))
        try:
            await self._request("DELETE", f"/sandboxes/{sandbox_id}")
        except Exception:
            pass  # Ignore errors during cleanup

    async def cleanup_all(self) -> None:
        """Delete all sandboxes created by this client."""
        sandbox_ids = list(self._created_sandboxes)
        if sandbox_ids:
            print(f"[Cleanup] Deleting {len(sandbox_ids)} sandbox(es)...")
        for sandbox_id in sandbox_ids:
            await self.delete_sandbox(sandbox_id)

    async def get_sandbox(self, sandbox_id: str) -> AsyncSandboxInstance:
        """
        Get an existing sandbox by ID.

        Args:
            sandbox_id: Sandbox ID

        Returns:
            AsyncSandboxInstance
        """
        response = await self._request("GET", f"/sandboxes/{sandbox_id}")
        return AsyncSandboxInstance(
            client=self, sandbox_id=response["sandbox_id"], data=response
        )

    async def health(self) -> dict:
        """Check service health."""
        return await self._request("GET", "/health")

    async def close(self, cleanup: bool = True) -> None:
        """Close the session and release resources.

        Args:
            cleanup: If True, delete all tracked sandboxes before closing (default: True)
        """
        if cleanup and self._auto_cleanup:
            await self.cleanup_all()

        if self._session and not self._session.closed:
            await self._session.close()
            # Give the underlying connections time to close gracefully
            # This helps prevent "Event loop is closed" errors on Windows
            await asyncio.sleep(0.25)
        self._session = None

    async def __aenter__(self):
        """Async context manager entry."""
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """Async context manager exit - cleanup sandboxes and close session."""
        try:
            await self.close(cleanup=True)
        except Exception:
            # Ignore errors during cleanup (e.g., event loop already closed)
            pass

    def __repr__(self) -> str:
        return f"AsyncSandboxClient(base_url={self.base_url})"


# Alias for backward compatibility
AsyncSandbox = AsyncSandboxClient


if __name__ == "__main__":
    # Example usage
    print("=== SandboxClient Example ===")

    # Create sandbox client
    with SandboxClient(
        os.getenv("SANDBOX_BASE_URL", "http://localhost:8000")
    ) as client:
        # Check health
        print("Health:", client.health())

        # Create a sandbox with context manager for automatic cleanup
        # Works with ANY image — agent is injected automatically via init container
        with client.create_sandbox(
            "mirror.gcr.io/swerebenchv2/behat-gherkin:343-e522894", block_network=False
        ) as sandbox:
            print(f"Created {sandbox}")

            # Check environment (bashrc, conda, python)
            env_info = sandbox.check_env()

            # Execute command
            result = sandbox.exec("echo 'Hello from sandbox!'")
            print(f"Exit code: {result.exit_code}")
            print(f"Output: {result.stdout}")
            # time.sleep(3600)

            # List OS info
            result = sandbox.exec("cat /etc/os-release | head -2")
            print(f"OS: {result.stdout.strip()}")

            result = sandbox.exec(
                "source /opt/miniconda3/etc/profile.d/conda.sh && conda activate testbed ; which python"
            )
            print(f"Python: {result.stdout.strip()}")

            result = sandbox.exec(
                "source /opt/miniconda3/etc/profile.d/conda.sh && conda activate testbed ; which python",
                login_shell=True,
            )
            print(f"Python: {result.stdout.strip()}")

            result = sandbox.exec("which conda")
            print(f"std: {result.stdout.strip()}")
            print(f"err: {result.stderr.strip()}")

            result = sandbox.exec("which conda", login_shell=True)
            print(f"which conda std: {result.stdout.strip()}")
            print(f"which conda err: {result.stderr.strip()}")

            result = sandbox.exec(
                "conda actiavte testbed ; which python", login_shell=True
            )
            print(f"conda actiavte testbed std: {result.stdout.strip()}")
            print(f"conda actiavte testbed err: {result.stderr.strip()}")

            result = sandbox.exec("pwd")
            print(f"Current directory: {result.stdout.strip()}")

            result = sandbox.exec("pwd", login_shell=True)
            print(f"Current directory with login shell: {result.stdout.strip()}")

            # Create a file
            result = sandbox.exec("echo 'test content' > /tmp/test.txt")
            print(f"File created: {result.succeeded}")

            # Read the file
            result = sandbox.exec("cat /tmp/test.txt")
            print(f"File content: {result.stdout.strip()}")

            # Sleep to test long-running command
            print("Sleeping for 600 seconds...")
            result = sandbox.exec("sleep 600 && echo 'Awake now!'", timeout=10)
            print(f"Sleep result: {result.stdout.strip()}")

            import pdb

            pdb.set_trace()

        print("Sandbox automatically deleted")
