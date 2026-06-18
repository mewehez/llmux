"""Benchmark runner service (Phase 3 follow-up).

Consumes benchmark specs from the `llm:bench:work` Redis stream (enqueued by the
API's POST /benchmark, i.e. the dashboard "Run benchmark" button) and runs them
via benchmark.run_benchmark, which writes results to `llm:bench:{run_id}`. Reuses
the worker image; selected by overriding the entrypoint.
"""

import asyncio
import logging
import signal

import redis.asyncio as aioredis

from benchmark import run_benchmark
from settings import load_models, settings

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

WORK_STREAM = "llm:bench:work"
GROUP = "bench-runners"
CONSUMER = settings.worker_id


async def ensure_group(redis: aioredis.Redis) -> None:
    try:
        await redis.xgroup_create(WORK_STREAM, GROUP, id="0", mkstream=True)
        logger.info("Consumer group '%s' created on '%s'", GROUP, WORK_STREAM)
    except Exception as exc:
        if "BUSYGROUP" not in str(exc):
            raise


async def handle(redis: aioredis.Redis, fields: dict) -> None:
    spec = {
        (k.decode() if isinstance(k, bytes) else k):
        (v.decode() if isinstance(v, bytes) else v)
        for k, v in fields.items()
    }
    model = spec.get("model", "135m")
    entry = next((m for m in load_models(settings.models_config_path) if m.id == model), None)
    runner_type = spec.get("runner") or (entry.runner if entry else "llamacpp")
    llm_url     = spec.get("llm_url") or (entry.llm_url if entry else settings.llm_url)
    llm_model   = spec.get("llm_model") or settings.llm_model or "local-model"
    count       = int(spec.get("count", 20))
    concurrency = int(spec.get("concurrency", 4))
    max_tokens  = int(spec.get("max_tokens", settings.max_tokens))

    logger.info("Running benchmark: model=%s runner=%s url=%s count=%d c=%d",
                model, runner_type, llm_url, count, concurrency)
    result = await run_benchmark(
        model_id=model, runner_type=runner_type, llm_url=llm_url, llm_model=llm_model,
        count=count, concurrency=concurrency, max_tokens=max_tokens, redis_url=settings.redis_url,
    )
    logger.info("Benchmark %s done: throughput=%s tok/s", result["run_id"], result["throughput_tok_s"])


async def main() -> None:
    redis = await aioredis.from_url(settings.redis_url, decode_responses=False)
    await ensure_group(redis)
    logger.info("[%s] Benchmark runner started on '%s'", CONSUMER, WORK_STREAM)

    shutdown = asyncio.Event()
    loop = asyncio.get_event_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, shutdown.set)

    while not shutdown.is_set():
        try:
            results = await redis.xreadgroup(GROUP, CONSUMER, {WORK_STREAM: ">"}, count=1, block=2000)
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
                try:
                    await handle(redis, fields)
                except Exception as exc:
                    logger.error("benchmark failed: %s", exc)
                finally:
                    await redis.xack(WORK_STREAM, GROUP, msg_id)

    logger.info("[%s] Benchmark runner shut down", CONSUMER)


if __name__ == "__main__":
    asyncio.run(main())
