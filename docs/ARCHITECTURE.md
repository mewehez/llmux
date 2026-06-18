# System Architecture

Multi-model LLM serving platform: a durable Redis work queue decouples a
stateless API from stateless workers, which drive RAM-bound llama.cpp pods.
Two **independent** autoscalers (cheap workers, expensive model pods) both read
one signal — the work-queue **lag** — and the whole thing is observable in real
time. Backpressure is held in Redis (durable), never in llama.cpp's volatile
in-memory queue.

## Component & data-flow diagram

```mermaid
flowchart TB
  classDef store fill:#0b2030,stroke:#60a5fa,color:#e5e7eb;
  classDef svc fill:#0c1a12,stroke:#34d399,color:#e5e7eb;
  classDef ctrl fill:#241a06,stroke:#f59e0b,color:#e5e7eb;

  subgraph CLIENT["Client"]
    BR["Browser · React dashboard<br/>Overview · Activity · Chat · Load · Benchmark · Metrics"]:::svc
  end

  subgraph EDGE["Ingress · single NodePort :30080"]
    NG["nginx reverse proxy<br/>/ → dashboard · /api → API<br/>SSE-friendly · buffering off"]:::svc
  end

  subgraph APIT["API tier · FastAPI · stateless"]
    API["API<br/>POST /chat · GET /sse/{id} · /events/stream<br/>/metrics/timeseries · /config · /benchmark<br/>/cluster · /replicas · /workers status"]:::svc
    REC["Scale reconciler · leader-locked<br/>liveness → scale events + counter"]:::ctrl
  end

  subgraph REDIS["Redis · message backbone + shared state"]
    WQ[("work streams llm:work:{model}<br/>consumer groups · LAG = backlog")]:::store
    RES[("result stream llm:result:stream:{id} replay<br/>+ pub/sub llm:result:{id} live tokens")]:::store
    TASK[("task meta llm:task:{id}<br/>status · attempts")]:::store
    DLQ[("dead-letter llm:work:dead")]:::store
    EV[("llm:events · activity feed · capped")]:::store
    WLIVE[("llm:worker:{id} · live state<br/>state · inflight · tok per s · ttft")]:::store
    TSC[("llm:ts:completions:{model}<br/>samples → p50 p95 p99")]:::store
    SLOTS[("llm:slots:{model}:{pod}<br/>processing · total · deferred")]:::store
    SCL[("llm:scale:state · count · leader")]:::store
    BNCH[("llm:bench:work · llm:bench:{run}")]:::store
  end

  subgraph WK["Worker tier · one Deployment per model · KEDA-scaled"]
    W["Worker · asyncio<br/>IN-FLIGHT ≤ worker_concurrency<br/>XREADGROUP count = concurrency − inflight<br/>Runner: llamacpp · ollama · vllm<br/>reclaim: XAUTOCLAIM → retry / DLQ<br/>shared httpx pool"]:::svc
  end

  subgraph INF["Inference tier · llama.cpp pods · RAM-bound · KEDA-scaled"]
    LLM["llm pod · llama.cpp<br/>SLOTS = --parallel · continuous batching<br/>DEFERRED = requests beyond slots<br/>--threads-http bounds HTTP concurrency"]:::svc
    SIDE["slots sidecar<br/>poll /slots + /metrics · 1s"]:::svc
    PVC[("model PVC · GGUF")]:::store
  end

  BENCHR["benchmark-runner<br/>reuses worker image"]:::svc

  subgraph AUTOS["Autoscaling · KEDA control plane"]
    KEDA["KEDA ScaledObjects<br/>redis-streams trigger on LAG"]:::ctrl
  end

  CFG[("config/models.json registry → ConfigMap<br/>streams · runner · scaling maxes")]:::store

  %% ---- request + token flow ----
  BR -->|"HTTP · SSE"| NG
  NG -->|"/api/*"| API
  API -->|"XADD task"| WQ
  WQ -->|"XREADGROUP new"| W
  W -->|"POST /v1/chat/completions stream<br/>fills a SLOT · overflow → DEFERRED"| LLM
  LLM -->|"tokens"| W
  W -->|"per token: XADD + PUBLISH"| RES
  RES -->|"replay + live"| API
  API -->|"SSE tokens"| BR

  %% ---- task lifecycle / resilience ----
  W -->|"status · attempts"| TASK
  W -->|"after max attempts"| DLQ

  %% ---- telemetry (write) ----
  W -->|"lifecycle events"| EV
  W -->|"heartbeat · live state"| WLIVE
  W -->|"completion sample"| TSC
  SIDE -->|"utilization"| SLOTS
  LLM --- SIDE
  LLM --- PVC

  %% ---- telemetry (read by API for the dashboard) ----
  API -->|"activity SSE"| EV
  API -->|"percentiles"| TSC
  API -->|"slots"| SLOTS
  API -->|"replicas + live"| WLIVE
  API -->|"scale count"| SCL

  %% ---- autoscaling control loop ----
  WQ -.->|"lag 5+ · fast cooldown"| KEDA
  WQ -.->|"lag 15+ · slow cooldown"| KEDA
  KEDA ==>|"scale workers 1..N"| WK
  KEDA ==>|"scale llm pods 1..M · RAM cap"| INF
  WLIVE -.->|"observe replica count"| REC
  SLOTS -.->|"observe pod count"| REC
  REC -->|"emit scale_up / scale_down"| EV
  REC -->|"cumulative counter"| SCL

  %% ---- benchmark ----
  API -->|"POST → enqueue"| BNCH
  BNCH -->|"XREADGROUP"| BENCHR
  BENCHR -->|"workload"| LLM
  BENCHR -->|"results"| BNCH

  %% ---- config (single source of truth) ----
  CFG -.-> API
  CFG -.-> W
  CFG -.->|"GET /config"| BR
```

## Request lifecycle (one chat, streamed)

```mermaid
sequenceDiagram
  autonumber
  participant U as Browser
  participant NG as nginx
  participant A as API
  participant R as Redis
  participant W as Worker
  participant L as llama.cpp pod

  U->>NG: POST /api/chat
  NG->>A: POST /chat
  A->>R: XADD llm:work:{model}
  A-->>U: { task_id }
  U->>NG: GET /api/sse/{task_id}
  NG->>A: GET /sse/{task_id}
  A->>R: replay result stream + SUBSCRIBE pub/sub

  W->>R: XREADGROUP count = concurrency − inflight
  Note over W: worker IN-FLIGHT increments (≤ concurrency)
  W->>L: POST /v1/chat/completions (stream)
  Note over L: takes a SLOT (--parallel) and decodes<br/>if all slots busy → waits in DEFERRED queue

  loop per token
    L-->>W: token delta
    W->>R: XADD result stream + PUBLISH (seq#)
    R-->>A: token (pub/sub)
    A-->>U: SSE data: token
  end

  W->>R: XADD done + completion sample + XACK
  Note over W,R: crash before ACK → XAUTOCLAIM reclaims<br/>fails ≥ max_attempts → llm:work:dead
```

## Legend — the load-bearing concepts

| Concept | What it is | Where |
|---|---|---|
| **Redis work-queue LAG** | entries enqueued but not yet read by the consumer group = the **durable backlog**. The single autoscaling signal. | `llm:work:{model}` |
| **Worker IN-FLIGHT** | tasks one worker decodes concurrently (asyncio). Bounded by `worker_concurrency`; it pulls only `concurrency − inflight` per read, so excess stays in Redis. | `llm:worker:{id}.inflight` |
| **Pod SLOTS** | `--parallel` — requests llama.cpp decodes at once (continuous batching). | llama.cpp |
| **Pod DEFERRED** | requests a pod accepted but can't start (all slots busy) — its volatile in-memory queue. Kept ~0 by design. | `/metrics` → `llm:slots:*` |
| **Worker autoscaler** | KEDA on lag, low threshold + fast cooldown — cheap, stateless. | `keda.yaml` |
| **LLM-pod autoscaler** | KEDA on lag, high threshold + long cooldown — RAM-bound, slow to warm. | `keda.yaml` |
| **Slot (capacity) invariant** | `max_replicas × worker_concurrency ≤ max_llm_pods × parallel` → workers never oversubscribe slots; backlog lives in durable Redis, not llama.cpp. | `config/models.json` |
| **CPU-budget invariant** | `llm_args.threads == resources.llm.requests.cpu` → the k8s scheduler reserves exactly the cores a pod will burn, so overflow KEDA replicas stay `Pending` instead of thrashing CPU. Reserve ~¼ of cores for OS/k8s/redis/workers. See `diagrams/06-capacity-128gb.excalidraw` + `scaling-theory.md`. | `config/models.json` |
| **Resilience** | crash recovery via `XAUTOCLAIM`, retry with attempt tracking, `llm:work:dead` after N fails, SSE token dedup via seq numbers. | worker |
| **Real scale events** | a leader-locked reconciler watches replica counts via Redis liveness and emits real `scale_up`/`scale_down` (works for KEDA *and* `docker compose --scale`). | API |
| **Pluggable runner** | the backend wire protocol (llamacpp / ollama / vllm) behind one interface, selected per model; same harness benchmarks each. | `worker/runners/` |
| **Single source of truth** | the model registry drives streams, routing, scaling maxes, runner type for API + workers + dashboard. | `config/models.json` |
