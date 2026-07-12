"""Static checks for the default GLM model profile."""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_default_litellm_config_uses_glm_52_two_deployment_pool():
    config = (ROOT / "litellm-config.yaml").read_text(encoding="utf-8")

    assert config.count("model_name: glm-5.2") == 2
    assert config.count("model: openai/glm-5.2") == 2
    assert "api_base: https://api.z.ai/api/coding/paas/v4" in config
    assert "api_key: os.environ/ZAI_API_KEY" in config
    assert "api_key: os.environ/ZAI_API_KEY_2" in config
    assert "id: glm-5-2-zai-coding-primary" in config
    assert "id: glm-5-2-zai-coding-secondary" in config


def test_litellm_config_keeps_glm_51_as_additional_alias():
    config = (ROOT / "litellm-config.yaml").read_text(encoding="utf-8")

    assert config.count("model_name: glm-5.1") == 2
    assert config.count("model: openai/glm-5.1") == 2
    assert "id: glm-5-1-zai-coding-primary" in config
    assert "id: glm-5-1-zai-coding-secondary" in config


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


def test_smoke_defaults_use_glm_52():
    makefile = (ROOT / "Makefile").read_text(encoding="utf-8")
    e2e = (ROOT / "tests" / "e2e" / "test_e2e.sh").read_text(encoding="utf-8")
    client_auth = (
        ROOT / "tests" / "e2e" / "test_client_auth.sh"
    ).read_text(encoding="utf-8")
    guardrails = (
        ROOT / "tests" / "e2e" / "test_guardrails_smoke.sh"
    ).read_text(encoding="utf-8")

    assert "ROUTING_SMOKE_MODEL:-glm-5.2" in makefile
    assert 'CHAT_MODEL="${CHAT_MODEL:-glm-5.2}"' in e2e
    assert 'CHAT_MODEL="${CHAT_MODEL:-glm-5.2}"' in client_auth
    assert 'DENIED_MODEL="${DENIED_MODEL:-glm-5.2}"' in client_auth
    assert "ZAI_API_KEY and ZAI_API_KEY_2" in client_auth
    assert 'CHAT_MODEL="${CHAT_MODEL:-glm-5.2}"' in guardrails


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
    assert "curl -L -sf http://localhost:4000/metrics" in makefile
    assert "Presidio Analyzer metrics exposed" in makefile
    assert "открыт без LiteLLM API key" in readme
    assert "require_auth_for_metrics_endpoint: false" in monitoring
