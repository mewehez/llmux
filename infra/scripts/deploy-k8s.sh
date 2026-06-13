#!/bin/bash
set -e

CLUSTER_NAME="llm-server"
VITE_API_URL="http://localhost:30000"

# ── Colors ─────────────────────────────────────────────────────────────────
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

log()    { echo -e "${BLUE}[deploy]${NC} $1"; }
success(){ echo -e "${GREEN}[done]${NC} $1"; }
warn()   { echo -e "${YELLOW}[warn]${NC} $1"; }
error()  { echo -e "${RED}[error]${NC} $1"; exit 1; }

# ── Preflight checks ────────────────────────────────────────────────────────
log "Checking prerequisites..."

command -v docker  &>/dev/null || error "docker not found"
command -v kind    &>/dev/null || error "kind not found"
command -v kubectl &>/dev/null || error "kubectl not found"

# Check cluster exists
kind get clusters | grep -q "^${CLUSTER_NAME}$" \
  || error "kind cluster '${CLUSTER_NAME}' not found. Run: kind create cluster --name ${CLUSTER_NAME} --config infra/k8s/kind-config.yaml"

# Set kubectl context
kubectl config use-context "kind-${CLUSTER_NAME}" &>/dev/null \
  || error "Could not switch to context kind-${CLUSTER_NAME}"

success "Prerequisites OK"

# ── Build images ────────────────────────────────────────────────────────────
log "Building api..."
docker build -t llm-server-api:k8s ./api \
  || error "Failed to build api"
success "api built"

log "Building worker..."
docker build -t llm-server-worker:k8s ./worker \
  || error "Failed to build worker"
success "worker built"

log "Building dashboard (production)..."
docker build \
  --target production \
  --build-arg VITE_API_URL=${VITE_API_URL} \
  -t llm-server-dashboard:k8s \
  ./dashboard \
  || error "Failed to build dashboard"
success "dashboard built"

# ── Load images into kind ───────────────────────────────────────────────────
log "Loading images into kind cluster '${CLUSTER_NAME}'..."

for image in llm-server-api:k8s llm-server-worker:k8s llm-server-dashboard:k8s; do
  log "  Loading ${image}..."
  kind load docker-image "${image}" --name "${CLUSTER_NAME}" \
    || error "Failed to load ${image}"
  success "  ${image} loaded"
done

# ── Apply manifests ─────────────────────────────────────────────────────────
log "Applying Kubernetes manifests..."

MANIFESTS=(
  infra/k8s/redis.yaml
  infra/k8s/llm-135m.yaml
  infra/k8s/llm-360m.yaml
  infra/k8s/api.yaml
  infra/k8s/worker.yaml
  infra/k8s/dashboard.yaml
)

for manifest in "${MANIFESTS[@]}"; do
  log "  Applying ${manifest}..."
  kubectl apply -f "${manifest}" \
    || error "Failed to apply ${manifest}"
  success "  ${manifest} applied"
done

# ── Restart deployments to pick up new images ───────────────────────────────
log "Restarting deployments..."

for deployment in api worker-135m worker-360m dashboard; do
  if kubectl get deployment/${deployment} &>/dev/null; then
    kubectl rollout restart deployment/${deployment} &>/dev/null
    log "  Restarted ${deployment}"
  else
    log "  Skipping ${deployment} (first deploy, no restart needed)"
  fi
done

# ── Wait for rollout ────────────────────────────────────────────────────────
log "Waiting for rollouts to complete..."

for deployment in api worker-135m worker-360m dashboard; do
  log "  Waiting for ${deployment}..."
  kubectl rollout status deployment/${deployment} --timeout=120s \
    || error "Rollout failed for ${deployment}"
  success "  ${deployment} ready"
done

# ── Final status ────────────────────────────────────────────────────────────
echo ""
log "Cluster state:"
kubectl get pods

echo ""
success "Deployment complete."
echo ""
echo -e "  API:       ${GREEN}http://localhost:30000${NC}"
echo -e "  Dashboard: ${GREEN}http://localhost:30001${NC}"
echo -e "  LLM 135m:  ${GREEN}http://localhost:30002${NC}"
echo -e "  LLM 360m:  ${GREEN}http://localhost:30003${NC}"
