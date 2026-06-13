import asyncio
import json
import logging
import os
import signal
import socket

import httpx
import redis.asyncio as aioredis

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ── Config ──────────────────────────────────────────────────────────────────

REDIS_URL      = os.getenv("REDIS_URL",      "redis://localhost:6379/0")
STREAM         = os.getenv("STREAM",         "llm:work:135m")
LLM_URL        = os.getenv("LLM_URL",        "http://localhost:8001")
CONSUMER_GROUP = os.getenv("CONSUMER_GROUP", "llm-workers")
WORKER_ID      = os.getenv("WORKER_ID",      socket.gethostname())
BLOCK_MS       = int(os.getenv("BLOCK_MS",   "2000"))
RESULT_PREFIX  = "llm:result:"
RESULT_STREAM_PREFIX = "llm:result:stream:"

# ── Redis ────────────────────────────────────────────────────────────────────

_redis: aioredis.Redis | None = None


async def get_redis() -> aioredis.Redis:
    global _redis
    if _redis is None:
        _redis = await aioredis.from_url(REDIS_URL, decode_responses=False)
    return _redis


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

async def stream_inference(message: str, task_id: str, redis: aioredis.Redis) -> None:
    """
    Send message to llama.cpp, stream tokens to pub/sub as they arrive.
    Publishes: token events, then a final done or error event.
    """
    channel = f"{RESULT_PREFIX}{task_id}"
    result_stream = f"{RESULT_STREAM_PREFIX}{task_id}"

    payload = {
        "model":    "local-model",
        "messages": [{"role": "user", "content": message}],
        "max_tokens": 512,
        "stream":   True,
    }

    try:
        async with httpx.AsyncClient(timeout=60) as client:
            async with client.stream(
                "POST",
                f"{LLM_URL}/v1/chat/completions",
                json=payload,
            ) as response:

                if response.status_code != 200:
                    error = await response.aread()
                    raise RuntimeError(f"LLM returned {response.status_code}: {error}")

                async for line in response.aiter_lines():
                    if not line.startswith("data:"):
                        continue

                    data = line[5:].strip()

                    if data == "[DONE]":
                        break

                    try:
                        chunk = json.loads(data)
                    except json.JSONDecodeError:
                        continue

                    delta = (
                        chunk
                        .get("choices", [{}])[0]
                        .get("delta", {})
                        .get("content", "")
                    )

                    if delta:
                        await redis.xadd(
                            result_stream,
                            {"type": "token", "content": delta},
                            maxlen=1000,
                        )
                        await redis.publish(
                            channel,
                            json.dumps({"type": "token", "content": delta}),
                        )

        await redis.xadd(result_stream, {"type": "done", "task_id": task_id})
        await redis.publish(
            channel,
            json.dumps({"type": "done", "task_id": task_id}),
        )
        logger.info("Task %s completed", task_id)
        # Set TTL on the result stream
        await redis.expire(result_stream, 300)  # 5 minutes

    except Exception as exc:
        logger.error("Task %s failed: %s", task_id, exc)
        await redis.xadd(result_stream, {"type": "error", "content": str(exc), "task_id": task_id})
        await redis.publish(
            channel,
            json.dumps({"type": "error", "content": str(exc), "task_id": task_id}),
        )
        # Set TTL on the result stream
        await redis.expire(result_stream, 300)  # 5 minutes
        raise


# ── Process one stream entry ──────────────────────────────────────────────────

async def process(redis: aioredis.Redis, msg_id: bytes, fields: dict) -> None:
    decoded = {
        (k.decode() if isinstance(k, bytes) else k):
        (v.decode() if isinstance(v, bytes) else v)
        for k, v in fields.items()
    }

    task_id = decoded.get("task_id", "unknown")
    message = decoded.get("message", "")
    logger.info("[%s] Processing task %s", WORKER_ID, task_id)

    await redis.hset(f"llm:task:{task_id}", "status", "running")

    try:
        await stream_inference(message, task_id, redis)
        await redis.hset(f"llm:task:{task_id}", "status", "done")
    except Exception:
        await redis.hset(f"llm:task:{task_id}", "status", "error")
    finally:
        await redis.xack(STREAM, CONSUMER_GROUP, msg_id)
        logger.info("[%s] ACKed task %s", WORKER_ID, task_id)


# ── Main loop ─────────────────────────────────────────────────────────────────

async def consume_loop() -> None:
    redis    = await get_redis()
    await ensure_group(redis)
    logger.info("[%s] Worker started on stream '%s' → %s", WORKER_ID, STREAM, LLM_URL)

    shutdown = asyncio.Event()

    def handle_signal(*_):
        logger.info("[%s] Shutdown signal received", WORKER_ID)
        shutdown.set()

    loop = asyncio.get_event_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, handle_signal)

    while not shutdown.is_set():
        try:
            results = await redis.xreadgroup(
                groupname    = CONSUMER_GROUP,
                consumername = WORKER_ID,
                streams      = {STREAM: ">"},
                count        = 1,
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
                await process(redis, msg_id, fields)

    logger.info("[%s] Worker shut down cleanly", WORKER_ID)


if __name__ == "__main__":
    asyncio.run(consume_loop())
