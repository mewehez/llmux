"""Runner selection + backend streaming/parse, driven by httpx.MockTransport
(no live llama.cpp / Ollama / vLLM needed)."""
import json

import httpx
import pytest

from runners import LlamaCppRunner, OllamaRunner, VllmRunner, get_runner


# ── selection ────────────────────────────────────────────────────────────────

def test_get_runner_selects_by_type():
    assert isinstance(get_runner("llamacpp", "http://x"), LlamaCppRunner)
    assert isinstance(get_runner("vllm", "http://x"), VllmRunner)
    assert isinstance(get_runner("ollama", "http://x"), OllamaRunner)


def test_get_runner_defaults_to_llamacpp_for_unknown_or_empty():
    assert isinstance(get_runner("", "http://x"), LlamaCppRunner)
    assert isinstance(get_runner("nope", "http://x"), LlamaCppRunner)
    assert isinstance(get_runner(None, "http://x"), LlamaCppRunner)


def test_runner_normalizes_url():
    r = get_runner("llamacpp", "http://llm-135m:8080/")
    assert r.llm_url == "http://llm-135m:8080"
    assert r.health_url() == "http://llm-135m:8080/health"


def test_vllm_requests_usage_in_stream():
    assert get_runner("vllm", "http://x").extra_payload() == {
        "stream_options": {"include_usage": True}
    }


def test_ollama_health_uses_api_tags():
    assert get_runner("ollama", "http://x").health_url() == "http://x/api/tags"


# ── streaming + parse ────────────────────────────────────────────────────────

def _sse(*chunks: dict) -> bytes:
    lines = []
    for c in chunks:
        lines.append(f"data: {json.dumps(c)}")
        lines.append("")  # blank line between SSE events
    lines.append("data: [DONE]")
    return ("\n".join(lines) + "\n").encode()


async def _run(runner, transport):
    tokens: list[str] = []

    async def on_token(t):
        tokens.append(t)

    async with httpx.AsyncClient(transport=transport) as client:
        timings = await runner.generate(
            client, "hi", max_tokens=16, model="local", on_token=on_token
        )
    return tokens, timings


async def test_llamacpp_streams_tokens_and_parses_timings():
    captured = {}

    def handler(request):
        captured["payload"] = json.loads(request.content)
        return httpx.Response(200, content=_sse(
            {"choices": [{"delta": {"content": "Hel"}}]},
            {"choices": [{"delta": {"content": "lo"}}]},
            {"choices": [{"delta": {}}],
             "timings": {"predicted_per_second": 42.0, "predicted_n": 2,
                         "prompt_n": 5, "prompt_ms": 12.0}},
        ))

    tokens, timings = await _run(get_runner("llamacpp", "http://llm"),
                                 httpx.MockTransport(handler))

    assert "".join(tokens) == "Hello"
    # request shaped correctly
    assert captured["payload"]["stream"] is True
    assert captured["payload"]["max_tokens"] == 16
    assert captured["payload"]["messages"] == [{"role": "user", "content": "hi"}]
    # timings normalized
    assert timings["decode_tok_s"] == 42.0
    assert timings["completion_tokens"] == 2
    assert timings["prompt_tokens"] == 5
    assert timings["ttft_ms"] == 12.0


async def test_llamacpp_ignores_malformed_chunks():
    def handler(request):
        body = (
            "data: {not json}\n\n"
            'data: {"choices": [{"delta": {"content": "ok"}}]}\n\n'
            "data: [DONE]\n"
        ).encode()
        return httpx.Response(200, content=body)

    tokens, _ = await _run(get_runner("llamacpp", "http://llm"),
                           httpx.MockTransport(handler))
    assert "".join(tokens) == "ok"


async def test_ollama_streams_ndjson_and_parses_timings():
    captured = {}

    def handler(request):
        captured["payload"] = json.loads(request.content)
        lines = [
            json.dumps({"message": {"content": "Hi"}, "done": False}),
            json.dumps({"message": {"content": " there"}, "done": False}),
            json.dumps({"message": {"content": ""}, "done": True,
                        "eval_count": 3, "eval_duration": 1_000_000_000,
                        "prompt_eval_count": 4, "prompt_eval_duration": 2_000_000}),
        ]
        return httpx.Response(200, content=("\n".join(lines) + "\n").encode())

    tokens, timings = await _run(get_runner("ollama", "http://llm"),
                                 httpx.MockTransport(handler))

    assert "".join(tokens) == "Hi there"
    assert captured["payload"]["options"] == {"num_predict": 16}
    assert timings["decode_tok_s"] == 3.0          # 3 tokens / 1.0s
    assert timings["completion_tokens"] == 3
    assert timings["prompt_tokens"] == 4
    assert timings["ttft_ms"] == 2.0               # 2_000_000 ns → ms


@pytest.mark.parametrize("runner_type", ["llamacpp", "ollama"])
async def test_runner_raises_on_non_200(runner_type):
    def handler(request):
        return httpx.Response(503, content=b"overloaded")

    async def on_token(_):
        raise AssertionError("should not be called on error")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(RuntimeError, match="503"):
            await get_runner(runner_type, "http://llm").generate(
                client, "hi", max_tokens=4, model="m", on_token=on_token
            )
