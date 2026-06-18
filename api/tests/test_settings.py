"""Registry-derived API settings: enabled filtering + lookup helpers."""
import json
from pathlib import Path

from settings import ModelConfig, Settings

REPO = Path(__file__).resolve().parents[2]


def test_modelconfig_derives_and_keeps_display_label():
    m = ModelConfig(id="foo")
    assert m.stream == "llm:work:foo"
    assert m.consumer_group == "llm-workers-foo"
    assert m.llm_url == "http://llm-foo:8080"
    assert m.display_label == "foo"


def test_models_property_serves_only_enabled(tmp_path):
    reg = tmp_path / "models.json"
    reg.write_text(json.dumps({"models": [
        {"id": "on1", "enabled": True},
        {"id": "off", "enabled": False},
        {"id": "on2"},  # enabled defaults to True
    ]}))
    s = Settings(_env_file=None, models_config_path=str(reg))
    assert [m.id for m in s.models] == ["on1", "on2"]
    assert s.model("off") is None          # disabled model is not served
    assert s.model("on1").id == "on1"


def test_stream_map_and_lookup_helpers(tmp_path):
    reg = tmp_path / "models.json"
    reg.write_text(json.dumps({"models": [
        {"id": "135m", "max_replicas": 3, "max_llm_pods": 2},
    ]}))
    s = Settings(_env_file=None, models_config_path=str(reg))
    assert s.stream_map == {"135m": "llm:work:135m"}
    assert s.llm_url("135m") == "http://llm-135m:8080"
    assert s.max_replicas("135m") == 3
    assert s.max_llm_pods("135m") == 2


def test_unknown_model_returns_safe_defaults(tmp_path):
    reg = tmp_path / "models.json"
    reg.write_text(json.dumps({"models": [{"id": "135m"}]}))
    s = Settings(_env_file=None, models_config_path=str(reg))
    assert s.llm_url("ghost") == "http://llm-ghost:8080"
    assert s.max_replicas("ghost") == 1
    assert s.max_llm_pods("ghost") == 1


def test_committed_registry_excludes_disabled_qwen():
    # The real config/models.json: 135m/360m enabled, qwen3-* disabled.
    s = Settings(_env_file=None, models_config_path=str(REPO / "config" / "models.json"))
    ids = {m.id for m in s.models}
    assert {"135m", "360m"} <= ids
    assert "qwen3-8b" not in ids
    assert "qwen3-32b" not in ids
