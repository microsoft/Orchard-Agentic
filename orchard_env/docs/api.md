# HTTP API reference

All endpoints except `GET /` and `GET /health` require an API key in the
`X-API-Key` header (unless the orchestrator runs with `REQUIRE_API_KEY=false`).

Base URL examples below assume a local port-forward:

```bash
kubectl port-forward -n orchestrator svc/sandbox-orchestrator 8000:80
export BASE_URL=http://localhost:8000
```

## Status codes

| Code | Meaning |
| --- | --- |
| `401` / `403` | Missing or invalid API key |
| `404` | Sandbox or job not found |
| `408` | Server-side wait timed out |
| `503` | Transient overload — safe to retry |

## Endpoints

| Method | Path | Description |
| --- | --- | --- |
| `GET` | `/` | Service banner |
| `GET` | `/health` | Health check (no auth) |
| `GET` | `/resources` | Cluster resource summary |
| `POST` | `/sandboxes` | Create a sandbox |
| `GET` | `/sandboxes/{sandbox_id}` | Get sandbox info |
| `GET` | `/sandboxes/{sandbox_id}/wait` | Block until the sandbox is ready |
| `DELETE` | `/sandboxes/{sandbox_id}` | Delete a sandbox |
| `POST` | `/sandboxes/{sandbox_id}/heartbeat` | Refresh the sandbox liveness timer |
| `POST` | `/sandboxes/{sandbox_id}/exec` | Run a command |
| `WS` | `/sandboxes/{sandbox_id}/exec/pty` | Interactive PTY session |
| `POST` | `/sandboxes/{sandbox_id}/apply_patch` | Apply a git patch |
| `POST` | `/sandboxes/{sandbox_id}/files` | Upload a file |
| `GET` | `/sandboxes/{sandbox_id}/files` | Download a file |
| `GET` | `/sandboxes/{sandbox_id}/files/list` | List a directory |
| `POST` | `/sandboxes/{sandbox_id}/services` | Expose a service port |
| `GET` | `/sandboxes/{sandbox_id}/services` | List exposed ports |
| `DELETE` | `/sandboxes/{sandbox_id}/services/{port}` | Revoke an exposed port |
| `ANY` | `/s/{token}/{path}` | Proxy HTTP to a sandbox service |
| `WS` | `/s/{token}/{path}` | Proxy a WebSocket to a sandbox service |
| `GET` | `/jobs/{job_id}` | Get job status and results |
| `GET` | `/jobs/{job_id}/wait` | Block until the job completes |

### Create a sandbox

```http
POST /sandboxes
Content-Type: application/json
X-API-Key: your-api-key

{
  "image": "python:3.11-slim",
  "block_network": true,
  "sandbox_id": "optional-custom-id",
  "cpu": "4",
  "memory": "16Gi",
  "timeout": 3600
}
```

```json
{
  "sandbox_id": "abc12345",
  "namespace": "sandbox-pods",
  "image": "python:3.11-slim",
  "block_network": true,
  "cpu": "4",
  "memory": "16Gi",
  "timeout": 3600,
  "status": "pending"
}
```

Wait for readiness without polling:

```http
GET /sandboxes/abc12345/wait?timeout=3600
```

### Execute a command

```http
POST /sandboxes/{sandbox_id}/exec
Content-Type: application/json
X-API-Key: your-api-key

{
  "command": "echo Hello",
  "timeout_seconds": 300,
  "cwd": "/workspace",
  "env": {"KEY": "value"},
  "login_shell": false,
  "wait": false
}
```

With `wait: false` (default) the call returns immediately:

```json
{ "job_id": "550e8400-e29b-41d4-a716-446655440000", "status": "queued" }
```

With `wait: true` the orchestrator blocks until the job finishes and returns the
full job object. **If the response status is `"running"`, the server-side wait
timed out and the job is still executing** — keep polling `GET /jobs/{job_id}`
or call `GET /jobs/{job_id}/wait`.

### Query a job

```http
GET /jobs/{job_id}
X-API-Key: your-api-key
```

```json
{
  "job_id": "550e8400-e29b-41d4-a716-446655440000",
  "sandbox_id": "abc12345",
  "command": "echo Hello",
  "status": "succeeded",
  "stdout": "Hello\n",
  "stderr": "",
  "exit_code": 0,
  "created_at": 1702900000.0,
  "started_at": 1702900001.0,
  "completed_at": 1702900002.0,
  "error": null
}
```

Statuses: `queued` → `running` → `succeeded` | `failed`.

Server-side wait (no client polling):

```http
GET /jobs/{job_id}/wait?timeout=300
```

### Apply a git patch

```http
POST /sandboxes/{sandbox_id}/apply_patch
Content-Type: application/json
X-API-Key: your-api-key

{
  "patch": "--- a/file.txt\n+++ b/file.txt\n@@ -1 +1,2 @@\n line1\n+line2",
  "timeout_seconds": 30
}
```

```json
{ "success": true, "stdout": "", "stderr": "", "exit_code": 0 }
```

### File operations

Upload (content is base64-encoded):

```http
POST /sandboxes/{sandbox_id}/files
Content-Type: application/json
X-API-Key: your-api-key

{ "path": "/workspace/test.py", "content": "<base64>" }
```

```json
{ "success": true, "path": "/workspace/test.py", "size": 1024 }
```

Download:

```http
GET /sandboxes/{sandbox_id}/files?path=/workspace/test.py
X-API-Key: your-api-key
```

```json
{ "path": "/workspace/test.py", "content": "<base64>", "size": 1024 }
```

List:

```http
GET /sandboxes/{sandbox_id}/files/list?path=/workspace
X-API-Key: your-api-key
```

```json
{
  "path": "/workspace",
  "files": [
    { "name": "test.py", "type": "file", "size": 1024, "modified": "2026-07-29T10:00:00" },
    { "name": "src", "type": "directory", "size": 4096, "modified": "2026-07-29T10:00:00" }
  ]
}
```

`size` is an integer (bytes). `type` is `"file"` or `"directory"`.

### Service endpoints

Exec and file I/O cover agents that *run commands*. Some workloads instead
speak a protocol to a long-running server inside the sandbox — an OpenEnv
environment server, an MCP server, a dev server, an evaluation endpoint. Those
need a reachable URL.

Disabled by default. Set `ENABLE_SERVICE_ENDPOINTS=true` on the orchestrator to
turn them on.

#### Expose a port

```http
POST /sandboxes/{sandbox_id}/services
Content-Type: application/json
X-API-Key: your-api-key

{
  "port": 8000,
  "ttl_seconds": 3600,
  "wait_ready": true,
  "health_path": "/health",
  "ready_timeout": 60
}
```

```json
{
  "sandbox_id": "abc12345",
  "port": 8000,
  "url": "https://7f0a...c3.sandboxes.example.net/s/eyJ...signed-token",
  "expires_at": 1702903600.0
}
```

`wait_ready` blocks until the service answers `health_path`. This is worth
setting: a sandbox reports ready when the *in-pod agent* is up, which can be
well before a server you just launched has finished binding.

Re-exposing an active port is idempotent. After revocation, exposing the same
port creates a new generation, so old unexpired URLs stay invalid.

**The returned URL is a bearer credential.** Anyone holding it can reach that
port on that sandbox until it expires. Keep it out of logs, scope it with
`ttl_seconds`, and revoke it when the work is done.

#### Use the endpoint

The token travels in the path, so no header is required — which is the point:
clients that cannot set headers (a raw WebSocket client, a browser) still work.
Appending a path also just works, so an OpenEnv client that adds `/ws` produces
a valid URL with no special-casing.

```bash
curl "$URL/health"                       # HTTP
websocat "${URL/https:/wss:}/ws"         # WebSocket
```

Every request re-checks the signature, the expiry, that the sandbox still
exists, and that the port is still exposed.

#### List and revoke

```http
GET /sandboxes/{sandbox_id}/services
X-API-Key: your-api-key
```

```json
{ "sandbox_id": "abc12345", "ports": [8000] }
```

```http
DELETE /sandboxes/{sandbox_id}/services/8000
X-API-Key: your-api-key
```

```json
{ "status": "revoked", "sandbox_id": "abc12345", "port": 8000 }
```

Revocation is immediate and permanent for that URL: the active generation is
consulted on every request, and re-exposure creates a different generation.

#### Security notes

- The in-pod agent port (`AGENT_PORT`, default `9090`) can never be exposed —
  it would hand out unauthenticated exec and file access inside the sandbox.
  `SERVICE_RESERVED_PORTS` adds more ports to that list.
- `SERVICE_PUBLIC_BASE_URL` is required and must contain one `{subdomain}`
  placeholder, for example
  `https://{subdomain}.sandboxes.example.net`. Configure wildcard DNS and TLS
  on a separate registrable domain from the management API. Every capability
  receives a different hostname, isolating browser origin state. The
  management API is refused on all matching service hostnames, and capability
  routes are refused elsewhere.
- `SERVICE_TOKEN_SECRET` is required. Use a separate random secret, not a
  management API key.
- Capability tokens appear in request paths. Orchard disables its own Uvicorn
  access log, but ingress, load-balancer, and tracing systems must also redact
  `/s/*`. Avoid putting service URLs in referrers or durable logs.
- Management credentials (`X-API-Key`, `Authorization`, and cookies) are never
  forwarded into sandbox code. Sandbox `Set-Cookie` and
  `Service-Worker-Allowed` response headers are stripped.
- Proxied traffic refreshes the sandbox's liveness timer by default
  for the full lifetime of an HTTP stream or WebSocket, so an active service
  is not reaped mid-session.

| Status | Meaning |
| --- | --- |
| `400` | Port is out of range or reserved |
| `403` | Token is invalid, expired, or the port is no longer exposed |
| `404` | Sandbox not found, or service endpoints are disabled |
| `408` | `wait_ready` timed out |
| `409` | Sandbox already exposes `MAX_SERVICES_PER_SANDBOX` ports |
| `413` | Request body exceeds `SERVICE_PROXY_MAX_REQUEST_BYTES` |
| `502` | Service inside the sandbox is unreachable |
| `504` | Service did not respond in time |

### Delete a sandbox

```http
DELETE /sandboxes/{sandbox_id}
X-API-Key: your-api-key
```

```json
{ "status": "deleted", "sandbox_id": "abc12345" }
```
