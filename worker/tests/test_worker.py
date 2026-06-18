"""Worker resilience: attempt tracking, ACK vs retry vs dead-letter, token
fan-out, and crash-recovery reclaim. fakeredis stands in for Redis; the LLM
call (stream_inference / runner) is monkeypatched so no backend is needed."""
import fakeredis.aioredis as fr
import pytest

import main
from telemetry import LiveState


def _r():
    return fr.FakeRedis(decode_responses=False)


async def _deliver(r, fields: dict):
    """xadd a work item and read it into this worker's PEL; return (msg_id, fields)."""
    try:
        await r.xgroup_create(main.STREAM, main.CONSUMER_GROUP, id="0", mkstream=True)
    except Exception:
        pass
    await r.xadd(main.STREAM, fields)
    res = await r.xreadgroup(main.CONSUMER_GROUP, main.WORKER_ID, {main.STREAM: ">"}, count=10)
    return res[0][1][-1]  # (msg_id, fields)


async def _pending(r) -> int:
    info = await r.xpending(main.STREAM, main.CONSUMER_GROUP)
    return info["pending"] if isinstance(info, dict) else info[0]


async def _event_types(r) -> list[bytes]:
    return [f[b"type"] for _id, f in await r.xrange("llm:events")]


# ── happy path ────────────────────────────────────────────────────────────────

async def test_process_success_acks_and_records(monkeypatch):
    r = _r()

    async def fake_infer(message, task_id, redis, live):
        live.token(task_id)
        live.token(task_id)
        return {"decode_tok_s": 12.0}

    monkeypatch.setattr(main, "stream_inference", fake_infer)
    live = LiveState(r, main.METRICS_KEY)
    mid, fields = await _deliver(r, {"task_id": "t1", "message": "hi", "model": "135m"})

    await main.process(r, mid, fields, live)

    assert await r.hget("llm:task:t1", "status") == b"done"
    assert await _pending(r) == 0                       # ACKed
    metrics = await r.hgetall(main.METRICS_KEY)
    assert metrics[b"tokens_per_sec"] == b"12.0"        # from runner decode rate
    assert int(metrics[b"total_reqs"]) == 1
    assert b"task_completed" in await _event_types(r)
    assert await r.zcard(f"llm:ts:completions:{main.MODEL_NAME}") == 1


# ── retryable failure ─────────────────────────────────────────────────────────

async def test_process_retryable_failure_leaves_in_pel(monkeypatch):
    r = _r()

    async def boom(*a, **k):
        raise RuntimeError("transient")

    monkeypatch.setattr(main, "stream_inference", boom)
    live = LiveState(r, main.METRICS_KEY)
    mid, fields = await _deliver(r, {"task_id": "t2", "message": "x", "model": "135m"})

    await main.process(r, mid, fields, live)

    assert await r.hget("llm:task:t2", "status") == b"error"
    assert await _pending(r) == 1                       # NOT ACKed → reclaim will retry
    metrics = await r.hgetall(main.METRICS_KEY)
    assert int(metrics[b"errors"]) == 1
    # task_error event carries the exception type
    errs = [f for _id, f in await r.xrange("llm:events") if f[b"type"] == b"task_error"]
    assert errs and b"RuntimeError" in errs[-1][b"error"]


# ── dead-letter paths ─────────────────────────────────────────────────────────

async def test_process_dead_letters_when_attempts_exceeded(monkeypatch):
    r = _r()
    # Pre-seed attempts at the max so this delivery's hincrby pushes it OVER.
    await r.hset("llm:task:t3", "attempts", main.MAX_ATTEMPTS)

    called = {"infer": False}

    async def must_not_run(*a, **k):
        called["infer"] = True
        return {}

    monkeypatch.setattr(main, "stream_inference", must_not_run)
    live = LiveState(r, main.METRICS_KEY)
    mid, fields = await _deliver(r, {"task_id": "t3", "message": "x", "model": "135m"})

    await main.process(r, mid, fields, live)

    assert called["infer"] is False                     # never attempted inference
    assert await r.hget("llm:task:t3", "status") == b"dead"
    assert len(await r.xrange(main.DEAD_STREAM)) == 1
    assert await _pending(r) == 0                        # ACKed (not redelivered)
    assert b"task_dead" in await _event_types(r)


async def test_process_dead_letters_on_final_attempt_failure(monkeypatch):
    r = _r()
    await r.hset("llm:task:t4", "attempts", main.MAX_ATTEMPTS - 1)  # this run == MAX

    async def boom(*a, **k):
        raise RuntimeError("still failing")

    monkeypatch.setattr(main, "stream_inference", boom)
    live = LiveState(r, main.METRICS_KEY)
    mid, fields = await _deliver(r, {"task_id": "t4", "message": "x", "model": "135m"})

    await main.process(r, mid, fields, live)

    assert await r.hget("llm:task:t4", "status") == b"dead"
    assert len(await r.xrange(main.DEAD_STREAM)) == 1
    assert await _pending(r) == 0


async def test_dead_letter_writes_stream_and_marks_status():
    r = _r()
    await main.dead_letter(r, "tx", {"model": "135m", "message": "boom"}, reason="poison")
    dead = await r.xrange(main.DEAD_STREAM)
    assert len(dead) == 1
    _id, f = dead[0]
    assert f[b"task_id"] == b"tx"
    assert f[b"reason"] == b"poison"
    assert await r.hget("llm:task:tx", "status") == b"dead"
    assert b"task_dead" in await _event_types(r)


# ── token fan-out (stream_inference) ──────────────────────────────────────────

async def test_stream_inference_fans_out_tokens_and_done(monkeypatch):
    r = _r()

    class FakeRunner:
        async def generate(self, client, message, *, max_tokens, model, on_token):
            await on_token("a")
            await on_token("b")
            return {"decode_tok_s": 5.0}

    monkeypatch.setattr(main, "runner", FakeRunner())
    monkeypatch.setattr(main, "get_http_client", lambda: None)
    live = LiveState(r, main.METRICS_KEY)
    live.start("tk")

    timings = await main.stream_inference("hello", "tk", r, live)

    assert timings == {"decode_tok_s": 5.0}
    entries = await r.xrange("llm:result:stream:tk")
    tokens = [f for _id, f in entries if f.get(b"type") == b"token"]
    assert [t[b"content"] for t in tokens] == [b"a", b"b"]
    assert [t[b"seq"] for t in tokens] == [b"1", b"2"]      # monotonic seq for dedup
    assert any(f.get(b"type") == b"done" for _id, f in entries)


async def test_stream_inference_emits_error_event_and_reraises(monkeypatch):
    r = _r()

    class BadRunner:
        async def generate(self, *a, **k):
            raise RuntimeError("backend down")

    monkeypatch.setattr(main, "runner", BadRunner())
    monkeypatch.setattr(main, "get_http_client", lambda: None)
    live = LiveState(r, main.METRICS_KEY)
    live.start("tk")

    with pytest.raises(RuntimeError, match="backend down"):
        await main.stream_inference("hi", "tk", r, live)

    errs = [f for _id, f in await r.xrange("llm:result:stream:tk") if f.get(b"type") == b"error"]
    assert errs and b"RuntimeError" in errs[-1][b"content"]


# ── crash recovery (reclaim → process) ────────────────────────────────────────

async def test_reclaim_reprocesses_orphaned_pending_entry(monkeypatch):
    r = _r()
    await r.xgroup_create(main.STREAM, main.CONSUMER_GROUP, id="0", mkstream=True)
    await r.xadd(main.STREAM, {"task_id": "tr", "message": "x", "model": "135m"})
    # A now-dead consumer took it but never ACKed.
    await r.xreadgroup(main.CONSUMER_GROUP, "crashed-consumer", {main.STREAM: ">"}, count=10)

    async def fake_infer(message, task_id, redis, live):
        return {"decode_tok_s": 1.0}

    monkeypatch.setattr(main, "stream_inference", fake_infer)
    live = LiveState(r, main.METRICS_KEY)

    _cursor, claimed, *_ = await r.xautoclaim(
        main.STREAM, main.CONSUMER_GROUP, main.WORKER_ID,
        min_idle_time=0, start_id="0-0", count=10,
    )
    for mid, fields in claimed:
        await main.process(r, mid, fields, live)

    assert await r.hget("llm:task:tr", "status") == b"done"
    assert await _pending(r) == 0
