# Architecture

<p align="center">
  <img src="figures/orchard-architecture.png" alt="Orchard Env architecture" width="850">
</p>

The client SDK talks to the orchestrator over REST. The orchestrator uses the
Kubernetes API server only for pod lifecycle (create / delete); exec, file I/O,
and health checks go directly to each sandbox's Pod IP, keeping the K8s API
server and its WebSocket setup overhead off the hot path.

## System overview

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                            User application                                  │
│                  (training loop, eval harness, notebook)                     │
└───────────────────────────────────┬─────────────────────────────────────────┘
                                    │
┌───────────────────────────────────▼─────────────────────────────────────────┐
│                       Client SDK — orchard_env.client                        │
│                                                                              │
│   SandboxClient (sync)                 AsyncSandboxClient (async)            │
│   ├── create_sandbox()                 ├── create_sandbox()                  │
│   ├── get_sandbox()                    ├── get_sandbox()                     │
│   ├── delete_sandbox()                 ├── delete_sandbox()                  │
│   └── cleanup_all()                    └── cleanup_all()                     │
│                                                                              │
│   SandboxInstance / AsyncSandboxInstance                                     │
│   ├── exec()            run a command (optionally under a PTY)               │
│   ├── apply_patch()     apply a git patch                                    │
│   ├── upload_file()  /  upload_content()                                     │
│   ├── download_file() /  download_content()                                  │
│   ├── list_files()                                                           │
│   └── delete()                                                               │
└───────────────────────────────────┬─────────────────────────────────────────┘
                                    │ HTTP / REST  (+ WebSocket for PTY)
┌───────────────────────────────────▼─────────────────────────────────────────┐
│               Orchestrator — orchard_env.orchestrator (N replicas)           │
│                                                                              │
│  ┌───────────────────────────────────────────────────────────────────────┐   │
│  │                      FastAPI application (api.py)                      │   │
│  │   request-ID middleware · X-API-Key auth · structured JSON logs        │   │
│  └───────┬───────────────────────────────────────────────────────────────┘   │
│          │                                                                   │
│   ┌──────▼───────┐  ┌──────────────┐  ┌─────────────┐  ┌────────────────┐    │
│   │   Sandbox    │  │     Exec     │  │     Job     │  │   PodWatcher   │    │
│   │   Manager    │  │   Manager    │  │    Store    │  │ (K8s informer) │    │
│   │              │  │              │  │             │  │                │    │
│   │ lifecycle,   │  │ per-sandbox  │  │ in-memory   │  │ live pod phase │    │
│   │ TTL, recon-  │  │ locks, glo-  │  │ or Redis    │  │ + pod IP cache │    │
│   │ ciliation    │  │ bal semaphore│  │             │  │                │    │
│   └──────┬───────┘  └──────┬───────┘  └──────┬──────┘  └───────┬────────┘    │
│          │                 │                 │                 │             │
│          │                 │          ┌──────▼──────┐          │             │
│          │                 │          │    Redis    │  shared state across   │
│          │                 │          │   (opt-in)  │  replicas + locks      │
│          │                 │          └─────────────┘          │             │
│          │                 │                                   │             │
│   ┌──────▼─────────────────┴───────────┐  ┌────────────────────▼─────────┐   │
│   │  K8sClient (kubernetes API)        │  │  AgentClient (aiohttp)       │   │
│   │  create/delete pods,               │  │  exec, files, PTY —          │   │
│   │  network policies                  │  │  straight to the pod IP      │   │
│   └──────┬─────────────────────────────┘  └────────────────┬─────────────┘   │
└──────────┼───────────────────────────────────────────────┬─┴─────────────────┘
           │ Kubernetes API                                │ direct HTTP to pod IP
           │ (control plane only)                          │ (hot path — bypasses
           │                                               │  the API server)
┌──────────▼───────────────────────────────────────────────▼──────────────────┐
│                          Kubernetes cluster                                  │
│                                                                              │
│  namespace: orchestrator           namespace: sandbox-pods                   │
│  ┌──────────────────────┐          ┌───────────────────────────────────────┐ │
│  │ orchestrator Deploy  │          │ NetworkPolicy deny-all-egress         │ │
│  │   (N replicas)       │          │   (namespace-wide, created once)      │ │
│  │ Service (ClusterIP)  │          │                                       │ │
│  │ Service (LoadBalancer)│         │ pod: sandbox-{id}                     │ │
│  │ Redis Deploy         │          │ ┌───────────────────────────────────┐ │ │
│  └──────────────────────┘          │ │ initContainer: agent-injector     │ │ │
│      node pool: sys                │ ├───────────────────────────────────┤ │ │
│                                    │ │ container: <user image>           │ │ │
│                                    │ │   /workspace                      │ │ │
│                                    │ │   agent on :9090                  │ │ │
│                                    │ ├───────────────────────────────────┤ │ │
│                                    │ │ volume: harness payload (ro)      │ │ │
│                                    │ └───────────────────────────────────┘ │ │
│                                    │   nodeSelector workload=sandbox       │ │
│                                    └───────────────────────────────────────┘ │
│                                              node pool: sbx                  │
└──────────────────────────────────────────────────────────────────────────────┘
```

Two design decisions shape everything else:

1. **The Kubernetes API server is only a control plane.** Pod create/delete and
   NetworkPolicy management go through it; exec and file I/O do not. Those go
   straight to an agent running inside the pod, over its pod IP. The API server
   would otherwise become the bottleneck at a few hundred concurrent sandboxes.
2. **One shared namespace, not one per sandbox.** All sandbox pods live in
   `sandbox-pods` under a single namespace-wide `deny-all-egress` NetworkPolicy.
   That removes two API calls per sandbox lifecycle and avoids namespace churn.

## Components

### Client SDK — `orchard_env/client/`

| Module | Responsibility |
| --- | --- |
| `sandbox_client.py` | `SandboxClient`, `AsyncSandboxClient`, and their sandbox-instance types |
| `process.py` | `ContainerProcess` / `AsyncContainerProcess` — PTY sessions over WebSocket |

Both clients are context managers, track every sandbox they create, and retry
transient failures (connection errors, timeouts, HTTP 503) with exponential
backoff and jitter. See the [SDK reference](sdk.md).

### Orchestrator — `orchard_env/orchestrator/`

| Module | Responsibility |
| --- | --- |
| `api.py` | All FastAPI routes (defined directly, no `APIRouter`), request-ID middleware, `X-API-Key` auth, background cleanup loop |
| `sandbox_manager.py` | Sandbox lifecycle, pod-IP caching, TTL/heartbeat cleanup, cluster reconciliation |
| `exec_manager.py` | Submits exec jobs, serialises them per sandbox, bounds global concurrency, retries agent-connect failures |
| `agent_client.py` | Pooled `aiohttp` client that talks directly to pod IPs |
| `service_proxy.py` | Pooled `aiohttp` client and helpers for proxying user services inside sandboxes (port allowlisting, header filtering) |
| `service_tokens.py` | Mint and verify the signed, expiring capability tokens that authenticate service URLs |
| `k8s_client.py` | Kubernetes API wrapper — pod spec construction, network policies, throttling, retries |
| `pod_watcher.py` | Kubernetes `Watch` informer keeping live pod phase and pod IP in memory |
| `job_store.py` / `redis_job_store.py` | Job state — in-memory (single replica) or Redis (multi-replica) |
| `redis_store.py` | Shared sandbox records and distributed locks |
| `settings.py` | `pydantic-settings` singleton, configured by environment variables |
| `utils.py` | Structured JSON logging, request-ID `ContextVar`, ID generation |

### Sandbox agent — `orchard_env/agent/`

A small FastAPI server injected into every sandbox pod, listening on port `9090`
and reachable only from inside the cluster.

| Endpoint | Purpose |
| --- | --- |
| `GET /health` | Startup/readiness probe target |
| `POST /exec` | Run a command, return stdout/stderr/exit code |
| `POST /files/upload` | Write a base64 payload to a path |
| `GET /files/download` | Read a file as base64 |
| `GET /files/list` | List a directory |
| `WS /exec/pty` | Interactive PTY session (`pty_runner.py`) |

## How the agent gets into an arbitrary image

Sandboxes run **user-supplied images** that may have no Python at all. An init
container solves this:

```
initContainer: sandbox-agent-injector
    copies /opt/sandbox-agent/. ──► emptyDir volume
      ├── bin/python3            self-contained CPython 3.11
      ├── lib/                   libc, dynamic linker, stdlib
      ├── pylib/                 fastapi, uvicorn, pydantic
      ├── server.py, pty_runner.py
      └── start.sh               launches via the bundled loader

main container: <user image>
    mounts the same volume and runs start.sh in the background
```

`start.sh` invokes the bundled dynamic linker with `--library-path`, so the agent
never touches the image's own libc, Python, or `PATH`. This is also why
`agent/server.py` keeps a fallback import: as a package it resolves
`orchard_env.agent.pty_runner`, but in the injected layout the files are loose
scripts and it falls back to a bare `pty_runner` import.

The harness payload (`codex`, `claude`, `pi`, `opencode`, `hermes`) arrives the
same way, but read-only and — on Kubernetes ≥ 1.33 — through an `image:` volume
source, so the kubelet pulls it once per node instead of copying per pod.

## Flows

### Create a sandbox

```
client.create_sandbox(image, cpu, memory, block_network)
   │
   ├─► POST /sandboxes
   │      SandboxManager.create_sandbox()
   │        ├── acquire the create semaphore (MAX_CONCURRENT_CREATES)
   │        ├── if block_network=False: create a per-sandbox allow-egress policy
   │        │     (deny-all-egress is already namespace-wide)
   │        ├── K8sClient.create_pod()  → pod "sandbox-{id}" in sandbox-pods
   │        │     initContainers: agent-injector
   │        │     volumes: agent emptyDir + read-only harness payload
   │        │     nodeSelector workload=sandbox (+ toleration for the sbx taint)
   │        │     startup/readiness probes → agent :9090/health
   │        └── store the record (Redis or memory)
   │
   └─► GET /sandboxes/{id}/wait      ← server-side wait, no client polling
          PodWatcher fires an asyncio.Event the moment the pod turns Ready
```

Readiness is gated on the **agent's** `/health`, not merely on container start,
so a sandbox is never handed out before it can accept commands.

### Execute a command

```
sandbox.exec("pytest -q", timeout=600)
   │
   └─► POST /sandboxes/{id}/exec  (wait=true)
          ExecManager.submit_exec()  → job_id, status "queued"
             ├── acquire the per-sandbox lock   (serialises that sandbox)
             ├── acquire the global semaphore   (MAX_CONCURRENT_EXECS)
             ├── status → "running"
             ├── resolve the pod IP from the PodWatcher cache
             │     (K8s fallback only on a cache miss)
             ├── AgentClient.exec_command() ──HTTP──► pod:9090/exec
             │     connect failures are retried for EXEC_CONNECT_RETRY_WINDOW
             │     seconds; command timeouts are NOT retried
             └── status → "succeeded" | "failed", with stdout/stderr/exit_code
```

The server blocks until the job finishes and returns the full result. If that
server-side wait times out, the response still carries status `"running"` and the
client falls back to polling `GET /jobs/{id}`. **A `"running"` status never means
"finished"** — the single most important invariant in the exec path.

### File operations and patches

Uploads, downloads, and listings take the same route: the orchestrator resolves
the pod IP and forwards a base64 payload to the agent. `apply_patch()` writes the
diff into the sandbox and runs `git apply` through the exec path.

### Proxy to a service inside a sandbox

```
sandbox.expose_service(8000)
   │
   └─► POST /sandboxes/{id}/services
          ├── refuse the agent port and any operator-reserved port
          ├── optionally poll the service's own health path
          ├── store (port → generation) outside the sandbox JSON record
          └── mint an HMAC capability naming
              (sandbox, port, generation, expiry)
                 → https://<capability>.sandboxes.example.net/s/<token>

GET  https://<capability>.sandboxes.example.net/s/<token>/health
WS   wss://<capability>.sandboxes.example.net/s/<token>/ws
   │
   └─► verify signature → check expiry → sandbox still exists?
          → generation still active?  (revocation never revives on re-expose)
          → resolve pod IP from the PodWatcher cache
          → forward to pod:8000, pinned against cross-destination redirects
```

Two things distinguish this from the exec path. The credential lives in the URL
rather than a header, because the clients that need it — a raw WebSocket client,
a browser — cannot attach one. And the token is a *stateless* HMAC, so any
replica validates a token minted by any other without shared state, while
revocation still takes effect immediately because the active generation is
re-read on every request. Service state lives in a separate Redis hash so older
replicas can continue reading the unchanged sandbox-record schema during a
rolling upgrade.

Each capability receives a different hostname under a wildcard service domain,
so hostile browser state is isolated between services as well as from the
management API. Management credentials and cookies are stripped before
forwarding, while sandbox cookies and broad service-worker headers are stripped
on the way back.

Kubernetes note: the pod spec is unchanged. `containerPort` is informational, so
any process listening on `0.0.0.0` inside the pod is already reachable at the pod
IP. What this adds is reachability from *outside* the cluster.

### Delete a sandbox

```
sandbox.delete()  /  context-manager exit  /  atexit  /  SIGINT|SIGTERM
   │
   └─► DELETE /sandboxes/{id}
          ├── drop the record immediately (the API returns straight away)
          └── background task: delete the pod, plus the per-sandbox
              NetworkPolicy if block_network=False created one
```

## Keeping state honest

Three independent mechanisms stop sandboxes from leaking, all driven by one
background loop in `api.py` that runs every `CLEANUP_INTERVAL_SECONDS`:

| Mechanism | Trigger | Notes |
| --- | --- | --- |
| **TTL** | `age > SANDBOX_TTL_HOURS` | The backstop that always applies |
| **Pending timeout** | not ready and `age > creation_timeout + buffer` | Catches sandboxes whose image never pulled |
| **Heartbeat** | `now - last_heartbeat > HEARTBEAT_TIMEOUT_SECONDS` | Applies only to sandboxes that sent at least one heartbeat, so clients that never call `POST /heartbeat` are not deleted early |

The same loop **reconciles** against the cluster:

- Pods in Kubernetes with no record → orphans, deleted.
- Records marked *ready* whose pod is gone → tracking removed.
- Records still *pending* are left alone; they may still be starting.
- If the pod `LIST` call fails, reconciliation is skipped entirely rather than
  risk mass-deleting records because of a transient API error.

## Running multiple replicas

```
                  Service (LoadBalancer / ClusterIP)
                                │
            ┌───────────────────┼───────────────────┐
            ▼                   ▼                   ▼
      orchestrator-0      orchestrator-1      orchestrator-2
            │                   │                   │
            └───────────────────┼───────────────────┘
                                ▼
                      Redis (state + locks)
```

With `USE_REDIS=true` every replica shares sandbox records and job state, and
per-sandbox execution locks become distributed locks — so a sandbox stays
serialised even when successive requests land on different replicas. Each replica
runs its own `PodWatcher`, so pod-IP lookups stay local and free.

With `USE_REDIS=false` the orchestrator keeps everything in memory and **must**
run as a single replica.

The reference Redis deployment requires authentication and applies an ingress
NetworkPolicy that permits only `app=sandbox-orchestrator` pods. This is a
control-plane boundary: a sandbox with egress enabled must not be able to
restore revoked capability generations or alter lifecycle state.

## Throughput notes

- Exec and file I/O never touch the Kubernetes API server.
- Pod IPs come from an in-memory informer cache; the API-server fallback fires
  only on a cache miss.
- K8s API calls are bounded by semaphores (`K8S_API_CONCURRENCY`,
  `MAX_CONCURRENT_CREATES`) and retried with backoff.
- Server-side waits (`/sandboxes/{id}/wait`, `/jobs/{id}/wait`) are event-driven,
  so idle clients cost nothing.
- HTTP keep-alive is held at 120s so client connection pools do not race a
  server-initiated FIN.

## See also

- [SDK reference](sdk.md) — the Python client surface
- [HTTP API reference](api.md) — every endpoint
- [Deployment guide](deployment.md) — cluster setup, configuration, operations
