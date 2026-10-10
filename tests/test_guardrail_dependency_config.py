"""Static checks for PII guardrail dependency client configuration."""

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
LITELLM_VERSION = "1.98.0"
LITELLM_IMAGE = (
    f"docker.litellm.ai/berriai/litellm:v{LITELLM_VERSION}@"
    "sha256:20b5044b619055374061a6d5b7b08754cad75aeabbf82ddf4f69cc0cf80ddaf4"
)

EXPECTED_ENV = {
    "PII_GUARDRAIL_REDIS_MAX_CONNECTIONS": "20",
    "PII_GUARDRAIL_REDIS_SOCKET_CONNECT_TIMEOUT_SECONDS": "1.0",
    "PII_GUARDRAIL_REDIS_SOCKET_TIMEOUT_SECONDS": "2.0",
    "PII_GUARDRAIL_ANALYZER_TIMEOUT_SECONDS": "240.0",
    "PII_GUARDRAIL_ANALYZER_CONNECT_TIMEOUT_SECONDS": "5.0",
    "PII_GUARDRAIL_ANALYZER_MAX_CONNECTIONS": "20",
    "PII_GUARDRAIL_ANALYZER_MAX_KEEPALIVE_CONNECTIONS": "10",
}


def test_configuration_docs_document_guardrail_dependency_client_env():
    configuration = (ROOT / "docs" / "configuration.md").read_text()

    for name, default in EXPECTED_ENV.items():
        assert f"`{name}` | `{default}`" in configuration


def test_compose_passes_guardrail_dependency_client_env_to_litellm():
    compose = (ROOT / "docker-compose.yml").read_text()

    for name, default in EXPECTED_ENV.items():
        assert f"{name}=${{{name}:-{default}}}" in compose


def test_setup_env_does_not_backfill_guardrail_dependency_client_env():
    setup_script = (ROOT / "scripts" / "setup_env.sh").read_text()

    for name in EXPECTED_ENV:
        assert f'ensure_key_exists "{name}"' not in setup_script

    assert "docs/configuration.md" in setup_script


def test_static_suite_runs_guardrail_dependency_config_regression():
    makefile = (ROOT / "Makefile").read_text()

    assert "tests/test_guardrail_dependency_config.py" in makefile


def test_guardrail_test_runner_installs_redis_dependency():
    requirements = (ROOT / "tests" / "requirements-guardrails.txt").read_text()

    assert "redis>=" in requirements


def test_guardrail_test_runner_uses_pip_available_python_image():
    dockerfile = (ROOT / "tests" / "Dockerfile.guardrails").read_text()
    requirements = (ROOT / "tests" / "requirements-guardrails.txt").read_text()

    assert "FROM python:" in dockerfile
    assert "litellm==" in requirements


def test_litellm_runtime_image_is_versioned_and_digest_pinned():
    dockerfile = (ROOT / "litellm" / "Dockerfile").read_text()
    base_images = [line for line in dockerfile.splitlines() if line.startswith("FROM ")]

    assert base_images == [f"FROM {LITELLM_IMAGE}"]


def test_guardrail_test_runner_matches_litellm_runtime_version():
    requirements = (ROOT / "tests" / "requirements-guardrails.txt").read_text()
    litellm_pins = [
        line for line in requirements.splitlines() if line.startswith("litellm")
    ]

    assert litellm_pins == [f"litellm=={LITELLM_VERSION}"]


def test_e2e_proxy_images_match_pinned_litellm_runtime():
    pre_egress = yaml.safe_load(
        (ROOT / "tests" / "e2e" / "docker-compose.pre-egress-proxy.yml").read_text()
    )
    ner_proxy = yaml.safe_load(
        (ROOT / "tests" / "e2e" / "docker-compose.ner-proxy.yml").read_text()
    )

    assert pre_egress["services"]["litellm"]["image"] == LITELLM_IMAGE
    for service in ("litellm-mask", "litellm-block"):
        assert ner_proxy["services"][service]["image"] == (
            f"${{LITELLM_IMAGE:-{LITELLM_IMAGE}}}"
        )


def test_baseline_ci_runs_guardrail_unit_suite():
    workflow = (ROOT / ".github" / "workflows" / "baseline.yml").read_text()

    assert "make test-guardrail" in workflow


def test_baseline_ci_runs_complete_guardrail_flow_suite():
    workflow = (ROOT / ".github" / "workflows" / "baseline.yml").read_text()

    assert "run: make test-flow" in workflow


def test_docs_explain_guardrail_dependency_client_limits():
    docs = {
        "README.md": (ROOT / "README.md").read_text(),
        "docs/architecture.md": (ROOT / "docs" / "architecture.md").read_text(),
        "docs/monitoring.md": (ROOT / "docs" / "monitoring.md").read_text(),
    }

    for path, text in docs.items():
        assert "PII_GUARDRAIL_REDIS_MAX_CONNECTIONS" in text or (
            "configuration.md" in text
        ), path
        assert "PII_GUARDRAIL_ANALYZER_MAX_CONNECTIONS" in text or (
            "configuration.md" in text
        ), path
        assert (
            "per process" in text or "процесс" in text or ("configuration.md" in text)
        ), path
        assert (
            "event loop" in text or "event-loop" in text or ("configuration.md" in text)
        ), path
        assert "close_guardrail_dependency_clients" in text or (
            "configuration.md" in text
        ), path
