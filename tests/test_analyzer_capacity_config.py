"""Static checks for Presidio Analyzer capacity configuration."""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
OVERLOAD_DOCS = ("README.md", "docs/architecture.md", "docs/monitoring.md")


def test_analyzer_dockerfile_uses_uvicorn_worker_setting():
    dockerfile = (ROOT / "presidio" / "Dockerfile").read_text()

    assert "PRESIDIO_ANALYZER_WORKERS" in dockerfile
    assert "COPY capacity.py" in dockerfile
    assert 'CMD ["sh", "-c", "exec uvicorn analyzer_server:app' in dockerfile
    assert "--workers" in dockerfile
    assert '\\"$PRESIDIO_ANALYZER_WORKERS\\"' in dockerfile
    assert "CMD uvicorn " not in dockerfile


def test_compose_exposes_analyzer_capacity_env():
    compose = (ROOT / "docker-compose.yml").read_text()

    assert "PRESIDIO_ANALYZER_CONCURRENCY_LIMIT" in compose
    assert "PRESIDIO_ANALYZER_QUEUE_LIMIT" in compose
    assert "PRESIDIO_ANALYZER_QUEUE_TIMEOUT_SECONDS" in compose


def test_env_example_documents_analyzer_capacity_env():
    env_example = (ROOT / ".env.example").read_text()

    assert "PRESIDIO_ANALYZER_WORKERS=1" in env_example
    assert "PRESIDIO_ANALYZER_CONCURRENCY_LIMIT=1" in env_example
    assert "PRESIDIO_ANALYZER_QUEUE_LIMIT=8" in env_example
    assert "PRESIDIO_ANALYZER_QUEUE_TIMEOUT_SECONDS=0.25" in env_example


def test_setup_env_backfills_analyzer_capacity_env():
    setup_script = (ROOT / "scripts" / "setup_env.sh").read_text()

    assert 'ensure_key_exists "PRESIDIO_ANALYZER_WORKERS" "1"' in setup_script
    assert 'ensure_key_exists "PRESIDIO_ANALYZER_CONCURRENCY_LIMIT" "1"' in setup_script
    assert 'ensure_key_exists "PRESIDIO_ANALYZER_QUEUE_LIMIT" "8"' in setup_script
    assert (
        'ensure_key_exists "PRESIDIO_ANALYZER_QUEUE_TIMEOUT_SECONDS" "0.25"'
        in setup_script
    )


def test_static_tests_are_wired_into_makefile_and_baseline_ci():
    makefile = (ROOT / "Makefile").read_text()
    workflow = (ROOT / ".github" / "workflows" / "baseline.yml").read_text()

    assert "test-static:" in makefile
    assert "tests/test_analyzer_capacity_config.py" in makefile
    assert "presidio/tests/test_capacity.py" in makefile
    assert "make -n test-static" in workflow
    assert "make test-static" in workflow


def test_docs_describe_analyzer_overload_as_fail_closed_override():
    for path in OVERLOAD_DOCS:
        text = (ROOT / path).read_text()
        overload_blocks = [
            block
            for block in text.split("\n\n")
            if (
                "analyzer_overloaded" in block
                or "Analyzer overload" in block
                or "перегрузке Analyzer" in block
            )
        ]
        assert overload_blocks, path

        joined = "\n".join(overload_blocks)
        assert "PII_GUARDRAIL_FAILURE_MODE" in joined
        assert "fail-closed" in joined or "fail_closed" in joined
        assert "независимо" in joined or "regardless" in joined
        assert "может пройти без маскирования" not in joined
        assert "может продолжить запрос без маскирования" not in joined


def test_monitoring_docs_list_analyzer_overload_log_event():
    monitoring = (ROOT / "docs" / "monitoring.md").read_text()

    assert "pii_guardrail_analyzer_overloaded" in monitoring
    assert 'operation="analyzer_overloaded"' in monitoring
