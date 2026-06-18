import asyncio
import json
import logging
import math
import time
import uuid
from contextlib import asynccontextmanager
from typing import AsyncGenerator

import redis.asyncio as aioredis
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from settings import settings

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Identifies this API process for the scale-reconciler leader lock.
INSTANCE_ID = uuid.uuid4().hex[:8]


@asynccontextmanager
async def lifespan(app: FastAPI):
    task = asyncio.create_task(scale_reconciler()) if settings.scale_events_enabled else None
    try:
        yield
    finally:
        if task:
            task.cancel()


app = FastAPI(title="LLM Server API", lifespan=lifespan)

# CORS allowlist is env-driven (CORS_ORIGINS, comma-separated, or "*").
_cors_origins = (
    ["*"] if settings.cors_origins.strip() == "*"
    else [o.strip() for o in settings.cors_origins.split(",") if o.strip()]
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Config ─────────────────────────────────────────────────────────────────

# Config is loaded from environment / .env via pydantic (see settings.py).
REDIS_URL          = settings.redis_url
RESULT_PREFIX      = "llm:result:"
EVENTS_STREAM      = "llm:events"
TASK_TTL           = settings.task_ttl
SSE_TIMEOUT        = settings.sse_timeout
SSE_POLL           = settings.sse_poll
WORK_STREAM_MAXLEN = settings.work_stream_maxlen
WORKER_HEALTHY_TTL = settings.worker_healthy_ttl
SLOTS_STALE_AFTER  = settings.slots_stale_after
STREAM_MAP         = settings.stream_map

# ── Redis client ────────────────────────────────────────────────────────────

_redis: aioredis.Redis | None = None


async def get_redis() -> aioredis.Redis:
    global _redis
    if _redis is None:
        _redis = await aioredis.from_url(
            REDIS_URL,
            decode_responses=False,
        )
    return _redis


# ── Schemas ─────────────────────────────────────────────────────────────────

class ChatRequest(BaseModel):
    message: str
    model:   str = "135m"   # "135m", "360m", or "auto"
    session_id: str | None = None


class TaskCreated(BaseModel):
    task_id:    str
    session_id: str
    model:      str
    stream:     str


# ── Helpers ──────────────────────────────────────────────────────────────────

async def enqueue(message: str, model: str, session_id: str) -> str:
    stream = STREAM_MAP.get(model)
    if stream is None:
        raise ValueError(f"Unknown model '{model}'. Choose: {list(STREAM_MAP)}")

    task_id = str(uuid.uuid4())
    redis   = await get_redis()

    await redis.xadd(
        stream,
        {
            "task_id":    task_id,
            "session_id": session_id,
            "model":      model,
            "message":    message,
        },
        maxlen=WORK_STREAM_MAXLEN,
        approximate=True,
    )

    await redis.hset(
        f"llm:task:{task_id}",
        mapping={"status": "queued", "model": model, "session_id": session_id},
    )
    await redis.expire(f"llm:task:{task_id}", TASK_TTL)

    logger.info("Enqueued task %s → stream %s", task_id, stream)
    return task_id


# ── Routes ───────────────────────────────────────────────────────────────────

@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/config")
async def config():
    """Public view of the model registry — drives the dashboard so it never
    hardcodes the model set, scaling maxes, or runner type."""
    return {
        "models": [
            {
                "id":          m.id,
                "label":       m.display_label,
                "runner":      m.runner,
                "maxReplicas": m.max_replicas,
                "maxLlmPods":  m.max_llm_pods,
            }
            for m in settings.models
        ]
    }


async def resolve_model(redis: aioredis.Redis, model: str) -> str:
    if model != "auto":
        return model

    best_model, best_depth = None, None
    for candidate, stream in STREAM_MAP.items():
        q = await model_queue_depth(redis, candidate, stream)
        if best_depth is None or q["queue_depth"] < best_depth:
            best_model, best_depth = candidate, q["queue_depth"]
    return best_model


@app.post("/chat", response_model=TaskCreated)
async def chat(request: ChatRequest):
    session_id = request.session_id or str(uuid.uuid4())
    redis = await get_redis()

    model = await resolve_model(redis, request.model)
    if model not in STREAM_MAP:
        raise HTTPException(400, f"Unknown model '{request.model}'. Choose: {list(STREAM_MAP)} or 'auto'")

    try:
        task_id = await enqueue(request.message, model, session_id)
    except ValueError as exc:
        raise HTTPException(400, str(exc))

    return TaskCreated(
        task_id    = task_id,
        session_id = session_id,
        model      = model,
        stream     = STREAM_MAP[model],
    )


@app.get("/sse/{task_id}")
async def sse(task_id: str):
    redis = await get_redis()

    meta = await redis.hgetall(f"llm:task:{task_id}")
    if not meta:
        raise HTTPException(404, f"Task {task_id} not found")

    async def event_stream() -> AsyncGenerator[str, None]:
        result_stream = f"llm:result:stream:{task_id}"
        channel = f"{RESULT_PREFIX}{task_id}"
        pubsub  = redis.pubsub()
        await pubsub.subscribe(channel)

        last_seq = 0  # highest token seq already delivered (replay → pub/sub dedup)
        try:
            # First replay any tokens already in the result stream
            existing = await redis.xrange(result_stream)
            for _, fields in existing:
                payload = {
                    k.decode() if isinstance(k, bytes) else k:
                    v.decode() if isinstance(v, bytes) else v
                    for k, v in fields.items()
                }
                seq = payload.get("seq")
                if seq is not None:
                    last_seq = max(last_seq, int(seq))
                yield f"data: {json.dumps(payload)}\n\n"
                if payload.get("type") in ("done", "error"):
                    return

            # Then listen on pub/sub for tokens still in flight
            silence = 0.0
            poll    = SSE_POLL

            while silence < SSE_TIMEOUT:
                msg = await pubsub.get_message(
                    ignore_subscribe_messages=True,
                    timeout=poll,
                )

                if msg is None:
                    silence += poll
                    continue

                silence = 0.0
                data    = msg["data"]

                if isinstance(data, bytes):
                    data = data.decode()

                try:
                    payload = json.loads(data)
                except json.JSONDecodeError:
                    continue

                # Skip a token already delivered during replay (it may also be
                # published on pub/sub if it landed in the gap before we read).
                seq = payload.get("seq")
                if seq is not None:
                    if int(seq) <= last_seq:
                        continue
                    last_seq = int(seq)

                yield f"data: {json.dumps(payload)}\n\n"

                if payload.get("type") in ("done", "error"):
                    return

        finally:
            await pubsub.unsubscribe(channel)
            await pubsub.aclose()

        yield f"data: {json.dumps({'type': 'error', 'content': 'timeout'})}\n\n"

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control":     "no-cache",
            "X-Accel-Buffering": "no",
            "X-Task-Id":         task_id,
        },
    )


@app.get("/tasks/{task_id}")
async def task_status(task_id: str):
    redis = await get_redis()
    meta  = await redis.hgetall(f"llm:task:{task_id}")
    if not meta:
        raise HTTPException(404, f"Task {task_id} not found")
    return {
        k.decode() if isinstance(k, bytes) else k:
        v.decode() if isinstance(v, bytes) else v
        for k, v in meta.items()
    }


async def model_queue_depth(redis: aioredis.Redis, model: str, stream: str) -> dict:
    group = f"llm-workers-{model}"

    try:
        group_info = await redis.xinfo_groups(stream)
        group_data = next(
            (g for g in group_info if g["name"].decode() == group),
            None,
        )
        lag = group_data["lag"] if group_data else 0
        last_delivered_id = group_data["last-delivered-id"].decode() if group_data else None
    except Exception:
        lag = 0
        last_delivered_id = None

    # Pending entries — consumed but not yet ACKed
    try:
        pending = await redis.xpending(stream, group)
        pending_count = pending["pending"]
    except Exception:
        pending_count = 0

    # Last entry delivered to the consumer group
    try:
        if last_delivered_id and last_delivered_id != "0-0":
            last_entries = await redis.xrange(stream, min=last_delivered_id, max=last_delivered_id)
        else:
            last_entries = []
    except Exception:
        last_entries = []

    last_task = None
    if last_entries:
        fields = last_entries[0][1]
        last_task = {
            "task_id": fields.get(b"task_id", b"").decode(),
            "message": fields.get(b"message", b"").decode(),
        }

    return {
        "lag":          lag,
        "pending":      pending_count,
        "queue_depth":  lag + pending_count,
        "last_task":    last_task,
    }


@app.get("/workers/status")
async def workers_status():
    redis = await get_redis()

    statuses = []
    for model, stream in STREAM_MAP.items():
        q = await model_queue_depth(redis, model, stream)
        statuses.append({
            "model":         model,
            "stream":        stream,
            "queue_depth":   q["queue_depth"],
            "pending":       q["pending"],
            "last_task":     q["last_task"],
            "llm_url":       settings.llm_url(model),
        })

    return {"workers": statuses}


@app.get("/replicas/status")
async def replicas_status():
    redis = await get_redis()

    replicas = []
    now = int(time.time())
    cursor = 0
    while True:
        cursor, keys = await redis.scan(cursor, match="llm:worker:*", count=100)
        for key in keys:
            data = await redis.hgetall(key)
            if not data:
                continue
            fields = {
                k.decode() if isinstance(k, bytes) else k:
                v.decode() if isinstance(v, bytes) else v
                for k, v in data.items()
            }
            worker_id = key.decode().split("llm:worker:", 1)[1]
            last_seen = int(fields.get("last_seen", "0"))
            model = fields.get("model", "unknown")

            replicas.append({
                "workerId":     worker_id,
                "model":        model,
                "totalReqs":    int(fields.get("total_reqs", "0")),
                "errors":       int(fields.get("errors", "0")),
                # Per-worker LAST latency (not a percentile). Real percentiles are
                # per-model in /metrics/timeseries. latencyP50 kept for back-compat.
                "lastLatencyMs": float(fields.get("last_latency_ms", "0")),
                "latencyP50":   float(fields.get("last_latency_ms", "0")),
                "tokensPerSec": float(fields.get("tokens_per_sec", "0")),
                # Live state (Phase 2) — what this worker is doing right now.
                "state":        fields.get("state", "idle"),
                "inflight":     int(fields.get("inflight", "0")),
                "liveTokS":     float(fields.get("live_tok_s", "0")),
                "ttftMs":       float(fields.get("ttft_ms", "0")),
                "lastTask":     fields.get("last_task", ""),
                "lastSeen":     last_seen,
                "healthy":      (now - last_seen) < WORKER_HEALTHY_TTL,
            })
        if cursor == 0:
            break

    return {"replicas": replicas}


async def model_slots(redis: aioredis.Redis, model: str) -> dict:
    """Per-pod llama.cpp slot utilisation for a model.

    Slot data is published by the per-pod slots sidecar into hashes
    llm:slots:{model}:{pod} (TTL-expired, so dead pods drop off). Each pod's
    `total` equals llama.cpp's --parallel value (the max concurrent requests it
    can hold); `processing` is how many of those slots are in flight; `deferred`
    is requests queued inside llama.cpp waiting for a free slot.

    Returns the per-pod list plus a model-level aggregate.
    """
    now = int(time.time())
    pods: list[dict] = []

    cursor = 0
    while True:
        cursor, keys = await redis.scan(cursor, match=f"llm:slots:{model}:*", count=100)
        for key in keys:
            data = await redis.hgetall(key)
            if not data:
                continue
            fields = {
                k.decode() if isinstance(k, bytes) else k:
                v.decode() if isinstance(v, bytes) else v
                for k, v in data.items()
            }
            if (now - int(fields.get("last_seen", "0"))) >= SLOTS_STALE_AFTER:
                continue
            pods.append({
                "pod":        fields.get("pod", key.decode().split(":", 3)[-1]),
                "processing": int(fields.get("processing", "0")),
                "total":      int(fields.get("total", "0")),  # = --parallel
                "deferred":   int(fields.get("deferred", "0")),
            })
        if cursor == 0:
            break

    pods.sort(key=lambda p: p["pod"])
    return {
        "pods":        pods,
        "activeSlots": sum(p["processing"] for p in pods),
        "maxSlots":    sum(p["total"] for p in pods),
        "deferred":    sum(p["deferred"] for p in pods),
    }


@app.get("/cluster/status")
async def cluster_status():
    redis = await get_redis()

    now = int(time.time())
    active_by_model: dict[str, set[str]] = {model: set() for model in STREAM_MAP}

    cursor = 0
    while True:
        cursor, keys = await redis.scan(cursor, match="llm:worker:*", count=100)
        for key in keys:
            data = await redis.hgetall(key)
            if not data:
                continue
            fields = {
                k.decode() if isinstance(k, bytes) else k:
                v.decode() if isinstance(v, bytes) else v
                for k, v in data.items()
            }
            last_seen = int(fields.get("last_seen", "0"))
            model = fields.get("model")
            if model in active_by_model and (now - last_seen) < WORKER_HEALTHY_TTL:
                worker_id = key.decode().split("llm:worker:", 1)[1]
                active_by_model[model].add(worker_id)
        if cursor == 0:
            break

    models = []
    for model, stream in STREAM_MAP.items():
        q = await model_queue_depth(redis, model, stream)
        slots = await model_slots(redis, model)
        max_replicas = settings.max_replicas(model)
        max_llm_pods = settings.max_llm_pods(model)
        models.append({
            "model":        model,
            # worker replicas (stateless, scale on queue depth)
            "replicas":     len(active_by_model[model]),
            "maxReplicas":  max_replicas,
            # llm pods (llama.cpp; scale independently, RAM-bound)
            "llmPods":      len(slots["pods"]),
            "maxLlmPods":   max_llm_pods,
            "queueDepth":   q["queue_depth"],
            "activeSlots":  slots["activeSlots"],
            "maxSlots":     slots["maxSlots"],
            "deferred":     slots["deferred"],
            "slotPods":     slots["pods"],
        })

    scale_events = int(await redis.get("llm:scale:count") or 0)
    return {"models": models, "scaleEvents": scale_events}


# ── Telemetry: events + time series (Phase 2) ─────────────────────────────────

def _event_from_entry(entry_id, fields: dict) -> dict:
    ev = {
        (k.decode() if isinstance(k, bytes) else k):
        (v.decode() if isinstance(v, bytes) else v)
        for k, v in fields.items()
    }
    ev["id"] = entry_id.decode() if isinstance(entry_id, bytes) else entry_id
    return ev


def _percentile(sorted_vals: list[float], p: float) -> float:
    """Linear-interpolated percentile over a pre-sorted list (p in 0..1)."""
    if not sorted_vals:
        return 0.0
    k = (len(sorted_vals) - 1) * p
    lo, hi = math.floor(k), math.ceil(k)
    if lo == hi:
        return sorted_vals[int(k)]
    return sorted_vals[lo] * (hi - k) + sorted_vals[hi] * (k - lo)


@app.get("/events")
async def events(limit: int = 50):
    """Recent lifecycle events, newest first."""
    redis = await get_redis()
    entries = await redis.xrevrange(EVENTS_STREAM, count=limit)
    return {"events": [_event_from_entry(eid, f) for eid, f in entries]}


@app.get("/events/stream")
async def sse_events():
    """Live activity feed: replay the most recent events, then stream new ones.
    Note: distinct from /sse/{task_id} (per-task token stream) to avoid the
    path-param route shadowing /sse/events."""
    redis = await get_redis()

    async def event_stream() -> AsyncGenerator[str, None]:
        # Replay recent events in chronological order.
        recent = list(reversed(await redis.xrevrange(EVENTS_STREAM, count=settings.events_replay)))
        last_id = "0"
        for eid, fields in recent:
            ev = _event_from_entry(eid, fields)
            last_id = ev["id"]
            yield f"data: {json.dumps(ev)}\n\n"

        # Then block-read for new events; ping on idle to keep the stream open.
        if last_id == "0":
            last_id = "$"
        while True:
            try:
                results = await redis.xread({EVENTS_STREAM: last_id}, block=15000, count=100)
            except asyncio.CancelledError:
                break
            except Exception:
                # A blocking XREAD can hit a client-side read timeout (block races
                # the socket timeout) or a transient redis hiccup. That must NOT
                # kill the SSE — emit a keep-alive and retry, so the dashboard's
                # Activity feed stays connected instead of flapping.
                yield ": ping\n\n"
                continue
            if not results:
                yield ": ping\n\n"
                continue
            for _stream, messages in results:
                for eid, fields in messages:
                    ev = _event_from_entry(eid, fields)
                    last_id = ev["id"]
                    yield f"data: {json.dumps(ev)}\n\n"

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


async def model_timeseries(redis: aioredis.Redis, model: str, window: int) -> dict:
    """Aggregate the rolling completion samples for one model into real
    percentiles, throughput, and a per-second series."""
    key = f"llm:ts:completions:{model}"
    now = time.time()
    raw = await redis.zrangebyscore(key, now - window, now)

    samples = []
    for member in raw:
        s = member.decode() if isinstance(member, bytes) else member
        parts = s.split("|")
        if len(parts) < 4:
            continue
        ts_ms, latency, tokens, tok_s = parts[0], parts[1], parts[2], parts[3]
        samples.append({
            "t":       int(ts_ms) // 1000,
            "latency": float(latency),
            "tokens":  int(tokens),
            "tok_s":   float(tok_s),
        })

    latencies = sorted(x["latency"] for x in samples)
    tok_s_vals = sorted(x["tok_s"] for x in samples)

    buckets: dict[int, dict] = {}
    for x in samples:
        b = buckets.setdefault(x["t"], {"tokens": 0, "requests": 0, "lat": []})
        b["tokens"]   += x["tokens"]
        b["requests"] += 1
        b["lat"].append(x["latency"])
    series = [
        {
            "t":          t,
            "tokens":     b["tokens"],
            "requests":   b["requests"],
            "latencyP50": round(_percentile(sorted(b["lat"]), 0.5), 1),
        }
        for t, b in sorted(buckets.items())
    ]

    q = await model_queue_depth(redis, model, settings.stream_map[model])
    slots = await model_slots(redis, model)

    return {
        "model":       model,
        "window":      window,
        "count":       len(samples),
        "requestRate": round(len(samples) / window, 3) if window else 0.0,
        "latency": {
            "p50": round(_percentile(latencies, 0.50), 1),
            "p95": round(_percentile(latencies, 0.95), 1),
            "p99": round(_percentile(latencies, 0.99), 1),
            "avg": round(sum(latencies) / len(latencies), 1) if latencies else 0.0,
        },
        "tokSec": {
            "avg": round(sum(tok_s_vals) / len(tok_s_vals), 2) if tok_s_vals else 0.0,
            "p50": round(_percentile(tok_s_vals, 0.50), 2),
        },
        "series":      series,
        "queueDepth":  q["queue_depth"],
        "activeSlots": slots["activeSlots"],
        "maxSlots":    slots["maxSlots"],
    }


@app.get("/metrics/timeseries")
async def metrics_timeseries(model: str | None = None, window: int | None = None):
    redis = await get_redis()
    window = window or settings.metrics_window
    targets = [model] if model else list(settings.stream_map)
    return {
        "models": [
            await model_timeseries(redis, m, window)
            for m in targets if m in settings.stream_map
        ]
    }


# ── Benchmark results (Phase 3) ───────────────────────────────────────────────
# Written by worker/benchmark.py; exposed read-only here for the dashboard.

class BenchmarkRequest(BaseModel):
    model:       str = "135m"
    count:       int = 20
    concurrency: int = 4
    max_tokens:  int | None = None
    runner:      str | None = None   # override the registry runner
    llm_url:     str | None = None   # override the backend URL (e.g. ollama)
    llm_model:   str | None = None


@app.post("/benchmark")
async def benchmark_run(req: BenchmarkRequest):
    """Enqueue a benchmark run (consumed by the benchmark-runner service)."""
    redis = await get_redis()
    spec = {k: str(v) for k, v in req.model_dump(exclude_none=True).items()}
    await redis.xadd("llm:bench:work", spec, maxlen=100, approximate=True)
    return {"queued": True, "spec": req.model_dump(exclude_none=True)}


@app.get("/benchmark")
async def benchmark_list(limit: int = 20):
    redis = await get_redis()
    ids = await redis.lrange("llm:bench:index", 0, limit - 1)
    runs = []
    for rid in ids:
        rid_s = rid.decode() if isinstance(rid, bytes) else rid
        raw = await redis.get(f"llm:bench:{rid_s}")
        if raw:
            runs.append(json.loads(raw))
    return {"runs": runs}


@app.get("/benchmark/{run_id}")
async def benchmark_get(run_id: str):
    redis = await get_redis()
    raw = await redis.get(f"llm:bench:{run_id}")
    if not raw:
        raise HTTPException(404, f"Benchmark run {run_id} not found")
    return json.loads(raw)


# ── Scale-event reconciler (Phase 4) ──────────────────────────────────────────
# Emits real scale_up / scale_down events by watching worker + llm-pod counts
# via Redis liveness — so whether KEDA (k8s) or `docker compose --scale` (dev)
# changed the replica count, the dashboard sees a real event. Replaces the old
# mock scale events. A best-effort leader lock keeps a single API instance
# emitting when the API itself is scaled out.

async def _emit_event(redis: aioredis.Redis, etype: str, **fields) -> None:
    event = {"type": etype, "ts": str(int(time.time() * 1000))}
    for key, value in fields.items():
        if value is not None:
            event[key] = str(value)
    await redis.xadd(EVENTS_STREAM, event, maxlen=settings.events_maxlen, approximate=True)


async def current_scale_counts(redis: aioredis.Redis) -> dict[str, dict]:
    """Per-model live replica counts: healthy workers + reporting llm pods."""
    now = int(time.time())
    active: dict[str, set] = {m: set() for m in settings.stream_map}

    cursor = 0
    while True:
        cursor, keys = await redis.scan(cursor, match="llm:worker:*", count=100)
        for key in keys:
            data = await redis.hgetall(key)
            if not data:
                continue
            fields = {
                k.decode() if isinstance(k, bytes) else k:
                v.decode() if isinstance(v, bytes) else v
                for k, v in data.items()
            }
            model = fields.get("model")
            last_seen = int(fields.get("last_seen", "0"))
            if model in active and (now - last_seen) < WORKER_HEALTHY_TTL:
                active[model].add(key.decode().split("llm:worker:", 1)[1])
        if cursor == 0:
            break

    counts: dict[str, dict] = {}
    for model in settings.stream_map:
        slots = await model_slots(redis, model)
        counts[model] = {"workers": len(active[model]), "llmPods": len(slots["pods"])}
    return counts


async def _is_leader(redis: aioredis.Redis) -> bool:
    ttl = max(settings.scale_poll * 3, 15)
    await redis.set("llm:scale:leader", INSTANCE_ID, nx=True, ex=ttl)
    val = await redis.get("llm:scale:leader")
    me = bool(val) and (val.decode() if isinstance(val, bytes) else val) == INSTANCE_ID
    if me:
        await redis.expire("llm:scale:leader", ttl)
    return me


async def reconcile_scale_once(redis: aioredis.Redis) -> list[tuple]:
    """Compare current counts to the stored state; emit an event per change.
    First observation of a (model, component) just records its baseline."""
    if not await _is_leader(redis):
        return []

    counts = await current_scale_counts(redis)
    state_key = "llm:scale:state"
    prev_raw = await redis.hgetall(state_key)
    prev = {
        (k.decode() if isinstance(k, bytes) else k):
        (v.decode() if isinstance(v, bytes) else v)
        for k, v in prev_raw.items()
    }

    emitted: list[tuple] = []
    for model, comp_counts in counts.items():
        for component, new in comp_counts.items():
            field = f"{model}:{component}"
            old = int(prev[field]) if field in prev else None
            if old is not None and new != old:
                etype = "scale_up" if new > old else "scale_down"
                await _emit_event(redis, etype, model=model, component=component,
                                  **{"from": old, "to": new})
                await redis.incr("llm:scale:count")  # cumulative counter (survives dashboard reloads)
                emitted.append((etype, model, component, old, new))
            await redis.hset(state_key, field, str(new))
    return emitted


async def scale_reconciler() -> None:
    redis = await get_redis()
    logger.info("Scale reconciler started (instance %s, poll %ss)", INSTANCE_ID, settings.scale_poll)
    while True:
        try:
            await reconcile_scale_once(redis)
        except asyncio.CancelledError:
            break
        except Exception as exc:
            logger.warning("scale reconciler error: %s", exc)
        try:
            await asyncio.sleep(settings.scale_poll)
        except asyncio.CancelledError:
            break
