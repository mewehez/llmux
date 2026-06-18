"""Centralised configuration for the worker and the slots sidecar.

Loaded from environment / .env via pydantic-settings. Precedence (highest
first): real environment variables (docker-compose / k8s) → .env file →
registry (config/models.json, selected by MODEL_ID) → built-in defaults.

A worker serves ONE model. Set MODEL_ID and the per-instance values (stream,
llm_url, consumer_group) are derived from the registry entry. Any of them can
still be set explicitly in the environment to override the registry — useful
for bare-metal dev where the llm runs on localhost rather than a service name.
"""

import json
import os
import socket
from functools import lru_cache
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


# ── Models registry (mirrors api/settings.py — data lives once in the JSON) ──

class ModelConfig(BaseModel):
    model_config = ConfigDict(protected_namespaces=(), extra="ignore")

    id:                 str
    label:              str  = ""
    enabled:            bool = True
    runner:             str  = "llamacpp"
    stream:             str  = ""
    consumer_group:     str  = ""
    llm_url:            str  = ""
    model_file:         str | None = None
    model_url:          str | None = None
    llm_args:           dict = {}
    pvc_size:           str  = "500Mi"
    resources:          dict = {}
    worker_concurrency: int  = 4
    max_replicas:       int  = 1
    max_llm_pods:       int  = 1

    @model_validator(mode="after")
    def _derive(self) -> "ModelConfig":
        self.stream         = self.stream or f"llm:work:{self.id}"
        self.consumer_group = self.consumer_group or f"llm-workers-{self.id}"
        self.llm_url        = self.llm_url or f"http://llm-{self.id}:8080"
        self.label          = self.label or self.id
        return self


DEFAULT_MODELS: list[dict] = [
    {"id": "135m", "label": "SmolLM2-135M", "stream": "llm:work:135m",
     "consumer_group": "llm-workers-135m", "llm_url": "http://llm-135m:8080"},
    {"id": "360m", "label": "SmolLM2-360M", "stream": "llm:work:360m",
     "consumer_group": "llm-workers-360m", "llm_url": "http://llm-360m:8080"},
]

_SEARCH_PATHS = ("config/models.json", "../config/models.json")


@lru_cache(maxsize=8)
def load_models(path: str | None = None) -> tuple[ModelConfig, ...]:
    candidates = [path, os.getenv("MODELS_CONFIG_PATH"), *_SEARCH_PATHS]
    for candidate in candidates:
        if candidate and Path(candidate).exists():
            raw = json.loads(Path(candidate).read_text())
            entries = raw["models"] if isinstance(raw, dict) else raw
            return tuple(ModelConfig(**e) for e in entries)
    return tuple(ModelConfig(**e) for e in DEFAULT_MODELS)


# ── Settings ─────────────────────────────────────────────────────────────────

class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
        protected_namespaces=(),   # allow `model_*` fields
    )

    # ── Connection / registry ────────────────────────────────────────────────
    redis_url:          str = "redis://localhost:6379/0"
    models_config_path: str | None = None
    model_id:           str = "135m"   # which registry entry this instance serves

    # ── Per-instance (derived from the registry unless set explicitly) ───────
    stream:         str | None = None
    llm_url:        str | None = None
    consumer_group: str | None = None
    model_name:     str | None = None
    runner:         str | None = None   # llamacpp | ollama | vllm
    worker_id:      str = Field(default_factory=socket.gethostname)
    pod_name:       str = Field(default_factory=socket.gethostname)

    # ── Worker behaviour ─────────────────────────────────────────────────────
    worker_concurrency: int | None = None   # falls back to the registry entry
    block_ms:           int = 2000   # XREADGROUP block (ms)
    worker_ttl:         int = 120    # metrics hash TTL (s); refreshed each heartbeat
    heartbeat_interval: int = 10     # seconds between liveness pings

    # ── Inference shaping ────────────────────────────────────────────────────
    llm_model:            str   = "local-model"  # model name sent to the runner
    max_tokens:           int   = 512
    http_timeout:         float = 60     # seconds to wait on the runner call
    result_ttl:           int   = 300    # replay stream retention (s)
    result_stream_maxlen: int   = 1000   # max tokens buffered for replay

    # ── Slots sidecar ────────────────────────────────────────────────────────
    poll_ms:    int   = 1000   # /slots poll interval (ms)
    slots_ttl:  int   = 15     # slot hash TTL (s)
    slots_http_timeout: float = 2  # per /slots and /metrics poll (s)

    # ── Telemetry (Phase 2) ──────────────────────────────────────────────────
    events_maxlen:  int = 2000   # cap on the llm:events stream
    metrics_window: int = 300    # completion-sample retention for time series (s)
    live_update_ms: int = 500    # throttle for live-state hash writes during inference

    # ── Benchmark harness (Phase 3) ──────────────────────────────────────────
    bench_ttl:       int = 604800  # retention for a benchmark result (s) = 7 days
    bench_index_max: int = 100     # how many recent run ids to keep in the index

    # ── Resilience (Phase 6) ─────────────────────────────────────────────────
    max_attempts:      int = 3              # tries before a task is dead-lettered
    dead_stream:       str = "llm:work:dead"
    dead_maxlen:       int = 1000
    claim_min_idle_ms: int = 30000          # reclaim PEL entries idle longer than this
    claim_interval:    int = 30             # reclaim sweep interval (s)
    claim_count:       int = 10             # max entries reclaimed per sweep
    liveness_file:     str = "/tmp/worker_alive"  # touched each heartbeat; probed by k8s

    @model_validator(mode="after")
    def _fill_from_registry(self) -> "Settings":
        entry = next((m for m in load_models(self.models_config_path) if m.id == self.model_id), None)
        if entry is not None:
            self.stream             = self.stream or entry.stream
            self.llm_url            = self.llm_url or entry.llm_url
            self.consumer_group     = self.consumer_group or entry.consumer_group
            self.worker_concurrency = self.worker_concurrency or entry.worker_concurrency
            self.runner             = self.runner or entry.runner
        # Final fallbacks (no registry / unknown MODEL_ID).
        self.stream             = self.stream or "llm:work:135m"
        self.llm_url            = self.llm_url or "http://localhost:8001"
        self.consumer_group     = self.consumer_group or "llm-workers"
        self.model_name         = self.model_name or self.model_id
        self.worker_concurrency = self.worker_concurrency or 4
        self.runner             = self.runner or "llamacpp"
        return self


settings = Settings()
