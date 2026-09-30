# Python SDK reference

Import everything from the top-level package:

```python
from orchard_env import (
    SandboxClient,
    AsyncSandboxClient,
    SandboxInstance,
    AsyncSandboxInstance,
    JobResult,
)
```

See the [README](../README.md) for installation and quickstart examples.

## `SandboxClient` / `AsyncSandboxClient`

```python
SandboxClient(
    base_url: Optional[str] = None,  # falls back to SANDBOX_BASE_URL
    timeout: int = 1200,             # request timeout in seconds
    auto_cleanup: bool = True,       # delete tracked sandboxes on exit
    api_key: Optional[str] = None,   # falls back to SANDBOX_API_KEY
    prefix: Optional[str] = None,    # falls back to SANDBOX_PREFIX
)
```

| Method | Description |
| --- | --- |
| `create_sandbox(image, ...)` | Create a new sandbox container |
| `get_sandbox(sandbox_id)` | Get an existing sandbox by ID |
| `delete_sandbox(sandbox_id)` | Delete a sandbox |
| `cleanup_all()` | Delete every sandbox created by this client |

Both clients are context managers, and that is the recommended way to use them —
leaving the block guarantees cleanup.

### Sandbox creation options

```python
sandbox = client.create_sandbox(
    image="python:3.11-slim",    # container image
    block_network=True,          # block outbound network (default: True)
    sandbox_id=None,             # custom ID (auto-generated when None)
    cpu="4",                     # CPU cores, e.g. "4" or "2000m"
    memory="16Gi",               # memory limit
    timeout=3600,                # seconds to wait for readiness
    wait_ready=True,
    poll_interval=1.0,
)
```

Sandboxes have **no outbound network access by default**. Anything that reaches
the internet — `pip install`, `git clone`, or an agent harness calling a model
API — needs `block_network=False`.

## `SandboxInstance` / `AsyncSandboxInstance`

| Method | Description |
| --- | --- |
| `exec(command, timeout, cwd, env)` | Run a command in the sandbox |
| `apply_patch(patch, timeout)` | Apply a git patch |
| `upload_file(local_path, remote_path)` | Upload a file |
| `upload_content(content, remote_path)` | Upload bytes/str directly |
| `download_file(remote_path, local_path)` | Download a file |
| `download_content(remote_path)` | Download file content as bytes |
| `list_files(remote_path)` | List a directory |
| `expose_service(port, ...)` | Expose a service inside the sandbox, returns `ServiceEndpoint` |
| `list_services()` | List exposed ports |
| `revoke_service(port)` | Stop exposing a port |
| `heartbeat()` | Send one heartbeat now |
| `start_heartbeat(interval)` / `stop_heartbeat()` | Background heartbeat loop |
| `get_job(job_id)` | Fetch job status and results |
| `delete()` | Delete the sandbox |

`exec()` also accepts `login_shell=True` (runs under `bash -lc` instead of
`bash -c`) and `pty=True` for an interactive session backed by
`ContainerProcess` / `AsyncContainerProcess`.

## `ServiceEndpoint`

`exec` and the file methods cover agents that *run commands*. When the workload
is instead a long-running server inside the sandbox — an OpenEnv environment
server, an MCP server, a dev server — `expose_service()` returns a URL a client
outside the cluster can talk to.

```python
sandbox.exec("nohup python -m http.server 8000 > /tmp/log 2>&1 &")

endpoint = sandbox.expose_service(8000, wait_ready=True, health_path="/")
endpoint.url         # https://orchestrator/s/<token>
endpoint.ws_url      # wss://orchestrator/s/<token>
endpoint.port
endpoint.expires_at

requests.get(f"{endpoint.url}/index.html")
sandbox.revoke_service(8000)
```

`wait_ready=True` is worth setting: a sandbox reports ready once the *in-pod
agent* is up, which can be well before a server you just launched has finished
binding.

> **The URL is a bearer credential.** Anyone holding it can reach that port on
> that sandbox until `expires_at`. Keep it out of logs, scope it with
> `ttl_seconds`, and call `revoke_service()` when finished — revocation takes
> effect immediately, even for URLs that have not expired. `repr()` deliberately
> omits the URL so it does not leak into log lines.

Requires `ENABLE_SERVICE_ENDPOINTS=true` on the orchestrator.

## `JobResult`

```python
result = sandbox.exec("ls -la")

result.job_id       # unique job identifier
result.status       # "queued" | "running" | "succeeded" | "failed"
result.stdout
result.stderr
result.exit_code
result.succeeded    # True if exit_code == 0
result.failed       # True if status == "failed"
result.is_complete  # True once the job has finished
```

> A `"running"` status returned from `exec(wait=True)` means the *server-side*
> wait timed out, not that the job finished. The client transparently falls back
> to polling `GET /jobs/{id}` in that case.

## Auto cleanup

The client tracks every sandbox it creates and cleans them up on:

1. **Context manager exit** — leaving a `with` block deletes the sandbox
2. **Program exit** — remaining sandboxes are deleted via an `atexit` handler
3. **Signals** — cleanup also runs on `SIGINT` (Ctrl+C) and `SIGTERM`

Disable it with `SandboxClient(auto_cleanup=False)`.

## Error handling and retries

```python
import requests

from orchard_env import SandboxClient

try:
    with SandboxClient() as client:
        with client.create_sandbox("python:3.11-slim") as sandbox:
            result = sandbox.exec("exit 1")
            if not result.succeeded:
                print(f"Command failed: {result.stderr}")
except TimeoutError as e:
    print(f"Sandbox creation timed out: {e}")
except requests.exceptions.HTTPError as e:
    print(f"API error: {e}")
```

Transient failures (connection errors, timeouts, chunked-encoding errors, HTTP
503) are retried automatically: 3 attempts with exponential backoff and jitter
(1s, 2s, 4s).

The underlying HTTP endpoints are documented in [api.md](api.md).
