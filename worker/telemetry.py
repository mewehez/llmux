"""Redis-native telemetry spine (Phase 2).

Captures, in real time, what a worker is doing:

  * lifecycle EVENTS  → XADD to the capped stream `llm:events`
                        (task_started / task_completed / task_error /
                         worker_up / worker_down) — drives the activity feed.
  * per-worker LIVE STATE → HSET `llm:worker:{id}` (state / inflight /
                        live_tok_s / ttft_ms / last_task) — drives the
                        "what is each worker doing now" view.
  * completion TIME SERIES → ZADD `llm:ts:completions:{model}` one sample per
                        finished request (latency, tokens, decode tok/s),
                        trimmed to a rolling window — drives real percentiles
                        and throughput charts.

The API reads all three; nothing here is mock.
"""

import time

from settings import settings

EVENTS_STREAM = "llm:events"


def _now_ms() -> int:
    return int(time.time() * 1000)


async def emit_event(redis, etype: str, **fields) -> None:
    """Append one lifecycle event to the capped llm:events stream."""
    event = {"type": etype, "ts": str(_now_ms())}
    for key, value in fields.items():
        if value is not None:
            event[key] = str(value)
    await redis.xadd(EVENTS_STREAM, event, maxlen=settings.events_maxlen, approximate=True)


async def record_completion(
    redis, model: str, *, latency_ms: float, tokens: int, tok_s: float, task_id: str
) -> None:
    """Record one finished request as a time-series sample, trimmed to the
    rolling window. Member encodes the sample; score is the unix timestamp."""
    key = f"llm:ts:completions:{model}"
    now = time.time()
    member = f"{int(now * 1000)}|{latency_ms:.1f}|{tokens}|{tok_s:.2f}|{task_id}"
    await redis.zadd(key, {member: now})
    await redis.zremrangebyscore(key, "-inf", now - settings.metrics_window)
    await redis.expire(key, settings.metrics_window * 2)


class LiveState:
    """Tracks this worker's in-flight tasks and writes a throttled aggregate
    snapshot to llm:worker:{id}, so the dashboard sees live activity (tokens/sec,
    time-to-first-token, how many requests in flight) without waiting for a task
    to finish. One instance per worker process; safe under asyncio concurrency."""

    def __init__(self, redis, key: str):
        self.redis = redis
        self.key = key
        # task_id -> {"start": monotonic, "tokens": int, "ttft_ms": float | None}
        self.inflight: dict[str, dict] = {}
        self._last_write = 0.0

    def start(self, task_id: str) -> None:
        self.inflight[task_id] = {"start": time.monotonic(), "tokens": 0, "ttft_ms": None}

    def token(self, task_id: str) -> None:
        st = self.inflight.get(task_id)
        if st is None:
            return
        st["tokens"] += 1
        if st["ttft_ms"] is None:
            st["ttft_ms"] = (time.monotonic() - st["start"]) * 1000

    def stats(self, task_id: str) -> dict:
        """Snapshot for a single task (read before finish() to build the
        task_completed event)."""
        st = self.inflight.get(task_id, {})
        return {"tokens": st.get("tokens", 0), "ttft_ms": st.get("ttft_ms") or 0.0}

    def finish(self, task_id: str) -> None:
        self.inflight.pop(task_id, None)

    async def flush(self, force: bool = False) -> None:
        now = time.monotonic()
        if not force and (now - self._last_write) * 1000 < settings.live_update_ms:
            return
        self._last_write = now

        live_tok_s = 0.0
        ttft_ms = 0.0
        last_task = ""
        for task_id, st in self.inflight.items():
            elapsed = max(now - st["start"], 1e-3)
            live_tok_s += st["tokens"] / elapsed
            if st["ttft_ms"]:
                ttft_ms = st["ttft_ms"]
            last_task = task_id

        await self.redis.hset(self.key, mapping={
            "state":      "busy" if self.inflight else "idle",
            "inflight":   str(len(self.inflight)),
            "live_tok_s": f"{live_tok_s:.2f}",
            "ttft_ms":    f"{ttft_ms:.0f}",
            "last_task":  last_task,
            "last_seen":  str(int(time.time())),
        })
        await self.redis.expire(self.key, settings.worker_ttl)
