#!/bin/bash
set -e

CLUSTER_NAME="llm-server"

# ── Colors ──────────────────────────────────────────────────────────────────
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

log()    { echo -e "${BLUE}[stop]${NC} $1"; }
success(){ echo -e "${GREEN}[done]${NC} $1"; }
warn()   { echo -e "${YELLOW}[warn]${NC} $1"; }
error()  { echo -e "${RED}[error]${NC} $1"; exit 1; }

# ── Usage ────────────────────────────────────────────────────────────────────
# ./stop-k8s.sh           → delete deployments and services, keep PVCs and cluster
# ./stop-k8s.sh --volumes → also delete PVCs (model files will be re-downloaded)
# ./stop-k8s.sh --cluster → delete the entire kind cluster

DELETE_VOLUMES=false
DELETE_CLUSTER=false

for arg in "$@"; do
  case $arg in
    --volumes) DELETE_VOLUMES=true ;;
    --cluster) DELETE_CLUSTER=true ;;
    *) error "Unknown argument: $arg. Use --volumes or --cluster" ;;
  esac
done

# ── Preflight ────────────────────────────────────────────────────────────────
command -v kind    &>/dev/null || error "kind not found"
command -v kubectl &>/dev/null || error "kubectl not found"

# ── Delete cluster entirely ───────────────────────────────────────────────────
if [ "$DELETE_CLUSTER" = true ]; then
  warn "Deleting entire kind cluster '${CLUSTER_NAME}'..."
  warn "This will destroy all data including PVCs and model files."
  read -p "Are you sure? (y/N) " confirm
  [[ "$confirm" =~ ^[Yy]$ ]] || { log "Aborted."; exit 0; }

  kind delete cluster --name "${CLUSTER_NAME}" \
    || error "Failed to delete cluster"
  success "Cluster '${CLUSTER_NAME}' deleted"
  exit 0
fi

# ── Switch context ────────────────────────────────────────────────────────────
kubectl config use-context "kind-${CLUSTER_NAME}" &>/dev/null \
  || error "Could not switch to context kind-${CLUSTER_NAME}. Is the cluster running?"

# ── Delete manifests ──────────────────────────────────────────────────────────
MANIFESTS=(
  infra/k8s/dashboard.yaml
  infra/k8s/worker.yaml
  infra/k8s/api.yaml
  infra/k8s/llm-360m.yaml
  infra/k8s/llm-135m.yaml
  infra/k8s/redis.yaml
)

log "Deleting deployments and services..."

for manifest in "${MANIFESTS[@]}"; do
  if [ -f "${manifest}" ]; then
    log "  Deleting ${manifest}..."
    kubectl delete -f "${manifest}" --ignore-not-found \
      || error "Failed to delete ${manifest}"
    success "  ${manifest} deleted"
  else
    warn "  ${manifest} not found, skipping"
  fi
done

# ── Optionally delete PVCs ────────────────────────────────────────────────────
if [ "$DELETE_VOLUMES" = true ]; then
  warn "Deleting PVCs (model files will need to be re-downloaded on next deploy)..."
  kubectl delete pvc --all --ignore-not-found
  success "PVCs deleted"
else
  log "PVCs preserved (model files kept). Use --volumes to delete them."
fi

# ── Final status ──────────────────────────────────────────────────────────────
echo ""
log "Remaining resources:"
kubectl get pods,pvc,services 2>/dev/null || true

echo ""
success "Done."