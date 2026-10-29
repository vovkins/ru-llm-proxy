"""Keep non-streaming cancellation enabled in shipped and test profiles."""

from pathlib import Path

import pytest
import yaml


ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("path", [
    "litellm-config.yaml",
    "litellm-config.codex-lb.yaml",
    "tests/e2e/litellm-config.pre-egress-proxy.yaml",
    "tests/e2e/litellm-config.ner-proxy.yaml",
    "tests/load/litellm-config.yaml",
])
def test_stock_disconnect_cancellation_is_enabled(path):
    config = yaml.safe_load((ROOT / path).read_text())
    assert config["general_settings"]["cancel_on_disconnect"] is True
    assert "litellm_guardrails.upstream_stream_adapter" in config["litellm_settings"]["callbacks"]


def test_existing_gate_runs_real_disconnect_checks():
    makefile = (ROOT / "Makefile").read_text()
    target = makefile.split("test-final-leak-proxy:\n", 1)[1].split("\n\n", 1)[0]
    assert "bash tests/e2e/test_nonstream_disconnect.sh" in target
    script = (ROOT / "tests/e2e/test_nonstream_disconnect.sh").read_text()
    assert "check_stream_disconnect.py" in script
    assert "litellm_guardrails.upstream_stream_adapter" in script
    workflow = (ROOT / ".github/workflows/baseline.yml").read_text()
    assert "bash -n tests/e2e/test_nonstream_disconnect.sh" in workflow
    assert "run: make test-final-leak-proxy" in workflow


@pytest.mark.parametrize("path,services", [
    ("docker-compose.yml", ("litellm",)),
    ("tests/e2e/docker-compose.pre-egress-proxy.yml", ("litellm",)),
    ("tests/e2e/docker-compose.ner-proxy.yml", ("litellm-mask", "litellm-block")),
    ("tests/load/docker-compose.yml", ("load-litellm",)),
])
def test_profiles_register_cancellation_without_replacing_native_cli(path, services):
    config = yaml.safe_load((ROOT / path).read_text())
    for service in services:
        assert config["services"][service]["entrypoint"] == ["sh", "/app/litellm_guardrails/entrypoint.sh"]
    source = (ROOT / "litellm_guardrails" / "server.py").read_text()
    assert "app.add_middleware(RequestCancellationMiddleware)" in source
    assert "run_server()" in source and "__mp_main__" in source
    assert "skip_server_startup" not in source and "uvicorn.run" not in source
