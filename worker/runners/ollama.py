"""Ollama runner.

Ollama's chat API (POST /api/chat) streams newline-delimited JSON objects
(not SSE). Each line: {"message": {"content": "..."}, "done": false}; the final
line has done=true plus eval_count / eval_duration (ns) for the decode rate and
prompt_eval_count / prompt_eval_duration for prefill.
"""

import json

import httpx

from .base import OnToken, Runner, Timings


class OllamaRunner(Runner):
    name = "ollama"
    capabilities = {"slots": False}

    def health_url(self) -> str:
        # Ollama has no /health; /api/tags returns 200 when up.
        return f"{self.llm_url}/api/tags"

    async def generate(
        self, client: httpx.AsyncClient, message: str, *,
        max_tokens: int, model: str, on_token: OnToken,
    ) -> Timings:
        payload = {
            "model":    model,
            "messages": [{"role": "user", "content": message}],
            "stream":   True,
            "options":  {"num_predict": max_tokens},
        }
        timings: Timings = {}

        async with client.stream("POST", f"{self.llm_url}/api/chat", json=payload) as response:
            if response.status_code != 200:
                error = await response.aread()
                raise RuntimeError(f"{self.name} returned {response.status_code}: {error!r}")

            async for line in response.aiter_lines():
                line = line.strip()
                if not line:
                    continue
                try:
                    chunk = json.loads(line)
                except json.JSONDecodeError:
                    continue

                delta = chunk.get("message", {}).get("content", "")
                if delta:
                    await on_token(delta)

                if chunk.get("done"):
                    eval_count = chunk.get("eval_count")
                    eval_dur = chunk.get("eval_duration")  # nanoseconds
                    if eval_count and eval_dur:
                        timings["decode_tok_s"] = eval_count / (eval_dur / 1e9)
                        timings["completion_tokens"] = int(eval_count)
                    prompt_count = chunk.get("prompt_eval_count")
                    prompt_dur = chunk.get("prompt_eval_duration")
                    if prompt_count is not None:
                        timings["prompt_tokens"] = int(prompt_count)
                    if prompt_dur:
                        timings["ttft_ms"] = prompt_dur / 1e6  # ns → ms

        return timings
