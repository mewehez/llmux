"""API helpers + Redis-backed flows (enqueue, scale reconciler) via fakeredis.
Functions are called directly — no HTTP layer / lifespan needed."""
import time

import fakeredis.aioredis as fr
import pytest

import main


def _r():
    return fr.FakeRedis(decode_responses=False)


# ── pure helpers ──────────────────────────────────────────────────────────────

def test_percentile_empty_is_zero():
    assert main._percentile([], 0.5) == 0.0


def test_percentile_endpoints_and_interpolation():
    vals = [10.0, 20.0, 30.0, 40.0]
    assert main._percentile(vals, 0.0) == 10.0
    assert main._percentile(vals, 1.0) == 40.0
    assert main._percentile(vals, 0.5) == 25.0          # interp between 20 and 30


def test_percentile_single_element():
    assert main._percentile([42.0], 0.95) == 42.0


def test_event_from_entry_decodes_bytes_and_adds_id():
    ev = main._event_from_entry(b"1-0", {b"type": b"task_started", b"model": b"135m"})
    assert ev == {"type": "task_started", "model": "135m", "id": "1-0"}


def test_event_from_entry_accepts_str_input():
    ev = main._event_from_entry("5-0", {"type": "x"})
    assert ev["id"] == "5-0"
    assert ev["type"] == "x"


# ── config endpoint (registry exposure) ───────────────────────────────────────

async def test_config_exposes_only_enabled_models():
    cfg = await main.config()
    ids = {m["id"] for m in cfg["models"]}
    assert {"135m", "360m"} <= ids
    assert "qwen3-8b" not in ids
    assert "qwen3-32b" not in ids
    assert set(cfg["models"][0]) == {"id", "label", "runner", "maxReplicas", "maxLlmPods"}


# ── enqueue ───────────────────────────────────────────────────────────────────

async def test_enqueue_writes_work_stream_and_task_hash():
    r = _r()
    main._redis = r
    try:
        task_id = await main.enqueue("hello", "135m", "sess-1")
        entries = await r.xrange("llm:work:135m")
        assert len(entries) == 1
        _id, fields = entries[0]
        assert fields[b"message"] == b"hello"
        assert fields[b"model"] == b"135m"
        assert fields[b"task_id"] == task_id.encode()
        meta = await r.hgetall(f"llm:task:{task_id}")
        assert meta[b"status"] == b"queued"
        assert meta[b"model"] == b"135m"
    finally:
        main._redis = None


async def test_enqueue_rejects_unknown_model():
    r = _r()
    main._redis = r
    try:
        with pytest.raises(ValueError, match="Unknown model"):
            await main.enqueue("hi", "does-not-exist", "s")
    finally:
        main._redis = None


# ── scale reconciler (real scale_up/scale_down events) ────────────────────────

async def test_reconcile_first_pass_sets_baseline_without_events():
    r = _r()
    emitted = await main.reconcile_scale_once(r)
    assert emitted == []                                # first observation = baseline
    assert await r.hgetall("llm:scale:state")           # state recorded
    assert await r.get("llm:scale:count") is None       # no scale event counted


async def test_reconcile_emits_scale_up_when_a_worker_appears():
    r = _r()
    await main.reconcile_scale_once(r)                  # baseline: 0 workers
    # A healthy worker for 135m registers.
    await r.hset("llm:worker:w1", mapping={"model": "135m", "last_seen": str(int(time.time()))})

    emitted = await main.reconcile_scale_once(r)

    assert ("scale_up", "135m", "workers", 0, 1) in emitted
    assert int(await r.get("llm:scale:count")) == 1
    types = [f[b"type"] for _id, f in await r.xrange("llm:events")]
    assert b"scale_up" in types


async def test_reconcile_emits_scale_down_when_worker_disappears():
    r = _r()
    await r.hset("llm:worker:w1", mapping={"model": "360m", "last_seen": str(int(time.time()))})
    await main.reconcile_scale_once(r)                  # baseline: 1 worker
    await r.delete("llm:worker:w1")                     # worker gone

    emitted = await main.reconcile_scale_once(r)

    assert ("scale_down", "360m", "workers", 1, 0) in emitted
