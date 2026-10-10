"""Contract for routing LiteLLM requests through the stock codex-lb API."""

from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]
BASE_CONFIG_PATH = ROOT / "litellm-config.yaml"
CODEX_LB_CONFIG_PATH = ROOT / "litellm-config.codex-lb.yaml"
PUBLIC_OPENAI_MODELS = (
    "gpt-6-luna",
    "gpt-6-sol",
    "gpt-6-astra",
    "gpt-6.1-sol",
)


def _load(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def test_codex_lb_profile_keeps_base_models_and_shared_settings_unchanged():
    base = _load(BASE_CONFIG_PATH)
    codex_lb = _load(CODEX_LB_CONFIG_PATH)

    inherited_models = codex_lb["model_list"][: len(base["model_list"])]
    assert inherited_models == base["model_list"]
    for section in (
        "router_settings",
        "guardrails",
        "general_settings",
        "litellm_settings",
    ):
        assert codex_lb[section] == base[section]


def test_codex_lb_profile_adds_explicit_openai_compatible_public_models():
    config = _load(CODEX_LB_CONFIG_PATH)
    base = _load(BASE_CONFIG_PATH)
    deployments = config["model_list"][len(base["model_list"]) :]

    actual_model_names = tuple(item["model_name"] for item in deployments)
    assert actual_model_names == PUBLIC_OPENAI_MODELS
    for deployment, model_name in zip(deployments, PUBLIC_OPENAI_MODELS):
        assert deployment["litellm_params"] == {
            "model": f"openai/{model_name}",
            "api_base": "http://codex-lb:2455/v1",
            "api_key": "os.environ/CODEX_LB_API_KEY",
        }
        assert deployment["model_info"]["base_model"] == model_name
        assert deployment["model_info"]["id"] == (
            f"openai-{model_name.replace('.', '-')}-codex-lb"
        )
        assert deployment["model_info"]["access_groups"] == [
            "openai",
            "standard",
        ]


def test_codex_lb_profile_does_not_publish_internal_or_temporary_models():
    config = CODEX_LB_CONFIG_PATH.read_text(encoding="utf-8")

    assert "codex-auto-review" not in config
    assert "codex-lb-smoke" not in config


def test_codex_lb_profile_does_not_silently_redirect_old_model_names():
    config = _load(CODEX_LB_CONFIG_PATH)
    deployments = config["model_list"]

    assert not any(item["model_name"].startswith("gpt-5.") for item in deployments)
    assert len({item["model_info"]["id"] for item in deployments}) == len(deployments)
    assert "model_group_alias" not in config["router_settings"]
    assert "fallbacks" not in config["router_settings"]


def test_codex_client_and_live_test_examples_use_luna_default():
    client = (ROOT / "docs/clients/codex.md").read_text(encoding="utf-8")
    runbook = (ROOT / "docs/codex-lb.md").read_text(encoding="utf-8")

    assert 'model = "gpt-6-luna"' in client
    assert "CHAT_MODEL=gpt-6-luna RESPONSES_MODEL=gpt-6-luna" in runbook
    for model in PUBLIC_OPENAI_MODELS:
        assert f"`{model}`" in runbook


def test_base_profile_has_no_codex_lb_dependency_or_openai_catalog():
    base = BASE_CONFIG_PATH.read_text(encoding="utf-8")

    assert "codex-lb" not in base
    assert "CODEX_LB_API_KEY" not in base
    for model_name in PUBLIC_OPENAI_MODELS:
        assert f"model_name: {model_name}" not in base


def test_codex_lb_profile_contains_no_literal_oauth_or_service_credentials():
    config = CODEX_LB_CONFIG_PATH.read_text(encoding="utf-8")

    assert "sk-clb-" not in config
    assert "access_token" not in config
    assert "refresh_token" not in config
    assert "auth.json" not in config
    assert config.count("os.environ/CODEX_LB_API_KEY") == len(
        PUBLIC_OPENAI_MODELS
    )


def test_client_auth_smoke_accepts_codex_lb_as_an_openai_upstream():
    smoke = (ROOT / "tests" / "e2e" / "test_client_auth.sh").read_text(
        encoding="utf-8"
    )

    assert "has_openai_upstream()" in smoke
    assert "has_configured_secret OPENAI_API_KEY ||" in smoke
    assert "has_configured_secret CODEX_LB_API_KEY" in smoke
    assert "if has_openai_upstream; then" in smoke
