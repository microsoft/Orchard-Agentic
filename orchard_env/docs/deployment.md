# Deployment guide

`orchard_env` runs on any Kubernetes cluster with Calico NetworkPolicy support.
The scripts under [`scripts/`](../scripts) automate a reference deployment on
Azure AKS; adapt them for other providers.

All commands below are run from the `orchard_env/` directory.

## Prerequisites

- A Kubernetes cluster (≥ 1.33 recommended for `image` volume sources)
- `kubectl`, `docker`, and Python 3.11+
- For the AKS reference path: an Azure subscription and the `az` CLI, logged in

## 1. Provision the cluster (AKS reference path)

```bash
chmod +x scripts/*.sh
./scripts/deploy_aks.sh
```

The script creates:

- A resource group holding everything
- A Log Analytics workspace for monitoring
- An Azure Container Registry (ACR) for the images
- An AKS cluster (`--tier standard`, Azure CNI, Calico NetworkPolicy, managed
  identity, ACR attached) with two autoscaled node pools:
  - `sys` — system components and the orchestrator, labelled `pool=system`
  - `sbx` — sandbox pods, labelled `workload=sandbox`, tainted
    `workload=sandbox:NoSchedule`, scaled from 0

It then fetches cluster credentials, patches `calico-typha` to tolerate the
sandbox taint (AKS-managed Calico does not tolerate custom taints by default, so
its pods would otherwise fail to schedule on sandbox nodes), and verifies access
with `kubectl get nodes`.

Every setting is overridable by environment variable:

```bash
export RESOURCE_GROUP="my-sandbox-rg"        # default: sandbox-orchestrator-rg
export LOCATION="westus2"
export CLUSTER_NAME="my-aks"                 # default: sandbox-aks
export ACR_NAME="mysandboxacr$(date +%s)"    # must be globally unique
export LOG_ANALYTICS_WORKSPACE="sandbox-logs"
export K8S_VERSION="1.32"

export SYSTEM_NODE_SIZE="Standard_D8as_v5"
export SYSTEM_NODE_MIN=1
export SYSTEM_NODE_MAX=2

export SANDBOX_NODE_SIZE="Standard_D8as_v5"
export SANDBOX_NODE_MIN=1
export SANDBOX_NODE_MAX=50                   # raise for large rollout fleets

./scripts/deploy_aks.sh
```

To preview the `az` commands without touching your subscription:

```bash
DRY_RUN=true ./scripts/deploy_aks.sh
```

The resulting names are printed and saved to `.azure-config` (git-ignored), which
`build_push.sh` and `deploy_k8s.sh` source automatically.

> **Non-Azure clusters.** Only this step is Azure-specific. Any Kubernetes cluster
> with Calico NetworkPolicy, a `workload=sandbox` node label, and a reachable
> container registry works — skip to step 3 and set `ACR_NAME` (or edit the image
> references in `k8s/`) yourself.

## 2. Get cluster credentials

`deploy_aks.sh` already ran `az aks get-credentials` for you. To reconnect from
another machine:

```bash
source .azure-config
az aks get-credentials \
  --resource-group "$RESOURCE_GROUP" \
  --name "$CLUSTER_NAME" \
  --overwrite-existing

kubectl get nodes
```

You should see `sys-*` nodes and, once the autoscaler reacts to demand, `sbx-*`
nodes.

## 3. Build and push the images

```bash
./scripts/build_push.sh            # all images
./scripts/build_push.sh tools      # just the agent-harness payload
```

Four images are produced (all from the `orchard_env/` build context):

| Image | Dockerfile | Purpose |
| --- | --- | --- |
| `sandbox-orchestrator` | `Dockerfile` | FastAPI control plane |
| `sandbox-python` | `Dockerfile.sandbox` | Reference sandbox image with the agent baked in |
| `sandbox-agent-injector` | `Dockerfile.agent-injector` | Init container that injects the agent into *any* user image |
| `sandbox-tools` | `Dockerfile.tools` | Read-only payload of agent harnesses |

Redis is imported from Docker Hub into your registry rather than built, so
sandbox nodes never need Docker Hub access.

If `.azure-config` is missing, set the registry manually:

```bash
export ACR_NAME=your-acr-name
./scripts/build_push.sh
```

## 4. Deploy to Kubernetes

```bash
./scripts/deploy_k8s.sh
```

This applies, in order:

1. The `orchestrator` namespace
2. ServiceAccount and RBAC (least privilege)
3. ConfigMap (with `${ACR_NAME}` substituted)
4. API-key Secret
5. The shared `sandbox-pods` namespace
6. Redis, waiting for it to become available
7. The orchestrator Deployment
8. A ClusterIP Service
9. A LoadBalancer Service (see below)
10. Optional HPA / PDB / Ingress from `k8s/optional.yaml`

```bash
kubectl get pods -n orchestrator -w
kubectl logs -n orchestrator -l app=sandbox-orchestrator -f
```

> **API keys.** Only the template `k8s/secret.example.yaml` is tracked. Before
> deploying, generate your own keys (`python k8s/gen_keys.py`), copy the template
> to `k8s/secret.yaml`, and paste them in. `k8s/secret.yaml` is gitignored, so
> real keys never land in the repo — `deploy_k8s.sh` refuses to run if the file
> is missing or still holds placeholders.

## 5. Reach the service

### LoadBalancer (default)

`deploy_k8s.sh` applies `k8s/service-loadbalancer.yaml`, then polls until the
cloud provider assigns an external IP and prints it:

```
Access the service:
  External (LoadBalancer): http://20.x.x.x
    export SANDBOX_BASE_URL=http://20.x.x.x
```

If provisioning is still in flight when the script gives up, fetch the IP later:

```bash
kubectl get svc sandbox-orchestrator-lb -n orchestrator
```

| Variable | Default | Effect |
| --- | --- | --- |
| `CREATE_LOADBALANCER` | `true` | Set to `false` to skip the LoadBalancer entirely |
| `LB_WAIT_SECONDS` | `180` | How long to wait for the external IP before moving on |

The Service carries the
`service.beta.kubernetes.io/azure-load-balancer-tcp-idle-timeout: "100"`
annotation so long-running exec calls are not cut off by the LB's idle timeout.

> The LoadBalancer exposes the orchestrator to the internet. Keep
> `REQUIRE_API_KEY=true`, and restrict access with `loadBalancerSourceRanges` or a
> private LoadBalancer annotation if the cluster is not meant to be public.

### Port-forward (development)

```bash
kubectl port-forward -n orchestrator svc/sandbox-orchestrator 8000:80
```

### Ingress (production)

Edit the Ingress section of `k8s/optional.yaml` with your hostname and TLS
certificate, then `kubectl apply -f k8s/optional.yaml`. Pair this with
`CREATE_LOADBALANCER=false` if you do not also want a raw L4 endpoint.

## 6. Smoke test

```bash
export SANDBOX_BASE_URL="http://<external-ip>"   # or http://localhost:8080 with a port-forward
export SANDBOX_API_KEY="<one-of-your-keys>"
./scripts/smoke_test.sh
```

It creates a sandbox, runs a command, polls the job, optionally applies a patch,
and deletes the sandbox. `TEST_IMAGE` overrides the image used
(default `python:3.11-slim`).

Then point the SDK at the same endpoint:

```python
from orchard_env import SandboxClient

with SandboxClient() as client:                       # reads SANDBOX_BASE_URL / SANDBOX_API_KEY
    with client.create_sandbox("python:3.11-slim") as sandbox:
        print(sandbox.exec("echo hello").stdout)
```

## Configuration

Set through `k8s/configmap.yaml` (non-secret) and `k8s/secret.yaml` (API keys).
Every field of `orchard_env/orchestrator/settings.py` can be overridden with the
matching upper-case environment variable.

| Variable | Default | Description |
| --- | --- | --- |
| `SERVICE_NAME` | `sandbox-orchestrator` | Service name used in logs and labels |
| `HOST` / `PORT` | `0.0.0.0` / `8000` | Listen address |
| `IN_CLUSTER` | `true` | `false` to use a local kubeconfig |
| `ORCHESTRATOR_NAMESPACE` | `orchestrator` | Namespace holding the control plane |
| `SANDBOX_NAMESPACE` | `sandbox-pods` | Shared namespace for all sandbox pods |
| `DEFAULT_CPU` | `4` | Default sandbox CPU request/limit |
| `DEFAULT_MEMORY` | `16Gi` | Default sandbox memory request/limit |
| `DEFAULT_WORKING_DIR` | `/workspace` | Default working directory |
| `DEFAULT_BLOCK_NETWORK` | `true` | Block sandbox egress unless overridden |
| `MAX_CONCURRENT_EXECS` | `400` | Global exec concurrency per replica |
| `MAX_CONCURRENT_CREATES` | `50` | Concurrent sandbox creations per replica |
| `DEFAULT_TIMEOUT_SECONDS` | `300` | Default command timeout |
| `AGENT_PORT` | `9090` | Port the in-pod agent listens on |
| `AGENT_INJECTOR_IMAGE` | — | Init-container image that injects the agent |
| `ENABLE_SANDBOX_TOOLS` | `true` | Mount the bundled agent harnesses |
| `SANDBOX_TOOLS_IMAGE` | — | Image holding the harness payload |
| `SANDBOX_TOOLS_VOLUME_MODE` | `image` | `image` or `initcontainer` |
| `SANDBOX_TTL_HOURS` | `2` | Auto-delete sandboxes after this long |
| `ORPHAN_JOB_TTL_HOURS` | `1` | Auto-delete orphaned jobs after this long |
| `CLEANUP_INTERVAL_SECONDS` | `900` | Reconciliation interval |
| `HEARTBEAT_TIMEOUT_SECONDS` | `600` | Sandbox considered dead without a heartbeat |
| `USE_REDIS` | `true` | Required for more than one replica |
| `REDIS_URL` | in-cluster Redis | Redis connection URL |
| `REDIS_PASSWORD` | (secret) | Required unless the password is embedded in `REDIS_URL` |
| `REDIS_REQUIRE_AUTH` | `true` | Fail closed when Redis has no credentials |
| `REQUIRE_API_KEY` | `true` | Enforce `X-API-Key` |
| `API_KEYS` | (secret) | Comma/whitespace-separated valid keys |
| `ENABLE_SERVICE_ENDPOINTS` | `false` | Allow exposing services inside sandboxes |
| `SERVICE_PUBLIC_BASE_URL` | — | Wildcard template, e.g. `https://{subdomain}.sandboxes.example.net` |
| `SERVICE_ALLOW_INSECURE_HTTP` | `false` | Development-only HTTP escape hatch |
| `SERVICE_TOKEN_SECRET` | — | Required HMAC key for service URLs |
| `SERVICE_TOKEN_TTL_SECONDS` | `3600` | Lifetime of a service URL |
| `SERVICE_RESERVED_PORTS` | — | Extra ports that may never be exposed |
| `MAX_SERVICES_PER_SANDBOX` | `8` | Ports one sandbox may expose at once |
| `SERVICE_TRAFFIC_REFRESHES_HEARTBEAT` | `true` | Proxied traffic keeps the sandbox alive |
| `SERVICE_ACTIVE_HEARTBEAT_INTERVAL_SECONDS` | `60` | Refresh interval during active streams/sockets |
| `SERVICE_PROXY_TIMEOUT_SECONDS` | `300` | Upstream request timeout |
| `SERVICE_PROXY_HANDSHAKE_TIMEOUT_SECONDS` | `15` | WebSocket handshake deadline |
| `SERVICE_PROXY_MAX_REQUEST_BYTES` | `16777216` | Max proxied request body |
| `SERVICE_PROXY_MAX_MESSAGE_BYTES` | `16777216` | Max proxied WebSocket frame |
| `LOG_LEVEL` / `LOG_FORMAT` | `INFO` / `json` | Logging |

Service endpoints are off by default because they deliberately open a hole in
the sandbox boundary. When enabling them:

- Set `SERVICE_PUBLIC_BASE_URL` to a wildcard HTTPS template for sandbox
  traffic, such as `https://{subdomain}.sandboxes.example.net`, on a separate
  registrable domain from the management API. Configure matching wildcard DNS
  and TLS. Each capability receives a different hostname, isolating browser
  localStorage, IndexedDB, caches, BroadcastChannel, and other origin state.
- Set `SERVICE_TOKEN_SECRET` to a separate random value. The feature fails
  closed when it is absent.
- Route only `/s/*` to the service origin, and redact that path in ingress,
  load-balancer, and trace logs because it contains a bearer capability.
- The agent port (`AGENT_PORT`) is always refused; add anything else sensitive
  to `SERVICE_RESERVED_PORTS`.

Apply changes with:

```bash
kubectl apply -f k8s/configmap.yaml
kubectl rollout restart deployment/sandbox-orchestrator -n orchestrator
```

### Redis

Redis shares sandbox and job state between orchestrator replicas and provides
the distributed exec locks. It is enabled by default. The reference deployment
requires `REDIS_PASSWORD` and applies an ingress NetworkPolicy that allows only
orchestrator pods; even a sandbox with `block_network=false` cannot reach it.

```bash
kubectl apply -f k8s/redis.yaml            # deploy in-cluster Redis
```

```yaml
# single replica only — configmap.yaml
USE_REDIS: "false"

# or point at a managed instance (prefer a secret REDIS_PASSWORD)
REDIS_URL: "rediss://your-redis.example.com:6380/0"
REDIS_PASSWORD: "..."
```

### Sandbox resource limits

Defaults per sandbox pod: 4 CPU cores, 16 GiB memory (requests = limits), node
selector `workload=sandbox`, working directory `/workspace`, 1 hour readiness
timeout. Change the global defaults via `DEFAULT_CPU` / `DEFAULT_MEMORY`, or set
them per sandbox:

```python
sandbox = client.create_sandbox(
    "python:3.11-slim",
    cpu="8",
    memory="32Gi",
    timeout=7200,
)
```

## Operations

### Troubleshooting

```bash
# orchestrator pod won't start
kubectl describe pod -n orchestrator -l app=sandbox-orchestrator

# sandbox pod won't schedule (autoscaler may need a few minutes)
kubectl get nodes -l workload=sandbox

# command execution failing — inspect the in-pod agent
kubectl logs -n sandbox-pods sandbox-<sandbox_id>

# network isolation
kubectl describe networkpolicy -n sandbox-pods
```

Debug helpers live in [`scripts/debug/`](../scripts/debug).

### Cleanup

```bash
# one sandbox
kubectl delete pod -n sandbox-pods sandbox-<sandbox_id>

# every sandbox
kubectl delete pods -n sandbox-pods --all

# the whole control plane (also releases the LoadBalancer's public IP)
kubectl delete namespace orchestrator

# the entire Azure resource group
az group delete --name "$RESOURCE_GROUP" --yes --no-wait
```

## Local development

```bash
pip install -e ".[dev]"

export IN_CLUSTER=false          # use your kubeconfig
export REQUIRE_API_KEY=false     # optional, for local testing
python -m orchard_env.orchestrator.main
```
