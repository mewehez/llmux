"""
Slots sidecar — runs alongside a llama.cpp server (same pod / same host) and
polls its /slots endpoint, publishing per-slot utilisation to Redis so the API
can surface "requests in flight" at the model level.

llama.cpp's /slots returns a JSON array, one object per --parallel slot, each
with an `is_processing` boolean. We count busy vs total. If --metrics is enabled
we also read `llamacpp:requests_deferred` (requests waiting for a free slot).

Writes Redis hash  llm:slots:{model}:{pod}  with a short TTL so a dead pod's
slot data disappears automatically. The API aggregates across pods per model.
"""

import asyncio
import logging
import time

import httpx
import redis.asyncio as aioredis

from settings import settings

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Config is loaded from environment / .env via pydantic (see settings.py).
REDIS_URL    = settings.redis_url
LLM_URL      = settings.llm_url
MODEL_NAME   = settings.model_name
POD_NAME     = settings.pod_name
POLL_MS      = settings.poll_ms
SLOTS_TTL    = settings.slots_ttl
HTTP_TIMEOUT = settings.slots_http_timeout  # seconds per /slots and /metrics poll

KEY = f"llm:slots:{MODEL_NAME}:{POD_NAME}"


async def parse_deferred(client: httpx.AsyncClient) -> int:
    """Read llamacpp:requests_deferred from the Prometheus /metrics endpoint.
    Returns 0 if --metrics is disabled or the metric is absent."""
    try:
        resp = await client.get(f"{LLM_URL}/metrics", timeout=HTTP_TIMEOUT)
        if resp.status_code != 200:
            return 0
        for line in resp.text.splitlines():
            if line.startswith("llamacpp:requests_deferred"):
                return int(float(line.split()[-1]))
    except Exception:
        pass
    return 0


async def poll_once(client: httpx.AsyncClient, redis: aioredis.Redis) -> None:
    resp = await client.get(f"{LLM_URL}/slots", timeout=HTTP_TIMEOUT)
    resp.raise_for_status()
    slots = resp.json()
    if not isinstance(slots, list):
        raise ValueError(f"unexpected /slots payload: {type(slots)}")

    total = len(slots)
    processing = sum(1 for s in slots if s.get("is_processing"))
    deferred = await parse_deferred(client)

    await redis.hset(KEY, mapping={
        "model":      MODEL_NAME,
        "pod":        POD_NAME,
        "total":      str(total),
        "processing": str(processing),
        "deferred":   str(deferred),
        "last_seen":  str(int(time.time())),
    })
    await redis.expire(KEY, SLOTS_TTL)


async def main() -> None:
    redis = await aioredis.from_url(REDIS_URL, decode_responses=False)
    logger.info("[slots] sidecar for model %s polling %s/slots → %s", MODEL_NAME, LLM_URL, KEY)

    async with httpx.AsyncClient() as client:
        while True:
            try:
                await poll_once(client, redis)
            except Exception as exc:
                logger.warning("[slots] poll failed: %s", exc)
            await asyncio.sleep(POLL_MS / 1000)


if __name__ == "__main__":
    asyncio.run(main())
