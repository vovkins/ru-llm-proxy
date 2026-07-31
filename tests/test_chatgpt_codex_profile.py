"""Static checks for the opt-in ChatGPT subscription profile."""

import tomllib
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]


def test_litellm_profile_preserves_codex_model_and_guardrail_order():
    profile = yaml.safe_load(
        (ROOT / "litellm-config.chatgpt-codex.yaml").read_text()
    )
    deployment = profile["model_list"][0]
    params = deployment["litellm_params"]
    guardrails = [item["guardrail_name"] for item in profile["guardrails"]]

    assert deployment["model_name"] == "*"
    assert params["model"] == "openai/*"
    assert params["api_base"] == "os.environ/CHATGPT_CODEX_API_BASE"
    assert params["api_key"] == "oauth-passthrough-placeholder"
    assert guardrails == [
        "ru-pii-mask-pre",
        "chatgpt-codex-auth",
        "ru-pii-mask-post",
    ]


def test_compose_overlay_uses_dedicated_config_without_static_oauth():
    overlay_text = (ROOT / "docker-compose.chatgpt-codex.yml").read_text()
    overlay = yaml.safe_load(overlay_text)
    service = overlay["services"]["litellm"]

    assert any(
        "litellm-config.chatgpt-codex.yaml:/app/config.yaml:ro" in volume
        for volume in service["volumes"]
    )
    assert any(
        "CHATGPT_CODEX_API_BASE=" in item
        and "chatgpt.com/backend-api/codex" in item
        for item in service["environment"]
    )
    assert "oauth-passthrough-placeholder" not in overlay_text
    assert "LITELLM_MASTER_KEY" not in overlay_text


def test_codex_profile_separates_proxy_key_from_chatgpt_oauth():
    profile = tomllib.loads(
        (ROOT / "config/ru-chatgpt-codex.config.toml").read_text()
    )
    provider = profile["model_providers"]["ru-chatgpt-codex"]

    assert profile["model_provider"] == "ru-chatgpt-codex"
    assert provider["base_url"] == "http://127.0.0.1:4100/v1"
    assert provider["wire_api"] == "responses"
    assert provider["requires_openai_auth"] is True
    assert provider["supports_websockets"] is False
    assert provider["env_http_headers"] == {
        "x-litellm-api-key": "RU_LLM_PROXY_TOKEN"
    }
