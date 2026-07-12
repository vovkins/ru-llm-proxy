"""Static checks for grouped environment variable documentation."""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


EXPECTED_DOCUMENTED_ENV = {
    # Provider keys
    "ZAI_API_KEY",
    "ZAI_API_KEY_2",
    "OPENAI_API_KEY",
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_BYOK_API_KEY",
    # Admin and storage
    "LITELLM_MASTER_KEY",
    "LITELLM_SALT_KEY",
    "UI_USERNAME",
    "UI_PASSWORD",
    "DISABLE_ADMIN_UI",
    "JWT_PUBLIC_KEY_URL",
    "JWT_AUDIENCE",
    "LITELLM_PORT",
    "LITELLM_DB_URL",
    "DATABASE_URL",
    "POSTGRES_USER",
    "POSTGRES_PASSWORD",
    "POSTGRES_DB",
    "REDIS_HOST",
    "REDIS_PORT",
    "REDIS_URL",
    "PROMETHEUS_MULTIPROC_DIR",
    # Analyzer and recognizers
    "PRESIDIO_ANALYZER_URL",
    "PRESIDIO_ANALYZER_PORT",
    "PRESIDIO_ANALYZER_WORKERS",
    "PRESIDIO_ANALYZER_CONCURRENCY_LIMIT",
    "PRESIDIO_ANALYZER_QUEUE_LIMIT",
    "PRESIDIO_ANALYZER_QUEUE_TIMEOUT_SECONDS",
    "PRESIDIO_ANALYZER_DETECT_BARE_INN_BY_CHECKSUM",
    "PRESIDIO_ANALYZER_INTERNAL_DOMAIN_SUFFIXES",
    "PRESIDIO_ANALYZER_DETECT_PUBLIC_IPS",
    "DEEPPAVLOV_NER_REQUIRED",
    # Guardrail policy
    "PII_GUARDRAIL_MODE",
    "PII_GUARDRAIL_FAILURE_MODE",
    "PII_MAPPING_TTL_SECONDS",
    "PRE_EGRESS_POLICY_MODE",
    "FINAL_PAYLOAD_LEAK_CHECK_MODE",
    "FINAL_PAYLOAD_LEAK_CHECK_CANARIES",
    "DICTIONARY_SUBSTITUTIONS_ENABLED",
    "DICTIONARY_SUBSTITUTIONS_FILE",
    "DICTIONARY_SUBSTITUTIONS_JSON",
    "DICTIONARY_SUBSTITUTIONS_FAILURE_MODE",
    "SYNTHETIC_PII_ALLOWLIST_MODE",
    "SYNTHETIC_PII_ALLOWLIST_JSON",
    "REGULATED_TOPIC_POLICY_MODE",
    "REGULATED_TOPIC_POLICY_EXTRA_RULES_JSON",
    # Guardrail dependency clients
    "PII_GUARDRAIL_REDIS_MAX_CONNECTIONS",
    "PII_GUARDRAIL_REDIS_SOCKET_CONNECT_TIMEOUT_SECONDS",
    "PII_GUARDRAIL_REDIS_SOCKET_TIMEOUT_SECONDS",
    "PII_GUARDRAIL_ANALYZER_TIMEOUT_SECONDS",
    "PII_GUARDRAIL_ANALYZER_CONNECT_TIMEOUT_SECONDS",
    "PII_GUARDRAIL_ANALYZER_MAX_CONNECTIONS",
    "PII_GUARDRAIL_ANALYZER_MAX_KEEPALIVE_CONNECTIONS",
    # DeepPavlov build
    "DEEPPAVLOV_NER_MODEL_URL",
    "DEEPPAVLOV_NER_MODEL_SHA256",
    "DEEPPAVLOV_NER_DOWNLOAD_TIMEOUT_SECONDS",
    "DEEPPAVLOV_NER_MODEL_DIR",
    # Client and smoke helpers
    "RU_LLM_PROXY_TOKEN",
    "RU_LLM_PROXY_URL",
    "OIDC_JWT",
    "ANTHROPIC_BASE_URL",
    "ANTHROPIC_AUTH_TOKEN",
    "ANTHROPIC_MODEL",
    "ANTHROPIC_CUSTOM_HEADERS",
    "LITELLM_URL",
    "API_URL",
    "ENV_FILE",
    "CHAT_MODEL",
    "DENIED_MODEL",
    "RESPONSES_MODEL",
    "MESSAGES_MODEL",
    "REQUIRE_ALL_PROTOCOLS",
    "ROUTING_SMOKE_MODEL",
    "LITELLM_ROUTING_TEST_KEY",
    "ANALYZER_URL",
    "CURL_CONNECT_TIMEOUT",
    "CURL_MAX_TIME",
    "SMOKE_RUN_ID",
    "SMOKE_PII_MARKER",
    "PRE_EGRESS_PROXY_PORT",
    "PRE_EGRESS_PROXY_PROJECT",
    "FINAL_LEAK_PROXY_PORT",
    "FINAL_LEAK_PROXY_PROJECT",
    # Virtual-key helper
    "KEY_ALIAS",
    "MODELS",
    "DURATION",
    "BUDGET_DURATION",
    "MAX_BUDGET",
    "RPM_LIMIT",
    "TPM_LIMIT",
    "USER_ID",
    "TEAM_ID",
    "METADATA_JSON",
    # Internals
    "PYTHONPATH",
    "PYTHONDONTWRITEBYTECODE",
    "PORT",
    "PYTHON_LOCAL",
    "PYTEST",
    "PYTEST_DOCKER_FLAGS",
}


QUICK_START_ENV = {
    "ZAI_API_KEY",
    "ZAI_API_KEY_2",
    "LITELLM_MASTER_KEY",
    "LITELLM_SALT_KEY",
    "UI_USERNAME",
    "UI_PASSWORD",
    "DISABLE_ADMIN_UI",
    "POSTGRES_PASSWORD",
    "LITELLM_DB_URL",
}


def _read(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def test_configuration_reference_documents_expected_env_vars():
    configuration = _read("docs/configuration.md")

    for name in EXPECTED_DOCUMENTED_ENV:
        assert f"`{name}`" in configuration, name

    for heading in (
        "## Default GLM Provider Pool",
        "## Optional Provider Examples",
        "## LiteLLM Admin And Secrets",
        "## LiteLLM Runtime And Storage",
        "## Presidio Analyzer Service",
        "## Recognizer Calibration",
        "## Guardrail Policy",
        "## Dictionary Substitutions",
        "## Synthetic/Test PII Allowlist",
        "## Regulated-Topic Policy",
        "## Guardrail Dependency Clients",
        "## DeepPavlov Model And Runtime",
        "## Client Tokens And Local Client Guides",
        "## Live Smoke And Diagnostics",
        "## Virtual Key Helper",
        "## Developer And Container Internals",
    ):
        assert heading in configuration


def test_quick_start_env_example_stays_minimal_and_grouped():
    env_example = _read(".env.example")

    for name in QUICK_START_ENV:
        assert f"{name}=" in env_example

    for name in EXPECTED_DOCUMENTED_ENV - QUICK_START_ENV:
        assert f"{name}=" not in env_example, name

    assert "Full grouped reference: docs/configuration.md" in env_example
    assert "# === Default GLM Provider Pool ===" in env_example
    assert "# === LiteLLM Admin And Secrets ===" in env_example
    assert "# === LiteLLM Persistence ===" in env_example


def test_primary_docs_link_to_configuration_reference():
    for path in (
        "README.md",
        "docs/examples.md",
        "docs/architecture.md",
        "docs/monitoring.md",
    ):
        assert "configuration.md" in _read(path), path


def test_compose_defaults_use_fail_closed_and_optional_provider_keys_are_quiet():
    compose = _read("docker-compose.yml")
    guardrail = _read("litellm_guardrails/pii_guardrail.py")

    assert "PII_GUARDRAIL_FAILURE_MODE=${PII_GUARDRAIL_FAILURE_MODE:-fail_closed}" in compose
    assert 'os.getenv("PII_GUARDRAIL_FAILURE_MODE", "fail_closed")' in guardrail
    assert "OPENAI_API_KEY=${OPENAI_API_KEY:-}" in compose
    assert "ANTHROPIC_API_KEY=${ANTHROPIC_API_KEY:-}" in compose
    assert "REDIS_URL=${REDIS_URL:-redis://redis:6379}" in compose
    assert "DEEPPAVLOV_NER_REQUIRED=${DEEPPAVLOV_NER_REQUIRED:-true}" in compose


def test_analyzer_pins_deeppavlov_compatible_transformers_runtime():
    requirements = _read("presidio/requirements-analyzer.txt")
    makefile = _read("Makefile")
    e2e = _read("tests/e2e/test_e2e.sh")

    assert "transformers==4.30.2" in requirements
    assert '"ner":"loaded"' in makefile
    assert '"ner":"loaded"' in e2e


def test_setup_env_only_backfills_quick_start_values():
    setup = _read("scripts/setup_env.sh")

    assert "docs/configuration.md" in setup
    assert 'ensure_key_exists "DISABLE_ADMIN_UI" "False"' in setup
    assert 'ensure_secret "ZAI_API_KEY_2" "***" "" || true' in setup

    for name in EXPECTED_DOCUMENTED_ENV - QUICK_START_ENV:
        assert f'ensure_key_exists "{name}"' not in setup, name
