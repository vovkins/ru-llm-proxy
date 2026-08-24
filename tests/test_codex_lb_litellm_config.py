"""Contract for routing LiteLLM requests through the stock codex-lb API."""

from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]
BASE_CONFIG_PATH = ROOT / "litellm-config.yaml"
CODEX_LB_CONFIG_PATH = ROOT / "litellm-config.codex-lb.yaml"


def _load(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def test_codex_lb_profile_keeps_base_models_and_shared_settings_unchanged():
    base = _load(BASE_CONFIG_PATH)
    codex_lb = _load(CODEX_LB_CONFIG_PATH)

    assert codex_lb["model_list"][:-1] == base["model_list"]
    for section in (
        "router_settings",
        "guardrails",
        "general_settings",
        "litellm_settings",
    ):
        assert codex_lb[section] == base[section]


def test_codex_lb_profile_adds_one_internal_openai_compatible_smoke_route():
    config = _load(CODEX_LB_CONFIG_PATH)
    deployment = config["model_list"][-1]

    assert deployment == {
        "model_name": "codex-lb-smoke",
        "litellm_params": {
            "model": "openai/gpt-5.6-luna",
            "api_base": "http://codex-lb:2455/v1",
            "api_key": "os.environ/CODEX_LB_API_KEY",
        },
        "model_info": {
            "id": "openai-codex-lb-smoke",
            "base_model": "gpt-5.6-luna",
            "access_groups": ["openai", "experimental"],
        },
    }


def test_base_profile_has_no_codex_lb_dependency_or_experimental_alias():
    base = BASE_CONFIG_PATH.read_text(encoding="utf-8")

    assert "codex-lb" not in base
    assert "CODEX_LB_API_KEY" not in base
    assert "codex-lb-smoke" not in base


def test_codex_lb_profile_contains_no_literal_oauth_or_service_credentials():
    config = CODEX_LB_CONFIG_PATH.read_text(encoding="utf-8")

    assert "sk-clb-" not in config
    assert "access_token" not in config
    assert "refresh_token" not in config
    assert "auth.json" not in config
    assert config.count("os.environ/CODEX_LB_API_KEY") == 1
