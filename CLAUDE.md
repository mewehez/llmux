# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this project is

A multi-model LLM serving platform built for learning Kubernetes. It serves two
SmolLM2 models (135M and 360M) via llama.cpp, with a Redis Streams work queue,
SSE token streaming, and a React dashboard for monitoring and load testing.
Runs in both Docker Compose (dev) and Kubernetes (kind).

See [Handoff.md](Handoff.md) for a detailed write-up and [scaling-theory.md](scaling-theory.md)
for the math behind replica/parallelism sizing decisions.

## Architecture

```
Browser → Dashboard (React)
        → POST /chat, GET /sse/{task_id}, GET /workers/status → FastAPI (api/)

FastAPI (api/main.py)
    POST /chat    → XADD to Redis Stream (llm:work:135m or llm:work:360m), returns {task_id, session_id, model, stream}
    GET /sse/{id} → replay llm:result:stream:{task_id}, then subscribe to llm:result:{task_id} pub/sub, stream SSE to browser

Worker (worker/main.py, one deployment per model)
    XREADGROUP from llm:work:{model}
    → POST /v1/chat/completions to llama.cpp (streaming)
    → per token: XADD llm:result:stream:{task_id} (replay) + PUBLISH llm:result:{task_id} (live)
    → XADD done/error event, EXPIRE result stream 300s, XACK

llama.cpp servers: llm-135m (SmolLM2-135M), llm-360m (SmolLM2-360M)

Redis:
    Streams: llm:work:135m, llm:work:360m (queues), llm:result:stream:{task_id} (replay)
    Pub/sub: llm:result:{task_id} (live token push)
    Hashes:  llm:task:{task_id} (metadata, TTL 1h)
```

The dual result mechanism (stream + pub/sub) exists so SSE clients that connect
late can replay missed tokens from the stream, while live clients get push via pub/sub.

## Development

### Docker Compose (primary dev workflow)
```bash
docker compose up -d          # start all services
docker compose logs -f api    # follow logs for a service (api, worker-135m, worker-360m, etc.)
docker compose ps
```
Dev ports: API `8000`, Dashboard `5173` (Vite, hot reload), llm-135m `8001`, llm-360m `8002`, Redis `6379`.

### Dashboard (dashboard/)
```bash
pnpm dev       # vite dev server
pnpm build     # tsc -b && vite build
pnpm lint      # eslint
```

### API / Worker (api/, worker/) — uv-managed Python 3.12 projects
Each has its own `pyproject.toml` / `.venv`. Use `uv run` from within `api/` or `worker/` to execute scripts.

### Kubernetes (kind)
```bash
kubectl config use-context kind-llm-server

./infra/scripts/deploy-k8s.sh          # build images, load into kind, apply manifests, rollout
./infra/scripts/stop-k8s.sh            # delete manifests, keep model PVCs
./infra/scripts/stop-k8s.sh --volumes  # also delete model files (re-downloaded next deploy)
./infra/scripts/stop-k8s.sh --cluster  # nuke entire cluster
```
Port mappings (host → cluster): `30000` API, `30001` Dashboard, `30002` llm-135m, `30003` llm-360m.
k8s is deployed via a **Helm chart** (`infra/helm/llm-server/`) that generates all resources from `config/models.json`. Shared resources (redis, api, dashboard, proxy, benchmark-runner, secret, `llm-models` ConfigMap) render once; per **enabled** model it renders a PVC, the llama.cpp Deployment (init container downloads the GGUF from `model_url`) + slots sidecar + Service, a worker Deployment, and KEDA ScaledObjects. `deploy-k8s.sh` copies `config/models.json` into the chart (`.Files`), installs KEDA, and runs `helm upgrade --install` (which prunes models you set `enabled:false`). Only `infra/k8s/kind-config.yaml` remains (it defines the kind cluster + host→cluster port mappings, used by `deploy-k8s.sh`); the former hand-written manifests were removed in favor of the chart.

## Key conventions

- The **model set is a registry** in `config/models.json` — the COMPLETE single source of truth (app + infra). Each entry: `id`, `label`, `enabled`, `runner`, `model_file`, `model_url` (GGUF source), `llm_args` (ctx_size/threads/parallel/threads_http), `worker_concurrency`, `max_replicas`, `max_llm_pods`, `pvc_size`, `resources`. `stream`/`consumer_group`/`llm_url` are **derived from `id`** (`api/settings.py` + `worker/settings.py` `ModelConfig._derive`). The API derives `stream_map` / scaling maxes / `GET /config` and serves only `enabled` models; each worker picks its entry via `MODEL_ID`; the dashboard fetches `/config`. **Adding a model = one registry entry, then `deploy-k8s.sh`** — the Helm chart generates the k8s resources and the GGUF is fetched into the PVC on first boot. Set `enabled:false` to stop serving one. The registry is bind-mounted in compose; in k8s it's read by the Helm chart (`.Files`) and shipped as the `llm-models` ConfigMap. **Both deploy paths are generated from the registry:** `docker-compose.yml` via `infra/scripts/generate-compose.py` (re-run after editing the registry — it bakes llama.cpp args as literals, host port for `llm-<id>` = 8001 + index), and k8s via the Helm chart.
- Consumer groups: workers call `xgroup_create` defensively (ignore `BUSYGROUP`) on startup so they're idempotent across restarts.
- **Pluggable runners** (`worker/runners/`): the backend wire protocol lives behind a `Runner` interface (`llamacpp`/`vllm` share the OpenAI base; `ollama` is its own). The worker selects one via the registry's `runner` field and delegates the HTTP/streaming protocol to it; Redis result fan-out + telemetry stay in the worker. `worker/benchmark.py` reuses the runners to benchmark backends and writes results to `llm:bench:{run_id}` (read via the API's `GET /benchmark`). The dashboard "Run benchmark" button calls `POST /benchmark`, which enqueues a spec to `llm:bench:work`; the `benchmark-runner` service (`worker/bench_worker.py`, reuses the worker image) consumes and runs it.
- **Telemetry** (Phase 2): workers write lifecycle events to the `llm:events` stream, live state to `llm:worker:{id}`, and completion samples to `llm:ts:completions:{model}`. The API surfaces these via `/events`, `/sse/events`, `/metrics/timeseries` (real percentiles), and live fields on `/replicas/status`.
- Dashboard uses shadcn/ui components (Tailwind v4, base-ui, phosphor icons). UI primitives live in `dashboard/src/components/ui`; feature components (ModelGroup, ActivityFeed, ChatPanel, LoadTester, BenchmarkPanel, MetricsPanel) are tabs in `App.tsx`. Data hooks in `dashboard/src/lib`: `config.ts` (/config), `api.ts` (`useCluster`, honest `mode: live|mock|offline`), `events.ts` (`useEvents` ← `/events/stream` SSE), `metrics.ts` (/metrics/timeseries), `benchmark.ts` (/benchmark). **No silent mock fallback** — mock data only when `VITE_MOCK=1`; otherwise the UI shows an honest offline state. The real activity feed is the server `llm:events` stream, not client-side fiction.
