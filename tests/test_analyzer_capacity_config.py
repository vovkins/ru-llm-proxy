"""Static checks for Presidio Analyzer capacity configuration."""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_analyzer_dockerfile_uses_uvicorn_worker_setting():
    dockerfile = (ROOT / "presidio" / "Dockerfile").read_text()

    assert "PRESIDIO_ANALYZER_WORKERS" in dockerfile
    assert "COPY capacity.py" in dockerfile
    assert "uvicorn" in dockerfile
    assert "--workers" in dockerfile


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
