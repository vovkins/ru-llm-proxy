"""Static checks for PII guardrail dependency client configuration."""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]

EXPECTED_ENV = {
    "PII_GUARDRAIL_REDIS_MAX_CONNECTIONS": "20",
    "PII_GUARDRAIL_REDIS_SOCKET_CONNECT_TIMEOUT_SECONDS": "1.0",
    "PII_GUARDRAIL_REDIS_SOCKET_TIMEOUT_SECONDS": "2.0",
    "PII_GUARDRAIL_ANALYZER_TIMEOUT_SECONDS": "30.0",
    "PII_GUARDRAIL_ANALYZER_CONNECT_TIMEOUT_SECONDS": "5.0",
    "PII_GUARDRAIL_ANALYZER_MAX_CONNECTIONS": "20",
    "PII_GUARDRAIL_ANALYZER_MAX_KEEPALIVE_CONNECTIONS": "10",
}


def test_env_example_documents_guardrail_dependency_client_env():
    env_example = (ROOT / ".env.example").read_text()

    for name, default in EXPECTED_ENV.items():
        assert f"{name}={default}" in env_example


def test_compose_passes_guardrail_dependency_client_env_to_litellm():
    compose = (ROOT / "docker-compose.yml").read_text()

    for name, default in EXPECTED_ENV.items():
        assert f"{name}=${{{name}:-{default}}}" in compose


def test_setup_env_backfills_guardrail_dependency_client_env():
    setup_script = (ROOT / "scripts" / "setup_env.sh").read_text()

    for name, default in EXPECTED_ENV.items():
        assert f'ensure_key_exists "{name}" "{default}"' in setup_script


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


def test_baseline_ci_runs_guardrail_unit_suite():
    workflow = (ROOT / ".github" / "workflows" / "baseline.yml").read_text()

    assert "make test-guardrail" in workflow


def test_docs_explain_guardrail_dependency_client_limits():
    docs = {
        "README.md": (ROOT / "README.md").read_text(),
        "docs/architecture.md": (ROOT / "docs" / "architecture.md").read_text(),
        "docs/monitoring.md": (ROOT / "docs" / "monitoring.md").read_text(),
    }

    for path, text in docs.items():
        assert "PII_GUARDRAIL_REDIS_MAX_CONNECTIONS" in text, path
        assert "PII_GUARDRAIL_ANALYZER_MAX_CONNECTIONS" in text, path
        assert "per process" in text or "процесс" in text, path
        assert "event loop" in text or "event-loop" in text, path
        assert "close_guardrail_dependency_clients" in text, path
