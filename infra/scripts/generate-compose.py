#!/usr/bin/env python
"""Generate docker-compose.yml from config/models.json (the registry).

Dev single-source: for each ENABLED model it emits an llm / worker / slots /
model-downloader service + a named volume, with literal values pulled from the
registry (model_file, model_url, llm_args). Shared services (redis, api,
dashboard, benchmark-runner, ollama) are constant.

Run after editing the registry:
    uv run --with pyyaml python infra/scripts/generate-compose.py
"""
import json
import sys
from pathlib import Path

try:
    import yaml
except ImportError:
    sys.exit("needs pyyaml — run: uv run --with pyyaml python infra/scripts/generate-compose.py")

ROOT = Path(__file__).resolve().parents[2]
REGISTRY = ROOT / "config" / "models.json"
OUT = ROOT / "docker-compose.yml"
BASE_LLM_PORT = 8001  # host port for llm-<id> = BASE + index in the full models list
DEFAULT_ARGS = {"ctx_size": 2048, "threads": 4, "parallel": 4, "threads_http": 8}


def llm_service(m: dict, host_port: int) -> dict:
    mid, mf = m["id"], m["model_file"]
    a = {**DEFAULT_ARGS, **m.get("llm_args", {})}
    return {
        "image": "ghcr.io/ggml-org/llama.cpp:server",
        "ports": [f"{host_port}:8080"],
        "volumes": [f"models_{mid}:/models"],
        "command": [
            "--model", f"/models/{mf}", "--host", "0.0.0.0", "--port", "8080",
            "--ctx-size", str(a["ctx_size"]), "--threads", str(a["threads"]),
            "--parallel", str(a["parallel"]), "--threads-http", str(a["threads_http"]),
            "--slots", "--metrics",
        ],
        "depends_on": {
            "redis": {"condition": "service_healthy"},
            f"model-downloader-{mid}": {"condition": "service_completed_successfully"},
        },
        "healthcheck": {
            "test": ["CMD-SHELL", "curl -sf http://localhost:8080/health || exit 1"],
            "interval": "10s", "timeout": "5s", "retries": 10, "start_period": "30s",
        },
    }


def downloader_service(m: dict) -> dict:
    mid, mf, mu = m["id"], m["model_file"], m["model_url"]
    return {
        "image": "alpine:3.19",
        "volumes": [f"models_{mid}:/models"],
        "command": ["sh", "-c",
                    f"if [ -f /models/{mf} ]; then echo 'Model exists, skipping.'; "
                    f"else echo 'Downloading {mf}' && wget -q -O /models/{mf} '{mu}' && echo 'Done'; fi"],
    }


def worker_volumes() -> list:
    return ["./worker:/app", "/app/.venv", "./config:/config:ro"]


def worker_service(m: dict) -> dict:
    mid = m["id"]
    return {
        "build": "./worker",
        "environment": [
            "REDIS_URL=redis://redis:6379/0",
            "MODELS_CONFIG_PATH=/config/models.json",
            f"MODEL_ID={mid}",
            f"LLM_URL=http://llm-{mid}:8080",
        ],
        "volumes": worker_volumes(),
        "depends_on": {
            "redis": {"condition": "service_healthy"},
            f"llm-{mid}": {"condition": "service_healthy"},
        },
    }


def slots_service(m: dict) -> dict:
    mid = m["id"]
    return {
        "build": "./worker",
        "entrypoint": ["uv", "run", "python", "slots_sidecar.py"],
        "environment": [
            "REDIS_URL=redis://redis:6379/0",
            "MODELS_CONFIG_PATH=/config/models.json",
            f"MODEL_ID={mid}",
            f"LLM_URL=http://llm-{mid}:8080",
            f"POD_NAME=llm-{mid}",
        ],
        "volumes": worker_volumes(),
        "depends_on": {
            "redis": {"condition": "service_healthy"},
            f"llm-{mid}": {"condition": "service_healthy"},
        },
    }


def build() -> dict:
    all_models = json.loads(REGISTRY.read_text())["models"]
    services: dict = {
        "redis": {
            "image": "redis:7-alpine",
            "ports": ["6379:6379"],
            "volumes": ["redis_data:/data"],
            "command": "redis-server --appendonly yes",
            "healthcheck": {"test": ["CMD", "redis-cli", "ping"], "interval": "5s", "timeout": "3s", "retries": 5},
        },
        "api": {
            "build": "./api",
            "ports": ["8000:8080"],
            "environment": ["REDIS_URL=redis://redis:6379/0", "MODELS_CONFIG_PATH=/config/models.json"],
            "volumes": ["./api:/app", "/app/.venv", "./config:/config:ro"],
            "depends_on": {"redis": {"condition": "service_healthy"}},
            "healthcheck": {"test": ["CMD-SHELL", "curl -sf http://localhost:8080/health || exit 1"],
                            "interval": "10s", "timeout": "5s", "retries": 5, "start_period": "10s"},
        },
        "dashboard": {
            "build": {"context": "./dashboard", "target": "dev"},
            "ports": ["5173:5173"],
            "volumes": ["./dashboard:/app", "/app/node_modules"],
            "environment": ["VITE_API_URL=http://localhost:8000"],
            "depends_on": {"api": {"condition": "service_healthy"}},
        },
    }

    volumes: dict = {"redis_data": None}
    for i, m in enumerate(all_models):
        if not m.get("enabled", True):
            continue
        mid = m["id"]
        services[f"llm-{mid}"] = llm_service(m, BASE_LLM_PORT + i)
        services[f"model-downloader-{mid}"] = downloader_service(m)
        services[f"worker-{mid}"] = worker_service(m)
        services[f"slots-{mid}"] = slots_service(m)
        volumes[f"models_{mid}"] = None

    services["benchmark-runner"] = {
        "build": "./worker",
        "entrypoint": ["uv", "run", "python", "bench_worker.py"],
        "environment": ["REDIS_URL=redis://redis:6379/0", "MODELS_CONFIG_PATH=/config/models.json"],
        "volumes": worker_volumes(),
        "depends_on": {"redis": {"condition": "service_healthy"}},
    }
    services["ollama"] = {
        "image": "ollama/ollama:latest",
        "profiles": ["ollama"],
        "ports": ["11434:11434"],
        "volumes": ["ollama_data:/root/.ollama"],
    }
    volumes["ollama_data"] = None

    return {"services": services, "volumes": volumes}


def render() -> str:
    """Full docker-compose.yml content (header + YAML) for the current registry."""
    header = (
        "# ─────────────────────────────────────────────────────────────────────\n"
        "# GENERATED from config/models.json — DO NOT EDIT BY HAND.\n"
        "# Re-generate after editing the registry:\n"
        "#   uv run --with pyyaml python infra/scripts/generate-compose.py  (or: make generate)\n"
        "# Verify it is in sync with the registry:  make verify-generated\n"
        "# Per ENABLED model: llm-<id> / worker-<id> / slots-<id> / model-downloader-<id>\n"
        "# (host port for llm-<id> = 8001 + its index in the registry).\n"
        "# ─────────────────────────────────────────────────────────────────────\n"
    )
    return header + yaml.safe_dump(build(), sort_keys=False, default_flow_style=False, width=1000)


def main() -> None:
    check = "--check" in sys.argv[1:]
    content = render()
    enabled = [m["id"] for m in json.loads(REGISTRY.read_text())["models"] if m.get("enabled", True)]
    if check:
        current = OUT.read_text() if OUT.exists() else ""
        if current != content:
            print(f"DRIFT: {OUT.relative_to(ROOT)} is stale — run `make generate`.", file=sys.stderr)
            sys.exit(1)
        print(f"OK: {OUT.relative_to(ROOT)} matches the registry — enabled models: {enabled}")
        return
    OUT.write_text(content)
    print(f"Wrote {OUT.relative_to(ROOT)} — enabled models: {enabled}")


if __name__ == "__main__":
    main()
