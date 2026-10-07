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


def test_existing_gate_runs_real_disconnect_checks():
    makefile = (ROOT / "Makefile").read_text()
    target = makefile.split("test-final-leak-proxy:\n", 1)[1].split("\n\n", 1)[0]
    assert "bash tests/e2e/test_nonstream_disconnect.sh" in target
    workflow = (ROOT / ".github/workflows/baseline.yml").read_text()
    assert "bash -n tests/e2e/test_nonstream_disconnect.sh" in workflow
    assert "run: make test-final-leak-proxy" in workflow
