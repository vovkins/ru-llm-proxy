"""Static checks for the default GLM model profile."""

from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("model", ("glm-5.3-flash", "glm-5.3"))
def test_glm_models_each_use_two_coding_plan_credentials(model):
    config = yaml.safe_load((ROOT / "litellm-config.yaml").read_text(encoding="utf-8"))
    deployments = [item for item in config["model_list"] if item["model_name"] == model]

    assert len(deployments) == 2
    for deployment, key, suffix in zip(
        deployments, ("ZAI_API_KEY", "ZAI_API_KEY_2"), ("primary", "secondary")
    ):
        assert deployment["litellm_params"] == {
            "model": f"openai/{model}",
            "api_base": "https://api.z.ai/api/coding/paas/v4",
            "api_key": f"os.environ/{key}",
        }
        assert deployment["model_info"] == {
            "id": f"{model.replace('.', '-')}-zai-coding-{suffix}",
            "base_model": model,
            "access_groups": ["zai", "standard"],
        }


def test_default_catalog_has_only_current_models_and_unique_provider_ids():
    config = yaml.safe_load((ROOT / "litellm-config.yaml").read_text(encoding="utf-8"))
    deployments = config["model_list"]

    assert [item["model_name"] for item in deployments] == [
        "glm-5.3-flash", "glm-5.3-flash", "glm-5.3", "glm-5.3"
    ]
    assert len({item["model_info"]["id"] for item in deployments}) == len(deployments)
    assert "model_group_alias" not in config["router_settings"]
    assert "fallbacks" not in config["router_settings"]


def test_default_litellm_config_does_not_activate_optional_providers():
    config = (ROOT / "litellm-config.yaml").read_text(encoding="utf-8")

    assert "zai-glm-" not in config
    assert "openai-gpt-" not in config
    assert "claude-" not in config
    assert "OPENAI_API_KEY" not in config
    assert "ANTHROPIC_API_KEY" not in config


def test_optional_provider_examples_are_separate_from_default_config():
    optional = (
        ROOT / "examples" / "litellm-config.optional-providers.yaml"
    ).read_text(encoding="utf-8")
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    examples = (ROOT / "docs" / "examples.md").read_text(encoding="utf-8")
    configuration = (ROOT / "docs" / "configuration.md").read_text(encoding="utf-8")

    assert "openai/<validated-openai-model-id>" in optional
    assert "anthropic/<validated-anthropic-model-id>" in optional
    assert "examples/litellm-config.optional-providers.yaml" in readme
    assert "examples/litellm-config.optional-providers.yaml" in examples
    assert "`OPENAI_API_KEY` | Пусто" in configuration
    assert "`ANTHROPIC_API_KEY` | Пусто" in configuration


def test_smoke_defaults_use_glm_53_flash():
    makefile = (ROOT / "Makefile").read_text(encoding="utf-8")
    e2e = (ROOT / "tests" / "e2e" / "test_e2e.sh").read_text(encoding="utf-8")
    client_auth = (
        ROOT / "tests" / "e2e" / "test_client_auth.sh"
    ).read_text(encoding="utf-8")
    guardrails = (
        ROOT / "tests" / "e2e" / "test_guardrails_smoke.sh"
    ).read_text(encoding="utf-8")

    assert "ROUTING_SMOKE_MODEL:-glm-5.3-flash" in makefile
    assert 'CHAT_MODEL="${CHAT_MODEL:-glm-5.3-flash}"' in e2e
    assert 'CHAT_MODEL="${CHAT_MODEL:-glm-5.3-flash}"' in client_auth
    assert 'DENIED_MODEL="${DENIED_MODEL:-glm-5.3-flash}"' in client_auth
    assert "ZAI_API_KEY and ZAI_API_KEY_2" in client_auth
    assert 'CHAT_MODEL="${CHAT_MODEL:-glm-5.3-flash}"' in guardrails


def test_env_and_setup_treat_second_zai_key_as_required_default():
    env_example = (ROOT / ".env.example").read_text(encoding="utf-8")
    setup = (ROOT / "scripts" / "setup_env.sh").read_text(encoding="utf-8")

    assert "ZAI_API_KEY=***" in env_example
    assert "ZAI_API_KEY_2=***" in env_example
    assert "OPENAI_API_KEY" not in env_example
    assert "ANTHROPIC_API_KEY" not in env_example
    assert 'ensure_secret "ZAI_API_KEY_2" "***" "" || true' in setup
    assert "Z.AI Coding Plan ключи" in setup


def test_static_suite_runs_model_profile_regression():
    makefile = (ROOT / "Makefile").read_text(encoding="utf-8")

    assert "tests/test_model_profile_config.py" in makefile


def test_metrics_endpoint_is_public_and_monitoring_targets_follow_redirects():
    config = (ROOT / "litellm-config.yaml").read_text(encoding="utf-8")
    makefile = (ROOT / "Makefile").read_text(encoding="utf-8")
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    monitoring = (ROOT / "docs" / "monitoring.md").read_text(encoding="utf-8")

    assert "require_auth_for_metrics_endpoint: false" in config
    assert "$(DEPLOY) metrics" in makefile
    deployment = (ROOT / "scripts/deployment.py").read_text(encoding="utf-8")
    assert "/metrics" in deployment and '"docker", "exec"' in deployment
    assert 'service not in {"litellm", "presidio-analyzer"}' in deployment
    assert 'port = 4000 if service == "litellm" else 5001' in deployment
    assert "открыт без ключа LiteLLM API" in readme
    assert "require_auth_for_metrics_endpoint: false" in monitoring
