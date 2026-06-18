"""Telemetry spine: event emission, completion samples, live-state tracking.
Uses fakeredis (async) — no live Redis."""
import time

import fakeredis.aioredis as fr

from telemetry import LiveState, emit_event, record_completion


def _r():
    return fr.FakeRedis(decode_responses=False)


# ── emit_event ────────────────────────────────────────────────────────────────

async def test_emit_event_writes_typed_entry_with_ts():
    r = _r()
    await emit_event(r, "task_started", task_id="t1", model="135m")
    entries = await r.xrange("llm:events")
    assert len(entries) == 1
    _id, f = entries[0]
    assert f[b"type"] == b"task_started"
    assert f[b"task_id"] == b"t1"
    assert f[b"model"] == b"135m"
    assert b"ts" in f and int(f[b"ts"]) > 0


async def test_emit_event_drops_none_fields_and_stringifies():
    r = _r()
    await emit_event(r, "task_completed", task_id="t1", tokens=42, ttft_ms=None)
    _id, f = (await r.xrange("llm:events"))[0]
    assert f[b"tokens"] == b"42"        # int → str
    assert b"ttft_ms" not in f          # None dropped


# ── record_completion ─────────────────────────────────────────────────────────

async def test_record_completion_encodes_sample():
    r = _r()
    await record_completion(r, "135m", latency_ms=123.4, tokens=10, tok_s=8.5, task_id="t1")
    members = await r.zrange("llm:ts:completions:135m", 0, -1)
    assert len(members) == 1
    parts = members[0].decode().split("|")
    # ts_ms | latency | tokens | tok_s | task_id
    assert parts[1] == "123.4"
    assert parts[2] == "10"
    assert parts[3] == "8.50"
    assert parts[4] == "t1"


async def test_record_completion_trims_samples_outside_window():
    r = _r()
    key = "llm:ts:completions:135m"
    # An old sample beyond the retention window.
    from settings import settings
    old_ts = time.time() - settings.metrics_window - 100
    await r.zadd(key, {"old|1.0|1|1.00|old": old_ts})
    await record_completion(r, "135m", latency_ms=5.0, tokens=2, tok_s=4.0, task_id="new")
    members = [m.decode() for m in await r.zrange(key, 0, -1)]
    assert all(not m.startswith("old|") for m in members)  # old one trimmed
    assert any(m.endswith("|new") for m in members)


# ── LiveState ─────────────────────────────────────────────────────────────────

async def test_livestate_tracks_tokens_and_ttft():
    r = _r()
    live = LiveState(r, "llm:worker:w1")
    live.start("t1")
    live.token("t1")
    live.token("t1")
    snap = live.stats("t1")
    assert snap["tokens"] == 2
    assert snap["ttft_ms"] > 0          # set on first token


async def test_livestate_token_for_unknown_task_is_noop():
    r = _r()
    live = LiveState(r, "llm:worker:w1")
    live.token("ghost")                 # no start() → ignored
    assert live.stats("ghost") == {"tokens": 0, "ttft_ms": 0.0}


async def test_livestate_flush_writes_busy_then_idle():
    r = _r()
    live = LiveState(r, "llm:worker:w1")
    live.start("t1")
    await live.flush(force=True)
    h = await r.hgetall("llm:worker:w1")
    assert h[b"state"] == b"busy"
    assert h[b"inflight"] == b"1"

    live.finish("t1")
    await live.flush(force=True)
    h = await r.hgetall("llm:worker:w1")
    assert h[b"state"] == b"idle"
    assert h[b"inflight"] == b"0"


async def test_livestate_flush_is_throttled_without_force():
    r = _r()
    live = LiveState(r, "llm:worker:w1")
    live.start("t1")
    await live.flush(force=True)        # sets _last_write = now
    live.start("t2")
    await live.flush()                  # throttled → no write
    h = await r.hgetall("llm:worker:w1")
    assert h[b"inflight"] == b"1"       # still the pre-throttle snapshot
