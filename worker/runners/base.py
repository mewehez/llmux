"""Runner base class + the normalized timings every backend reports."""

from typing import Awaitable, Callable, TypedDict

import httpx

# Called once per generated token delta. The worker passes a closure that writes
# the token to the Redis result stream + pub/sub and updates live state.
OnToken = Callable[[str], Awaitable[None]]


class Timings(TypedDict, total=False):
    """Backend-reported timings, normalized across runners. All optional — the
    worker falls back to wall-clock measurement for anything missing."""
    ttft_ms:           float   # time to first token (backend-reported, if any)
    decode_tok_s:      float   # generation rate (tokens/sec)
    prompt_tokens:     int
    completion_tokens: int


class Runner:
    """Base runner. Subclasses implement `generate` for a specific backend."""

    name: str = "base"
    capabilities: dict = {}

    def __init__(self, llm_url: str):
        self.llm_url = llm_url.rstrip("/")

    def health_url(self) -> str:
        return f"{self.llm_url}/health"

    async def generate(
        self,
        client: httpx.AsyncClient,
        message: str,
        *,
        max_tokens: int,
        model: str,
        on_token: OnToken,
    ) -> Timings:
        """Stream a completion for `message`, awaiting `on_token(delta)` per
        token, and return normalized timings. Raises on a non-200 response."""
        raise NotImplementedError
