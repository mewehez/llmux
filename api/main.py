import asyncio
import json
import logging
import os
import uuid
from typing import AsyncGenerator

import redis.asyncio as aioredis
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="LLM Server API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Config ─────────────────────────────────────────────────────────────────

REDIS_URL       = os.getenv("REDIS_URL", "redis://localhost:6379/0")
STREAM_135M     = os.getenv("STREAM_135M", "llm:work:135m")
STREAM_360M     = os.getenv("STREAM_360M", "llm:work:360m")
RESULT_PREFIX   = "llm:result:"
TASK_TTL        = 3600
SSE_TIMEOUT     = 120

STREAM_MAP = {
    "135m": STREAM_135M,
    "360m": STREAM_360M,
}

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
    model:   str = "135m"   # "135m" or "360m"
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
        maxlen=1000,
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


@app.post("/chat", response_model=TaskCreated)
async def chat(request: ChatRequest):
    session_id = request.session_id or str(uuid.uuid4())
    try:
        task_id = await enqueue(request.message, request.model, session_id)
    except ValueError as exc:
        raise HTTPException(400, str(exc))

    return TaskCreated(
        task_id    = task_id,
        session_id = session_id,
        model      = request.model,
        stream     = STREAM_MAP[request.model],
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

        try:
            # First replay any tokens already in the result stream
            existing = await redis.xrange(result_stream)
            for _, fields in existing:
                payload = {
                    k.decode() if isinstance(k, bytes) else k:
                    v.decode() if isinstance(v, bytes) else v
                    for k, v in fields.items()
                }
                yield f"data: {json.dumps(payload)}\n\n"
                if payload.get("type") in ("done", "error"):
                    return

            # Then listen on pub/sub for tokens still in flight
            silence = 0.0
            poll    = 0.05

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


@app.get("/workers/status")
async def workers_status():
    redis = await get_redis()

    statuses = []
    for model, stream in STREAM_MAP.items():
        # Counts only entries not yet delivered to the consumer group
        try:
            group_info = await redis.xinfo_groups(stream)
            group_data = next(
                (g for g in group_info if g["name"].decode() == group),
                None,
            )
            queue_depth = group_data["lag"] if group_data else 0
        except Exception:
            queue_depth = 0

        # Pending entries — consumed but not yet ACKed
        group = f"llm-workers-{model}"
        try:
            pending = await redis.xpending(stream, group)
            pending_count = pending["pending"]
        except Exception:
            pending_count = 0

        # Last entry delivered to the consumer group
        try:
            group_info = await redis.xinfo_groups(stream)
            group_data = next(
                (g for g in group_info if g["name"].decode() == group),
                None,
            )
            last_delivered_id = group_data["last-delivered-id"].decode() if group_data else None
            if last_delivered_id and last_delivered_id != "0-0":
                last_entries = await redis.xrange(
                    stream,
                    min=last_delivered_id,
                    max=last_delivered_id,
                )
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

        statuses.append({
            "model":         model,
            "stream":        stream,
            "queue_depth":   queue_depth,
            "pending":       pending_count,
            "last_task":     last_task,
            "llm_url":       f"http://llm-{model}:8080",
        })

    return {"workers": statuses}
