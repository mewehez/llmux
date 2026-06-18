#!/bin/bash
set -e

CLUSTER_NAME="llm-server"
NS="llm-server"
RELEASE="llm-server"
CHART="infra/helm/llm-server"
KEDA_VERSION="2.13.0"
# Dashboard talks to the API through the reverse proxy (same origin), under /api.
VITE_API_URL="http://localhost:30080/api"

# ── Colors ─────────────────────────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; BLUE='\033[0;34m'; NC='\033[0m'
log()    { echo -e "${BLUE}[deploy]${NC} $1"; }
success(){ echo -e "${GREEN}[done]${NC} $1"; }
warn()   { echo -e "${YELLOW}[warn]${NC} $1"; }
error()  { echo -e "${RED}[error]${NC} $1"; exit 1; }

# ── Preflight ────────────────────────────────────────────────────────────────
log "Checking prerequisites..."
command -v docker  &>/dev/null || error "docker not found"
command -v kind    &>/dev/null || error "kind not found"
command -v kubectl &>/dev/null || error "kubectl not found"
command -v helm    &>/dev/null || error "helm not found. Install: https://helm.sh/docs/intro/install/"

kind get clusters | grep -q "^${CLUSTER_NAME}$" \
  || error "kind cluster '${CLUSTER_NAME}' not found. Run: kind create cluster --name ${CLUSTER_NAME} --config infra/k8s/kind-config.yaml"
kubectl config use-context "kind-${CLUSTER_NAME}" &>/dev/null \
  || error "Could not switch to context kind-${CLUSTER_NAME}"
success "Prerequisites OK"

warn "Entrypoint is the reverse proxy on host port 30080 (see kind-config.yaml)."

# ── Build images ────────────────────────────────────────────────────────────
log "Building images..."
docker build -t llm-server-api:k8s ./api          || error "Failed to build api"
docker build -t llm-server-worker:k8s ./worker    || error "Failed to build worker"
docker build --target production --build-arg VITE_API_URL=${VITE_API_URL} \
  -t llm-server-dashboard:k8s ./dashboard         || error "Failed to build dashboard"
success "Images built"

log "Loading images into kind cluster '${CLUSTER_NAME}'..."
for image in llm-server-api:k8s llm-server-worker:k8s llm-server-dashboard:k8s; do
  kind load docker-image "${image}" --name "${CLUSTER_NAME}" || error "Failed to load ${image}"
done
success "Images loaded"

# ── Sync the registry into the chart (single source of truth) ────────────────
# The Helm chart reads config/models.json via .Files, which must live inside the
# chart dir — so we copy it here before every render.
log "Syncing config/models.json into the chart..."
cp config/models.json "${CHART}/files/models.json"

# ── Install KEDA (autoscaling) ───────────────────────────────────────────────
if kubectl get crd scaledobjects.keda.sh &>/dev/null; then
  log "KEDA already installed"
else
  log "Installing KEDA ${KEDA_VERSION}..."
  kubectl apply --server-side -f \
    "https://github.com/kedacore/keda/releases/download/v${KEDA_VERSION}/keda-${KEDA_VERSION}.yaml" \
    || error "Failed to install KEDA"
  kubectl wait --for=condition=available --timeout=120s deployment/keda-operator -n keda \
    || warn "KEDA operator not ready yet; ScaledObjects will reconcile once it is"
  success "KEDA installed"
fi

# ── Deploy via Helm ───────────────────────────────────────────────────────────
# rollme=<timestamp> rolls the app pods so they pick up freshly-rebuilt :k8s
# images (whose tag doesn't change). helm upgrade prunes anything no longer
# rendered (e.g. a model you set enabled:false).
log "helm upgrade --install ${RELEASE}..."
helm upgrade --install "${RELEASE}" "${CHART}" \
  --namespace "${NS}" --create-namespace \
  --set rollme="$(date +%s)" \
  || error "helm upgrade failed"
success "Helm release applied"

# ── Status ────────────────────────────────────────────────────────────────────
echo ""
log "Cluster state:"
kubectl get pods -n "${NS}"
echo ""
success "Deployment complete."
echo ""
echo -e "  Dashboard: ${GREEN}http://localhost:30080${NC}        (single entrypoint via reverse proxy)"
echo -e "  API:       ${GREEN}http://localhost:30080/api${NC}    (proxied; not exposed directly)"
echo ""
echo -e "  ${YELLOW}Add/disable a model in config/models.json, then re-run this script.${NC}"
