"""OpenAI-compatible chat-completions runners: llama.cpp server and vLLM.

Both expose POST /v1/chat/completions with SSE ("data: {json}") streaming and
`choices[0].delta.content` token deltas. They differ in the timing fields they
report and in small request-shaping details, captured by subclasses.
"""

import json

import httpx

from .base import OnToken, Runner, Timings


class OpenAIChatRunner(Runner):
    chat_path = "/v1/chat/completions"

    def extra_payload(self) -> dict:
        """Backend-specific additions to the request body."""
        return {}

    def parse_timings(self, chunk: dict, timings: Timings) -> None:
        """Pull whatever timing/usage fields this backend emits into `timings`."""
        usage = chunk.get("usage")
        if usage:
            if usage.get("prompt_tokens") is not None:
                timings["prompt_tokens"] = usage["prompt_tokens"]
            if usage.get("completion_tokens") is not None:
                timings["completion_tokens"] = usage["completion_tokens"]

    async def generate(
        self, client: httpx.AsyncClient, message: str, *,
        max_tokens: int, model: str, on_token: OnToken,
    ) -> Timings:
        payload = {
            "model":      model,
            "messages":   [{"role": "user", "content": message}],
            "max_tokens": max_tokens,
            "stream":     True,
            **self.extra_payload(),
        }
        timings: Timings = {}

        async with client.stream("POST", f"{self.llm_url}{self.chat_path}", json=payload) as response:
            if response.status_code != 200:
                error = await response.aread()
                raise RuntimeError(f"{self.name} returned {response.status_code}: {error!r}")

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
                    chunk.get("choices", [{}])[0]
                    .get("delta", {})
                    .get("content", "")
                )
                if delta:
                    await on_token(delta)

                self.parse_timings(chunk, timings)

        return timings


class LlamaCppRunner(OpenAIChatRunner):
    """llama.cpp server. Emits a rich `timings` object per chunk including
    `predicted_per_second` (decode rate) and token counts."""
    name = "llamacpp"
    capabilities = {"slots": True, "metrics": True}

    def parse_timings(self, chunk: dict, timings: Timings) -> None:
        super().parse_timings(chunk, timings)
        t = chunk.get("timings")
        if t:
            if t.get("predicted_per_second") is not None:
                timings["decode_tok_s"] = float(t["predicted_per_second"])
            if t.get("predicted_n") is not None:
                timings["completion_tokens"] = int(t["predicted_n"])
            if t.get("prompt_n") is not None:
                timings["prompt_tokens"] = int(t["prompt_n"])
            # llama.cpp reports prompt_ms (prefill) — a decent TTFT proxy.
            if t.get("prompt_ms") is not None:
                timings["ttft_ms"] = float(t["prompt_ms"])


class VllmRunner(OpenAIChatRunner):
    """vLLM. OpenAI-compatible; needs stream_options to emit usage while
    streaming. Exposes `vllm:num_requests_waiting` on /metrics (used by KEDA)."""
    name = "vllm"
    capabilities = {"metrics": True, "waiting_metric": "vllm:num_requests_waiting"}

    def extra_payload(self) -> dict:
        return {"stream_options": {"include_usage": True}}
