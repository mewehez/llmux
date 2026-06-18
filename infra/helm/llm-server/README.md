# llm-server Helm chart

Generates all Kubernetes resources from the **`config/models.json`** registry.
Per **enabled** model it renders: a model PVC, the llama.cpp Deployment (with an
init container that downloads the GGUF from `model_url`) + slots sidecar +
Service, a worker Deployment, and (when `keda.enabled`) two KEDA `ScaledObject`s.
Shared resources (redis, api, dashboard, proxy, benchmark-runner, the registry
ConfigMap, the secret) are rendered once.

## Add / choose models
Everything is the registry — edit `config/models.json`, then redeploy:

```bash
./infra/scripts/deploy-k8s.sh        # build images → load into kind → helm upgrade
```

- **Add a model** → add an entry (`id`, `label`, `model_file`, `model_url`,
  `llm_args`, scaling maxes, `resources`). `stream`/`consumer_group`/`llm_url`
  are derived from `id`. The GGUF is pulled into the PVC on first boot.
- **Stop serving a model** → set `"enabled": false`. `helm upgrade` **prunes**
  its resources and the API drops it from `/config` + scaling. Model PVCs are
  annotated `helm.sh/resource-policy: keep`, so the downloaded weights survive.

## How the registry reaches the chart
`config/models.json` is the single source. `deploy-k8s.sh` copies it to
`files/models.json` (Helm `.Files` can only read inside the chart), and the
template reads it with `.Files.Get "files/models.json" | fromJson`. The same
file is also shipped as the `llm-models` ConfigMap that the API/workers mount.

## Useful
```bash
helm template llm-server infra/helm/llm-server -n llm-server        # render to stdout
helm template ... | kubectl apply --dry-run=server -f -             # validate vs the cluster
helm uninstall llm-server -n llm-server                             # remove (keeps PVCs)
```

Tunables live in `values.yaml` (images, redis storage, proxy nodePort,
`keda.enabled`, and per-field `defaults` used when a model omits something).

> The hand-written manifests in `infra/k8s/*.yaml` are **superseded** by this
> chart (kept only for `kind-config.yaml` and legacy cleanup in `stop-k8s.sh`).
