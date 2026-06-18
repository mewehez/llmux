#!/bin/bash
set -e

CLUSTER_NAME="llm-server"
NS="llm-server"
RELEASE="llm-server"

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; BLUE='\033[0;34m'; NC='\033[0m'
log()    { echo -e "${BLUE}[stop]${NC} $1"; }
success(){ echo -e "${GREEN}[done]${NC} $1"; }
warn()   { echo -e "${YELLOW}[warn]${NC} $1"; }
error()  { echo -e "${RED}[error]${NC} $1"; exit 1; }

# ./stop-k8s.sh           → helm uninstall, keep model PVCs and cluster
# ./stop-k8s.sh --volumes → also delete PVCs (GGUFs re-downloaded next deploy)
# ./stop-k8s.sh --cluster → delete the entire kind cluster
DELETE_VOLUMES=false; DELETE_CLUSTER=false
for arg in "$@"; do
  case $arg in
    --volumes) DELETE_VOLUMES=true ;;
    --cluster) DELETE_CLUSTER=true ;;
    *) error "Unknown argument: $arg. Use --volumes or --cluster" ;;
  esac
done

command -v kind    &>/dev/null || error "kind not found"
command -v kubectl &>/dev/null || error "kubectl not found"
command -v helm    &>/dev/null || error "helm not found"

if [ "$DELETE_CLUSTER" = true ]; then
  warn "Deleting entire kind cluster '${CLUSTER_NAME}' (all data, including model PVCs)."
  read -p "Are you sure? (y/N) " confirm
  [[ "$confirm" =~ ^[Yy]$ ]] || { log "Aborted."; exit 0; }
  kind delete cluster --name "${CLUSTER_NAME}" || error "Failed to delete cluster"
  success "Cluster deleted"; exit 0
fi

kubectl config use-context "kind-${CLUSTER_NAME}" &>/dev/null \
  || error "Could not switch to context kind-${CLUSTER_NAME}. Is the cluster running?"

# ── Helm uninstall ────────────────────────────────────────────────────────────
if helm status "${RELEASE}" -n "${NS}" &>/dev/null; then
  log "helm uninstall ${RELEASE}..."
  helm uninstall "${RELEASE}" -n "${NS}" || warn "helm uninstall reported an issue"
  success "Helm release removed (model PVCs kept — resource-policy: keep)"
else
  log "No Helm release '${RELEASE}' found."
fi

# ── Optionally delete PVCs ────────────────────────────────────────────────────
if [ "$DELETE_VOLUMES" = true ]; then
  warn "Deleting PVCs (GGUF model files will be re-downloaded on next deploy)..."
  kubectl delete pvc -l app.kubernetes.io/part-of=llm-server -n "${NS}" --ignore-not-found
  kubectl delete pvc --all -n "${NS}" --ignore-not-found
  success "PVCs deleted"
else
  log "PVCs preserved (model files kept). Use --volumes to delete them."
fi

echo ""
log "Remaining resources:"
kubectl get pods,pvc,svc -n "${NS}" 2>/dev/null || true
echo ""
success "Done."
