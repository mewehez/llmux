"""Benchmark harness (Phase 3) — compare inference runners fairly.

Fires a fixed workload (N requests at concurrency C, a shared prompt set) at a
model's runner *directly* (bypassing the work queue, to isolate runner
performance), measures normalized metrics — time-to-first-token, decode tok/s,
end-to-end latency, aggregate throughput under load — and writes a result to
Redis (`llm:bench:{run_id}`, indexed by `llm:bench:index`). The API exposes the
results read-only and the dashboard renders the comparison.

Run it as a CLI:

    uv run python benchmark.py --model 135m --count 20 --concurrency 4
    uv run python benchmark.py --model 360m --count 20 --concurrency 4 \
        --llm-url http://ollama:11434 --runner ollama --llm-model smollm2:360m

Point --llm-url / --runner / --llm-model at a different backend to benchmark
Ollama or vLLM against the same workload.
"""

import argparse
import asyncio
import json
import statistics
import time
import uuid

import httpx
import redis.asyncio as aioredis

from runners import get_runner
from settings import load_models, settings

PROMPTS = [
    "Count from 1 to 10.",
    "What is the capital of France?",
    "Write a haiku about rain.",
    "List three primary colors.",
    "Explain Kubernetes in one sentence.",
    "Name two programming languages.",
]


def _pct(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    if len(s) == 1:
        return s[0]
    k = (len(s) - 1) * p
    lo, hi = int(k), min(int(k) + 1, len(s) - 1)
    return s[lo] + (s[hi] - s[lo]) * (k - lo)


async def _one_request(runner, client, prompt, max_tokens, model) -> dict:
    start = time.monotonic()
    state = {"tokens": 0, "ttft_ms": None}

    async def on_token(_delta: str) -> None:
        state["tokens"] += 1
        if state["ttft_ms"] is None:
            state["ttft_ms"] = (time.monotonic() - start) * 1000

    timings = await runner.generate(client, prompt, max_tokens=max_tokens, model=model, on_token=on_token)
    latency_ms = (time.monotonic() - start) * 1000
    tokens = timings.get("completion_tokens") or state["tokens"]
    decode = timings.get("decode_tok_s") or (tokens / (latency_ms / 1000) if latency_ms > 0 else 0.0)
    return {
        "ttft_ms":      state["ttft_ms"] or 0.0,
        "latency_ms":   latency_ms,
        "tokens":       tokens,
        "decode_tok_s": decode,
    }


async def run_benchmark(
    *, model_id: str, runner_type: str, llm_url: str, llm_model: str,
    count: int, concurrency: int, max_tokens: int, redis_url: str,
) -> dict:
    runner = get_runner(runner_type, llm_url)
    sem = asyncio.Semaphore(concurrency)
    results: list[dict] = []
    errors = 0

    async with httpx.AsyncClient(timeout=settings.http_timeout) as client:
        async def worker(i: int) -> None:
            nonlocal errors
            prompt = PROMPTS[i % len(PROMPTS)]
            async with sem:
                try:
                    results.append(await _one_request(runner, client, prompt, max_tokens, llm_model))
                except Exception as exc:  # noqa: BLE001 — record, don't abort the run
                    errors += 1
                    print(f"  request {i} failed: {exc}")

        wall_start = time.monotonic()
        await asyncio.gather(*(worker(i) for i in range(count)))
        wall_s = time.monotonic() - wall_start

    ttfts   = [r["ttft_ms"] for r in results]
    lats    = [r["latency_ms"] for r in results]
    decodes = [r["decode_tok_s"] for r in results]
    total_tokens = sum(r["tokens"] for r in results)

    run_id = f"{int(time.time())}-{uuid.uuid4().hex[:8]}"
    result = {
        "run_id":            run_id,
        "ts":                int(time.time() * 1000),
        "model":             model_id,
        "runner":            runner_type,
        "llm_url":           llm_url,
        "llm_model":         llm_model,
        "count":             count,
        "completed":         len(results),
        "errors":            errors,
        "concurrency":       concurrency,
        "max_tokens":        max_tokens,
        "ttft_p50_ms":       round(_pct(ttfts, 0.50), 1),
        "ttft_p95_ms":       round(_pct(ttfts, 0.95), 1),
        "latency_p50_ms":    round(_pct(lats, 0.50), 1),
        "latency_p95_ms":    round(_pct(lats, 0.95), 1),
        "decode_tok_s_avg":  round(statistics.fmean(decodes), 2) if decodes else 0.0,
        "decode_tok_s_p50":  round(_pct(decodes, 0.50), 2),
        "throughput_tok_s":  round(total_tokens / wall_s, 2) if wall_s > 0 else 0.0,
        "total_tokens":      total_tokens,
        "wall_s":            round(wall_s, 2),
    }

    redis = await aioredis.from_url(redis_url, decode_responses=False)
    await redis.set(f"llm:bench:{run_id}", json.dumps(result), ex=settings.bench_ttl)
    await redis.lpush("llm:bench:index", run_id)
    await redis.ltrim("llm:bench:index", 0, settings.bench_index_max - 1)
    await redis.aclose()
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description="Benchmark an inference runner.")
    parser.add_argument("--model", default=settings.model_id, help="registry model id (e.g. 135m)")
    parser.add_argument("--count", type=int, default=20, help="total requests")
    parser.add_argument("--concurrency", type=int, default=4, help="concurrent requests")
    parser.add_argument("--max-tokens", type=int, default=settings.max_tokens)
    parser.add_argument("--runner", default=None, help="override runner type (llamacpp|ollama|vllm)")
    parser.add_argument("--llm-url", default=None, help="override backend URL")
    parser.add_argument("--llm-model", default=None, help="model name sent to the backend")
    parser.add_argument("--redis-url", default=settings.redis_url)
    args = parser.parse_args()

    entry = next((m for m in load_models(settings.models_config_path) if m.id == args.model), None)
    runner_type = args.runner or (entry.runner if entry else "llamacpp")
    llm_url     = args.llm_url or (entry.llm_url if entry else settings.llm_url)
    llm_model   = args.llm_model or settings.llm_model or "local-model"

    print(f"Benchmarking model={args.model} runner={runner_type} url={llm_url} "
          f"count={args.count} concurrency={args.concurrency} max_tokens={args.max_tokens}")
    result = asyncio.run(run_benchmark(
        model_id=args.model, runner_type=runner_type, llm_url=llm_url, llm_model=llm_model,
        count=args.count, concurrency=args.concurrency, max_tokens=args.max_tokens,
        redis_url=args.redis_url,
    ))
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
