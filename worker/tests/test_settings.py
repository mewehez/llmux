"""Registry derivation + settings precedence (worker side)."""
import json

from settings import ModelConfig, Settings, load_models


def test_modelconfig_derives_from_id():
    m = ModelConfig(id="foo")
    assert m.stream == "llm:work:foo"
    assert m.consumer_group == "llm-workers-foo"
    assert m.llm_url == "http://llm-foo:8080"
    assert m.label == "foo"  # label falls back to id


def test_modelconfig_explicit_values_override_derivation():
    m = ModelConfig(id="bar", stream="custom:stream", llm_url="http://x:9", label="Bar")
    assert m.stream == "custom:stream"
    assert m.llm_url == "http://x:9"
    assert m.label == "Bar"
    assert m.consumer_group == "llm-workers-bar"  # still derived


def test_modelconfig_ignores_unknown_infra_fields():
    # The registry carries infra-only / doc keys (e.g. _role) — extra="ignore".
    m = ModelConfig(id="z", _role="doc note", pvc_size="8Gi", llm_args={"threads": 4})
    assert m.id == "z"
    assert m.pvc_size == "8Gi"
    assert m.llm_args == {"threads": 4}


def test_load_models_reads_file_without_filtering(tmp_path):
    reg = tmp_path / "models.json"
    reg.write_text(json.dumps({"models": [
        {"id": "a", "enabled": True},
        {"id": "b", "enabled": False},
    ]}))
    models = load_models(str(reg))
    assert [m.id for m in models] == ["a", "b"]  # load_models does NOT filter enabled
    assert models[0].stream == "llm:work:a"


def test_load_models_falls_back_to_builtin_defaults(tmp_path, monkeypatch):
    # No file found anywhere → built-in 135m/360m registry.
    monkeypatch.delenv("MODELS_CONFIG_PATH", raising=False)
    monkeypatch.setattr("settings._SEARCH_PATHS", ())
    load_models.cache_clear()
    models = load_models(str(tmp_path / "missing.json"))
    assert {m.id for m in models} == {"135m", "360m"}
    load_models.cache_clear()


def test_settings_fills_per_instance_fields_from_registry(tmp_path):
    reg = tmp_path / "models.json"
    reg.write_text(json.dumps({"models": [
        {"id": "360m", "worker_concurrency": 7, "runner": "ollama"},
    ]}))
    s = Settings(_env_file=None, model_id="360m", models_config_path=str(reg))
    assert s.stream == "llm:work:360m"
    assert s.consumer_group == "llm-workers-360m"
    assert s.llm_url == "http://llm-360m:8080"
    assert s.worker_concurrency == 7
    assert s.runner == "ollama"


def test_settings_unknown_model_id_uses_safe_fallbacks(tmp_path):
    reg = tmp_path / "models.json"
    reg.write_text(json.dumps({"models": [{"id": "135m"}]}))
    s = Settings(_env_file=None, model_id="does-not-exist", models_config_path=str(reg))
    assert s.stream == "llm:work:135m"
    assert s.consumer_group == "llm-workers"
    assert s.worker_concurrency == 4
    assert s.runner == "llamacpp"


def test_settings_env_overrides_registry(tmp_path):
    reg = tmp_path / "models.json"
    reg.write_text(json.dumps({"models": [{"id": "135m", "worker_concurrency": 2}]}))
    # Explicit value (highest precedence) wins over the registry entry.
    s = Settings(_env_file=None, model_id="135m", models_config_path=str(reg),
                 llm_url="http://localhost:9999", worker_concurrency=1)
    assert s.llm_url == "http://localhost:9999"
    assert s.worker_concurrency == 1
