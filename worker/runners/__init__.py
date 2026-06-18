"""Pluggable inference-runner abstraction (Phase 3).

A Runner hides the wire protocol of a specific inference backend behind one
interface, so the worker (and the benchmark harness) can drive llama.cpp,
Ollama or vLLM interchangeably and compare them on normalized metrics. The
runner is selected per model by the registry's `runner` field (RUNNER_TYPE).

The runner owns ONLY the backend HTTP protocol (request shape + streaming
parse + timings). Redis result-stream / pub/sub / telemetry stay in the worker.
"""

from .base import Runner, Timings
from .ollama import OllamaRunner
from .openai import LlamaCppRunner, VllmRunner

RUNNERS: dict[str, type[Runner]] = {
    "llamacpp": LlamaCppRunner,
    "vllm":     VllmRunner,
    "ollama":   OllamaRunner,
}


def get_runner(runner_type: str, llm_url: str) -> Runner:
    """Build the Runner for a backend type, defaulting to llama.cpp."""
    cls = RUNNERS.get((runner_type or "llamacpp").lower(), LlamaCppRunner)
    return cls(llm_url)


__all__ = ["Runner", "Timings", "get_runner", "RUNNERS",
           "LlamaCppRunner", "VllmRunner", "OllamaRunner"]
