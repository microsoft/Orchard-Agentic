#!/bin/bash
set -euo pipefail

###############################################################################
# Deploy Orchestrator to Kubernetes
###############################################################################

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"

# Load Azure configuration if available
if [ -f "$PROJECT_ROOT/.azure-config" ]; then
    source "$PROJECT_ROOT/.azure-config"
    echo "Loaded configuration from .azure-config"
fi

# Check required variables
if [ -z "${ACR_NAME:-}" ]; then
    echo "Warning: ACR_NAME is not set. Using placeholder."
    echo "You may need to manually update the deployment image."
    ACR_NAME="REPLACE_WITH_YOUR_ACR"
fi

echo "============================================"
echo "Deploy Orchestrator to Kubernetes"
echo "============================================"
echo "ACR: $ACR_NAME"
echo "============================================"
echo ""

# Change to project root
cd "$PROJECT_ROOT"

# Check kubectl is available
if ! command -v kubectl &> /dev/null; then
    echo "Error: kubectl is not installed"
    exit 1
fi

# Check cluster connection
echo "Checking cluster connection..."
if ! kubectl cluster-info &> /dev/null; then
    echo "Error: Cannot connect to Kubernetes cluster"
    echo "Run: az aks get-credentials --resource-group <RG> --name <CLUSTER>"
    exit 1
fi

echo "✓ Connected to cluster"
echo ""

# Process templates with ACR name
echo "Processing templates..."
TEMP_DEPLOYMENT=$(mktemp)
TEMP_CONFIGMAP=$(mktemp)
TEMP_REDIS=$(mktemp)
sed "s/\${ACR_NAME}/$ACR_NAME/g" k8s/deployment.yaml > "$TEMP_DEPLOYMENT"
sed "s/\${ACR_NAME}/$ACR_NAME/g" k8s/configmap.yaml > "$TEMP_CONFIGMAP"
sed "s/\${ACR_NAME}/$ACR_NAME/g" k8s/redis.yaml > "$TEMP_REDIS"
echo "✓ Templates processed"
echo ""

# Apply Kubernetes manifests in order
echo "Applying Kubernetes manifests..."

echo "1. Creating namespace..."
kubectl apply -f k8s/namespace.yaml

echo "2. Creating service account..."
kubectl apply -f k8s/serviceaccount.yaml

echo "3. Creating RBAC rules..."
kubectl apply -f k8s/rbac.yaml

echo "4. Creating config map..."
kubectl apply -f "$TEMP_CONFIGMAP"

echo "5. Creating API keys secret..."
if [ ! -f k8s/secret.yaml ]; then
    echo "Error: k8s/secret.yaml not found." >&2
    echo "  Generate keys and create it from the template:" >&2
    echo "    python k8s/gen_keys.py" >&2
    echo "    cp k8s/secret.example.yaml k8s/secret.yaml" >&2
    echo "  then paste the generated keys into k8s/secret.yaml." >&2
    exit 1
fi
if grep -q REPLACE_WITH_GENERATED_KEY k8s/secret.yaml; then
    echo "Error: k8s/secret.yaml still contains placeholder API keys." >&2
    echo "  Run 'python k8s/gen_keys.py' and paste real keys before deploying." >&2
    exit 1
fi
if grep -q 'ENABLE_SERVICE_ENDPOINTS: "true"' "$TEMP_CONFIGMAP" &&
   grep -q REPLACE_WITH_A_RANDOM_32_BYTE_SECRET k8s/secret.yaml; then
    echo "Error: service endpoints are enabled but SERVICE_TOKEN_SECRET is unset." >&2
    echo "  Replace its placeholder in k8s/secret.yaml with a random value." >&2
    exit 1
fi
if grep -q REPLACE_WITH_A_DIFFERENT_RANDOM_32_BYTE_SECRET k8s/secret.yaml; then
    echo "Error: k8s/secret.yaml still contains the Redis password placeholder." >&2
    echo "  Replace REDIS_PASSWORD with a separate random value." >&2
    exit 1
fi
kubectl apply -f k8s/secret.yaml

echo "6. Creating shared sandbox namespace..."
kubectl create namespace sandbox-pods --dry-run=client -o yaml | kubectl apply -f -
kubectl label namespace sandbox-pods app=sandbox managed-by=orchestrator --overwrite

echo "7. Deploying Redis for state sharing..."
kubectl apply -f "$TEMP_REDIS"
echo "   Waiting for Redis to be ready..."
kubectl wait --for=condition=available --timeout=60s deployment/redis -n orchestrator || {
    echo "Warning: Redis did not become ready in time"
}

echo "8. Creating deployment..."
kubectl apply -f "$TEMP_DEPLOYMENT"

echo "9. Creating service (ClusterIP)..."
kubectl apply -f k8s/service.yaml

# LoadBalancer service — gives the orchestrator an external IP so clients
# outside the cluster can reach it without a port-forward.
# Set CREATE_LOADBALANCER=false to skip (e.g. when fronting with an Ingress).
CREATE_LOADBALANCER="${CREATE_LOADBALANCER:-true}"
LB_WAIT_SECONDS="${LB_WAIT_SECONDS:-180}"
LB_IP=""
if [ "$CREATE_LOADBALANCER" = "true" ]; then
    echo "10. Creating LoadBalancer service..."
    kubectl apply -f k8s/service-loadbalancer.yaml

    echo "    Waiting up to ${LB_WAIT_SECONDS}s for an external IP..."
    deadline=$(( SECONDS + LB_WAIT_SECONDS ))
    while [ "$SECONDS" -lt "$deadline" ]; do
        LB_IP=$(kubectl get svc sandbox-orchestrator-lb -n orchestrator \
            -o jsonpath='{.status.loadBalancer.ingress[0].ip}' 2>/dev/null || true)
        if [ -n "$LB_IP" ]; then
            break
        fi
        sleep 5
    done

    if [ -n "$LB_IP" ]; then
        echo "    ✓ External IP: $LB_IP"
    else
        echo "    Warning: no external IP yet (provisioning can take a few minutes)."
        echo "    Check later with: kubectl get svc sandbox-orchestrator-lb -n orchestrator"
    fi
else
    echo "10. Skipping LoadBalancer service (CREATE_LOADBALANCER=false)"
fi

# Optional resources
if [ -f "k8s/optional.yaml" ]; then
    echo "11. Creating optional resources (HPA, PDB)..."
    kubectl apply -f k8s/optional.yaml || echo "Note: Some optional resources may have failed (this is OK)"
fi

# Clean up temp files
rm -f "$TEMP_DEPLOYMENT" "$TEMP_CONFIGMAP" "$TEMP_REDIS"

echo ""
echo "✓ All manifests applied"
echo ""

# Wait for deployment to be ready
echo "Waiting for deployment to be ready..."
kubectl wait --for=condition=available --timeout=300s \
    deployment/sandbox-orchestrator -n orchestrator || {
    echo "Warning: Deployment did not become ready in time"
    echo "Check status with: kubectl get pods -n orchestrator"
}

echo ""
echo "============================================"
echo "Deployment Complete!"
echo "============================================"
echo ""
echo "Check status:"
echo "  kubectl get pods -n orchestrator"
echo "  kubectl logs -n orchestrator -l app=sandbox-orchestrator"
echo ""
echo "Access the service:"
if [ -n "$LB_IP" ]; then
    echo "  External (LoadBalancer): http://$LB_IP"
    echo "    export SANDBOX_BASE_URL=http://$LB_IP"
    echo ""
fi
echo "  Port-forward: kubectl port-forward -n orchestrator svc/sandbox-orchestrator 8000:80"
echo "  Then visit: http://localhost:8000"
echo ""
echo "Run smoke tests:"
echo "  ./scripts/smoke_test.sh"
echo "============================================"
