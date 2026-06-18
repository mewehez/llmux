# TODO

Roadmap beyond the published baseline. **Tier 1 — README, MIT LICENSE, CI, and
branch protection — is done.** What follows is the prioritized backlog from the
project review.

## Tier 2 — Headline feature

- [ ] **Thinking vs. output token streaming** (reasoning models)
  - Classify `<think>…</think>` tokens in the worker and stream them as a
    distinct event type (separate from output tokens).
  - Render reasoning separately (collapsible) in the dashboard Chat panel.
  - Relevant now that the Qwen3 reasoning models are in the registry; builds
    directly on the existing token fan-out (`worker/main.py::stream_inference`).

## Tier 3 — Engineering hardening

- [ ] **Split `api/main.py`** (~800 lines) into FastAPI routers
      (`chat`, `sse`, `metrics`, `benchmark`, `scale`).
- [ ] **Broaden test coverage**: the SSE replay / pub-sub path (`/sse/{id}`),
      the benchmark harness, and `model_timeseries` aggregation.
- [ ] **CI: build the Docker images** (and optionally push to GHCR on tags) so
      the pipeline proves the images build, not just that the code tests pass.
- [ ] **Load tester: optionally surface a response preview** per task — see the
      trade-off note below.

## Tier 4 — Production-grade

- [ ] **Stand up the 128 GB Qwen deployment** — flip `enabled: true` on
      `qwen3-8b` / `qwen3-32b` and validate the sized profile on the real box.
- [ ] **Prometheus + Grafana** — the deliberately-deferred observability upgrade
      (current telemetry is Redis-native).
- [ ] **API auth + rate limiting** — the API is currently open; real serving
      needs API keys / quotas.

## Known trade-offs / notes

- **Load tester shows completion stats but not answer text — by design.**
  During a run it consumes **one** `/events/stream` connection instead of **N**
  per-task `/sse/{id}` streams, to avoid the browser's ~6-connections-per-host
  limit starving the dashboard's own polling (which made the Overview look
  frozen). The `task_completed` events carry tokens / latency / tok-s but not the
  generated text, so `RunCard` shows the prompt + stats only.
  - To restore answers without the connection problem: add
    `GET /result/{task_id}` that assembles the text from the 300 s replay stream
    (`llm:result:stream:{task_id}`) and lazy-load a single answer when a
    `RunCard` row is expanded (one connection at a time).
