import asyncio
import json
import logging
import signal
import time
from urllib.parse import urlparse

import httpx
import redis.asyncio as aioredis

from runners import get_runner
from settings import settings
from telemetry import LiveState, emit_event, record_completion

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ── Config ──────────────────────────────────────────────────────────────────

# Config is loaded from environment / .env via pydantic (see settings.py).
REDIS_URL      = settings.redis_url
STREAM         = settings.stream
LLM_URL        = settings.llm_url
CONSUMER_GROUP = settings.consumer_group
WORKER_ID      = settings.worker_id
MODEL_NAME     = settings.model_name
# The llm target this worker calls. In compose/k8s LLM_URL points at the llm
# Service (e.g. http://llm-135m:8080), so this is the model's llm endpoint.
LLM_HOST       = urlparse(LLM_URL).hostname or LLM_URL
# How many requests this worker handles concurrently. Should match llama.cpp's
# --parallel so a single worker can saturate the model's slots.
CONCURRENCY    = settings.worker_concurrency
BLOCK_MS       = settings.block_ms
WORKER_TTL     = settings.worker_ttl                 # metrics hash expiry; refreshed each heartbeat
HEARTBEAT_INTERVAL = settings.heartbeat_interval     # seconds between liveness pings
# Inference request shaping
LLM_MODEL      = settings.llm_model                  # model name sent to the runner
MAX_TOKENS     = settings.max_tokens
HTTP_TIMEOUT   = settings.http_timeout               # seconds for the runner call
# Pluggable backend (llamacpp | ollama | vllm), selected by the registry.
RUNNER         = settings.runner
runner         = get_runner(RUNNER, LLM_URL)
# Result stream retention
RESULT_TTL           = settings.result_ttl           # seconds to keep replay stream
RESULT_STREAM_MAXLEN = settings.result_stream_maxlen # max tokens buffered for replay
RESULT_PREFIX  = "llm:result:"
RESULT_STREAM_PREFIX = "llm:result:stream:"
METRICS_KEY    = f"llm:worker:{WORKER_ID}"
# Resilience (Phase 6)
DEAD_STREAM       = settings.dead_stream
DEAD_MAXLEN       = settings.dead_maxlen
MAX_ATTEMPTS      = settings.max_attempts
CLAIM_MIN_IDLE_MS = settings.claim_min_idle_ms
CLAIM_INTERVAL    = settings.claim_interval
CLAIM_COUNT       = settings.claim_count
LIVENESS_FILE     = settings.liveness_file

# ── Redis ────────────────────────────────────────────────────────────────────

_redis: aioredis.Redis | None = None


async def get_redis() -> aioredis.Redis:
    global _redis
    if _redis is None:
        _redis = await aioredis.from_url(REDIS_URL, decode_responses=False)
    return _redis


# Shared HTTP client (connection pool) reused across all inferences. Opening a
# new TCP connection per request — as we did before — caused connection-refused
# / "server disconnected" errors against llama.cpp under load. The pool is
# bounded so the worker can't open more connections than it has capacity for.
_http_client: httpx.AsyncClient | None = None


def get_http_client() -> httpx.AsyncClient:
    global _http_client
    if _http_client is None:
        _http_client = httpx.AsyncClient(
            timeout=HTTP_TIMEOUT,
            limits=httpx.Limits(
                max_connections=CONCURRENCY + 2,          # main concurrency + reclaim + headroom
                max_keepalive_connections=CONCURRENCY,
            ),
        )
    return _http_client


# ── Consumer group setup ─────────────────────────────────────────────────────

async def ensure_group(redis: aioredis.Redis) -> None:
    try:
        await redis.xgroup_create(STREAM, CONSUMER_GROUP, id="0", mkstream=True)
        logger.info("Consumer group '%s' created on stream '%s'", CONSUMER_GROUP, STREAM)
    except Exception as exc:
        if "BUSYGROUP" in str(exc):
            logger.info("Consumer group '%s' already exists", CONSUMER_GROUP)
        else:
            raise


# ── Inference ─────────────────────────────────────────────────────────────────

async def stream_inference(message: str, task_id: str, redis: aioredis.Redis, live: LiveState) -> dict:
    """
    Stream a completion from the configured runner (llamacpp / ollama / vllm),
    pushing tokens to the Redis result stream + pub/sub as they arrive, then a
    final done or error event. Returns the runner's normalized timings.

    The runner owns the backend wire protocol; this function owns the Redis
    result fan-out and live-state tracking.
    """
    channel = f"{RESULT_PREFIX}{task_id}"
    result_stream = f"{RESULT_STREAM_PREFIX}{task_id}"

    seq = {"n": 0}

    async def on_token(delta: str) -> None:
        seq["n"] += 1
        # seq lets late SSE clients dedupe a token seen in both replay + pub/sub.
        payload = {"type": "token", "content": delta, "seq": str(seq["n"])}
        await redis.xadd(result_stream, payload, maxlen=RESULT_STREAM_MAXLEN)
        await redis.publish(channel, json.dumps(payload))
        live.token(task_id)        # count token + capture TTFT
        await live.flush()         # throttled live-state write

    try:
        client = get_http_client()  # shared pooled client (no per-request connection)
        timing_info = await runner.generate(
            client, message, max_tokens=MAX_TOKENS, model=LLM_MODEL, on_token=on_token,
        )

        done_event = {
            "type":      "done",
            "task_id":   task_id,
            "worker_id": WORKER_ID,
            "llm":       LLM_HOST,
            "model":     MODEL_NAME,
            "runner":    RUNNER,
        }
        await redis.xadd(result_stream, done_event)
        await redis.publish(channel, json.dumps(done_event))
        logger.info("Task %s completed by worker %s via %s (%s)", task_id, WORKER_ID, LLM_HOST, RUNNER)
        # Set TTL on the result stream
        await redis.expire(result_stream, RESULT_TTL)
        return timing_info

    except Exception as exc:
        logger.error("Task %s failed: %s", task_id, exc)
        error_event = {
            "type":      "error",
            "content":   f"{type(exc).__name__}: {exc}",
            "task_id":   task_id,
            "worker_id": WORKER_ID,
            "llm":       LLM_HOST,
            "model":     MODEL_NAME,
            "runner":    RUNNER,
        }
        await redis.xadd(result_stream, error_event)
        await redis.publish(channel, json.dumps(error_event))
        # Set TTL on the result stream
        await redis.expire(result_stream, RESULT_TTL)
        raise


# ── Process one stream entry ──────────────────────────────────────────────────

async def dead_letter(redis: aioredis.Redis, task_id: str, decoded: dict, reason: str) -> None:
    """Move a permanently-failed task to the dead-letter stream so it isn't lost
    (for inspection / manual replay), and mark it dead."""
    await redis.xadd(
        DEAD_STREAM,
        {
            "task_id":   task_id,
            "model":     decoded.get("model", MODEL_NAME),
            "message":   decoded.get("message", ""),
            "reason":    reason,
            "worker_id": WORKER_ID,
            "failed_at": str(int(time.time())),
        },
        maxlen=DEAD_MAXLEN,
        approximate=True,
    )
    await redis.hset(f"llm:task:{task_id}", "status", "dead")
    await emit_event(redis, "task_dead", task_id=task_id,
                     model=decoded.get("model", MODEL_NAME), worker_id=WORKER_ID, reason=reason)
    logger.error("[%s] task %s dead-lettered: %s", WORKER_ID, task_id, reason)


async def process(redis: aioredis.Redis, msg_id: bytes, fields: dict, live: LiveState) -> None:
    decoded = {
        (k.decode() if isinstance(k, bytes) else k):
        (v.decode() if isinstance(v, bytes) else v)
        for k, v in fields.items()
    }

    task_id = decoded.get("task_id", "unknown")
    message = decoded.get("message", "")

    # Attempt tracking: a poison task that keeps failing (or a crash that keeps
    # getting reclaimed) is dead-lettered instead of retrying forever.
    attempt = await redis.hincrby(f"llm:task:{task_id}", "attempts", 1)
    if attempt > MAX_ATTEMPTS:
        await dead_letter(redis, task_id, decoded, reason=f"exceeded {MAX_ATTEMPTS} attempts")
        await redis.xack(STREAM, CONSUMER_GROUP, msg_id)
        return

    logger.info("[%s] Processing task %s (attempt %d/%d)", WORKER_ID, task_id, attempt, MAX_ATTEMPTS)
    await redis.hset(f"llm:task:{task_id}", "status", "running")
    live.start(task_id)
    await emit_event(redis, "task_started", task_id=task_id, model=MODEL_NAME, worker_id=WORKER_ID)
    await live.flush(force=True)

    start = time.monotonic()
    succeeded = False
    try:
        timing_info = await stream_inference(message, task_id, redis, live)
        await redis.hset(f"llm:task:{task_id}", "status", "done")

        latency_ms = (time.monotonic() - start) * 1000
        snap = live.stats(task_id)
        tokens = snap["tokens"]
        ttft_ms = snap["ttft_ms"]
        # Prefer the runner's normalized decode rate; fall back to tokens/elapsed.
        tokens_per_sec = timing_info.get("decode_tok_s") or (
            tokens / (latency_ms / 1000) if latency_ms > 0 else 0
        )

        await redis.hset(METRICS_KEY, mapping={
            "model":           MODEL_NAME,
            "last_latency_ms": str(round(latency_ms, 1)),
            "tokens_per_sec":  str(round(tokens_per_sec, 2)),
            "last_seen":       str(int(time.time())),
        })
        await redis.hincrby(METRICS_KEY, "total_reqs", 1)

        await emit_event(
            redis, "task_completed",
            task_id=task_id, model=MODEL_NAME, worker_id=WORKER_ID, llm=LLM_HOST, runner=RUNNER,
            tokens=tokens, ttft_ms=round(ttft_ms, 1),
            tok_s=round(tokens_per_sec, 2), latency_ms=round(latency_ms, 1),
        )
        await record_completion(
            redis, MODEL_NAME,
            latency_ms=latency_ms, tokens=tokens, tok_s=tokens_per_sec, task_id=task_id,
        )
        succeeded = True
    except Exception as exc:
        await redis.hset(f"llm:task:{task_id}", "status", "error")
        await redis.hset(METRICS_KEY, mapping={
            "model":     MODEL_NAME,
            "last_seen": str(int(time.time())),
        })
        await redis.hincrby(METRICS_KEY, "total_reqs", 1)
        await redis.hincrby(METRICS_KEY, "errors", 1)
        await emit_event(
            redis, "task_error",
            task_id=task_id, model=MODEL_NAME, worker_id=WORKER_ID,
            # Always include the exception type — some exceptions stringify empty.
            error=f"{type(exc).__name__}: {exc}"[:200],
            attempt=attempt,
        )
    finally:
        live.finish(task_id)
        await live.flush(force=True)
        await redis.expire(METRICS_KEY, WORKER_TTL)

    # ACK on success or final failure; otherwise leave in the PEL so XAUTOCLAIM
    # redelivers it after CLAIM_MIN_IDLE_MS (retry with built-in backoff).
    if succeeded:
        await redis.xack(STREAM, CONSUMER_GROUP, msg_id)
        logger.info("[%s] ACKed task %s", WORKER_ID, task_id)
    elif attempt >= MAX_ATTEMPTS:
        await dead_letter(redis, task_id, decoded, reason="failed after max attempts")
        await redis.xack(STREAM, CONSUMER_GROUP, msg_id)
    else:
        logger.warning("[%s] task %s failed (attempt %d/%d) — will retry via reclaim",
                       WORKER_ID, task_id, attempt, MAX_ATTEMPTS)


async def heartbeat(redis: aioredis.Redis) -> None:
    """
    Refresh liveness independent of task processing so an idle-but-alive
    worker doesn't appear 'down'. Sets model/last_seen, registers counters
    on first run, and refreshes the metrics-hash TTL.
    """
    await redis.hset(METRICS_KEY, mapping={
        "model":     MODEL_NAME,
        "last_seen": str(int(time.time())),
    })
    # Initialise counters only if absent (don't clobber accumulated totals).
    await redis.hsetnx(METRICS_KEY, "total_reqs", 0)
    await redis.hsetnx(METRICS_KEY, "errors", 0)
    await redis.expire(METRICS_KEY, WORKER_TTL)
    # Touch a liveness file so the k8s probe can verify the heartbeat loop is
    # actually running (not just that PID 1 exists).
    try:
        with open(LIVENESS_FILE, "w") as f:
            f.write(str(int(time.time())))
    except OSError:
        pass


async def heartbeat_loop(redis: aioredis.Redis, shutdown: asyncio.Event, interval: int = HEARTBEAT_INTERVAL) -> None:
    """Refresh liveness on a fixed cadence, independent of how busy the consume
    loop is — so a worker saturated with concurrent inferences never looks down."""
    while not shutdown.is_set():
        try:
            await heartbeat(redis)
        except Exception as exc:
            logger.warning("[%s] heartbeat failed: %s", WORKER_ID, exc)
        try:
            await asyncio.wait_for(shutdown.wait(), timeout=interval)
        except asyncio.TimeoutError:
            pass


# ── Crash recovery: reclaim stuck entries from the PEL ────────────────────────

async def reclaim_loop(redis: aioredis.Redis, live: LiveState, shutdown: asyncio.Event) -> None:
    """Periodically reclaim work entries idle in the Pending Entries List longer
    than CLAIM_MIN_IDLE_MS (a worker took them but crashed before ACKing, or a
    prior attempt failed and was left for retry) and reprocess them. This is why
    we use Redis Streams over pub/sub — no task is silently lost on a crash."""
    while not shutdown.is_set():
        try:
            result = await redis.xautoclaim(
                STREAM, CONSUMER_GROUP, WORKER_ID,
                min_idle_time=CLAIM_MIN_IDLE_MS, start_id="0-0", count=CLAIM_COUNT,
            )
            # redis-py returns (next_cursor, claimed[, deleted]); claimed = [(id, fields), ...]
            claimed = result[1] if len(result) >= 2 else []
            for msg_id, fields in claimed:
                if not fields:
                    # Tombstone (entry was deleted) — clear it from the PEL.
                    await redis.xack(STREAM, CONSUMER_GROUP, msg_id)
                    continue
                logger.info("[%s] Reclaiming stuck task %s", WORKER_ID, msg_id)
                await process(redis, msg_id, fields, live)
        except asyncio.CancelledError:
            break
        except Exception as exc:
            logger.warning("[%s] reclaim error: %s", WORKER_ID, exc)
        try:
            await asyncio.wait_for(shutdown.wait(), timeout=CLAIM_INTERVAL)
        except asyncio.TimeoutError:
            pass


# ── Main loop ─────────────────────────────────────────────────────────────────

async def consume_loop() -> None:
    redis    = await get_redis()
    await ensure_group(redis)
    await heartbeat(redis)  # register immediately so the dashboard sees us before any task
    logger.info(
        "[%s] Worker started on stream '%s' → %s (concurrency=%d)",
        WORKER_ID, STREAM, LLM_URL, CONCURRENCY,
    )

    # Live-state tracker + a worker_up event so the dashboard sees us join.
    live = LiveState(redis, METRICS_KEY)
    await live.flush(force=True)
    await emit_event(redis, "worker_up", worker_id=WORKER_ID, model=MODEL_NAME)

    shutdown = asyncio.Event()

    def handle_signal(*_):
        logger.info("[%s] Shutdown signal received", WORKER_ID)
        shutdown.set()

    loop = asyncio.get_event_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, handle_signal)

    hb_task = asyncio.create_task(heartbeat_loop(redis, shutdown))
    reclaim_task = asyncio.create_task(reclaim_loop(redis, live, shutdown))

    # Up to CONCURRENCY inferences run at once so one worker can fill all of
    # llama.cpp's --parallel slots. We only pull as many new messages as we
    # have free capacity for, leaving the rest in the stream for other workers.
    inflight: set[asyncio.Task] = set()

    while not shutdown.is_set():
        inflight = {t for t in inflight if not t.done()}
        free = CONCURRENCY - len(inflight)

        if free <= 0:
            # At capacity — wait for at least one inference to finish.
            await asyncio.wait(inflight, return_when=asyncio.FIRST_COMPLETED)
            continue

        try:
            results = await redis.xreadgroup(
                groupname    = CONSUMER_GROUP,
                consumername = WORKER_ID,
                streams      = {STREAM: ">"},
                count        = free,
                block        = BLOCK_MS,
            )
        except asyncio.CancelledError:
            break
        except Exception as exc:
            logger.error("XREADGROUP error: %s", exc)
            await asyncio.sleep(1)
            continue

        if not results:
            continue

        for _stream, messages in results:
            for msg_id, fields in messages:
                inflight.add(asyncio.create_task(process(redis, msg_id, fields, live)))

    # Drain in-flight inferences before exiting.
    if inflight:
        logger.info("[%s] Draining %d in-flight task(s)", WORKER_ID, len(inflight))
        await asyncio.gather(*inflight, return_exceptions=True)
    await emit_event(redis, "worker_down", worker_id=WORKER_ID, model=MODEL_NAME)
    hb_task.cancel()
    reclaim_task.cancel()
    if _http_client is not None:
        await _http_client.aclose()
    logger.info("[%s] Worker shut down cleanly", WORKER_ID)


if __name__ == "__main__":
    asyncio.run(consume_loop())
