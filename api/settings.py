"""Centralised configuration loaded from environment / .env via pydantic-settings.

Precedence (highest first): real environment variables (set by docker-compose or
k8s) → values in the .env file → the defaults below. So deployment-specific
overrides live in the orchestrator, while the .env file holds local defaults.

The MODEL SET (which models exist, their streams, scaling maxes, runner type) is
loaded from a JSON registry — config/models.json — so adding a model is a data
edit, not a code change. See load_models() for the search path.
"""

import json
import os
from functools import lru_cache
from pathlib import Path

from pydantic import BaseModel, ConfigDict, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


# ── Models registry ──────────────────────────────────────────────────────────

class ModelConfig(BaseModel):
    """One entry in the model registry (config/models.json). stream /
    consumer_group / llm_url are derived from `id` unless given explicitly."""
    model_config = ConfigDict(protected_namespaces=(), extra="ignore")

    id:                 str
    label:              str  = ""
    enabled:            bool = True
    runner:             str  = "llamacpp"   # llamacpp | ollama | vllm
    stream:             str  = ""
    consumer_group:     str  = ""
    llm_url:            str  = ""
    # Infra-layer fields (consumed by the Helm chart; harmless to the app):
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

    @property
    def display_label(self) -> str:
        return self.label or self.id


# Built-in fallback registry — used when no config/models.json is found, so the
# system keeps working out of the box (mirrors the original 135m/360m setup).
DEFAULT_MODELS: list[dict] = [
    {"id": "135m", "label": "SmolLM2-135M", "stream": "llm:work:135m",
     "consumer_group": "llm-workers-135m", "llm_url": "http://llm-135m:8080",
     "max_replicas": 3, "max_llm_pods": 2},
    {"id": "360m", "label": "SmolLM2-360M", "stream": "llm:work:360m",
     "consumer_group": "llm-workers-360m", "llm_url": "http://llm-360m:8080",
     "max_replicas": 2, "max_llm_pods": 2},
]

# Searched in order; first existing wins. Lets the file be found whether the
# process runs from the repo root, from api/, or via an explicit env path.
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
        protected_namespaces=(),
    )

    # ── Connections / registry ───────────────────────────────────────────────
    redis_url:          str = "redis://localhost:6379/0"
    models_config_path: str | None = None   # overrides the registry search path
    cors_origins:       str = "*"            # comma-separated allowlist, or "*"

    # ── Task + SSE behaviour ─────────────────────────────────────────────────
    task_ttl:           int   = 3600   # task metadata hash TTL (s)
    sse_timeout:        int   = 120    # max idle seconds before an SSE stream gives up
    sse_poll:           float = 0.05   # pub/sub poll interval (s)
    work_stream_maxlen: int   = 1000   # work queue cap

    # ── Liveness thresholds ──────────────────────────────────────────────────
    worker_healthy_ttl: int = 60   # worker/pod alive if it reported within this many s
    slots_stale_after:  int = 30   # ignore slot data older than this (s)

    # ── Telemetry (Phase 2) ──────────────────────────────────────────────────
    events_replay:  int = 50    # events replayed when an SSE/list client connects
    metrics_window: int = 300   # default time-series window (s)
    events_maxlen:  int = 2000  # cap when the API emits to llm:events (scale events)

    # ── Scale-event reconciler (Phase 4) ─────────────────────────────────────
    # Observes worker/llm-pod counts via Redis liveness and emits real
    # scale_up/scale_down events — works whether KEDA (k8s) or `docker compose
    # --scale` (dev) did the scaling. No k8s API dependency.
    scale_events_enabled: bool = True
    scale_poll:           int  = 5   # reconcile interval (s)

    # ── Derived from the registry ────────────────────────────────────────────
    @property
    def models(self) -> tuple[ModelConfig, ...]:
        # Only ENABLED models are served / shown / scaled.
        return tuple(m for m in load_models(self.models_config_path) if m.enabled)

    def model(self, model_id: str) -> ModelConfig | None:
        return next((m for m in self.models if m.id == model_id), None)

    @property
    def stream_map(self) -> dict[str, str]:
        return {m.id: m.stream for m in self.models}

    def llm_url(self, model_id: str) -> str:
        m = self.model(model_id)
        return m.llm_url if m else f"http://llm-{model_id}:8080"

    def max_replicas(self, model_id: str) -> int:
        m = self.model(model_id)
        return m.max_replicas if m else 1

    def max_llm_pods(self, model_id: str) -> int:
        m = self.model(model_id)
        return m.max_llm_pods if m else 1


settings = Settings()
