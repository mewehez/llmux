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
Manifests live in `infra/k8s/`: redis, llm-135m, llm-360m (each with init container that downloads the GGUF model into a PVC), api, worker (two Deployments: worker-135m, worker-360m), dashboard.

## Key conventions

- API and worker select the target model stream via the `STREAM_135M`/`STREAM_360M`/`STREAM` env vars and a `STREAM_MAP` dict (`api/main.py`) — adding a new model means adding entries here plus a new worker deployment and llama.cpp deployment.
- Consumer groups: workers call `xgroup_create` defensively (ignore `BUSYGROUP`) on startup so they're idempotent across restarts.
- Dashboard uses shadcn/ui components (Tailwind v4, base-ui). UI components live in `dashboard/src/components/ui`; feature components (WorkerStatus, MessageSender, LoadTester) are tabs in `App.tsx`.
