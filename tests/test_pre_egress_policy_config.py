"""Static checks for pre-egress config/log policy wiring."""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_env_compose_and_setup_wire_pre_egress_policy_mode():
    env_example = (ROOT / ".env.example").read_text()
    compose = (ROOT / "docker-compose.yml").read_text()
    setup_script = (ROOT / "scripts" / "setup_env.sh").read_text()

    assert "PRE_EGRESS_POLICY_MODE=block" in env_example
    assert "PRE_EGRESS_POLICY_MODE=${PRE_EGRESS_POLICY_MODE:-block}" in compose
    assert 'ensure_key_exists "PRE_EGRESS_POLICY_MODE" "block"' in setup_script


def test_litellm_guardrail_info_documents_pre_egress_policy_mode():
    config = (ROOT / "litellm-config.yaml").read_text()

    assert "pre_egress_policy_mode" in config
    assert "PRE_EGRESS_POLICY_MODE" in config
    assert "before Presidio analysis and provider calls" in config


def test_docs_distinguish_pre_egress_policy_from_pii_modes():
    docs = {
        "README.md": (ROOT / "README.md").read_text(),
        "docs/examples.md": (ROOT / "docs" / "examples.md").read_text(),
        "docs/architecture.md": (ROOT / "docs" / "architecture.md").read_text(),
        "docs/monitoring.md": (ROOT / "docs" / "monitoring.md").read_text(),
    }

    for path, text in docs.items():
        assert "PRE_EGRESS_POLICY_MODE" in text, path
        assert "pre_egress_policy_blocked" in text, path
        assert "raw payload" in text, path

    architecture = docs["docs/architecture.md"]
    assert "до `POST /api/v1/analyze`" in architecture
    assert "#28 Secondary DLP scan" in architecture


def test_static_suite_runs_pre_egress_policy_config_regression():
    makefile = (ROOT / "Makefile").read_text()

    assert "tests/test_pre_egress_policy_config.py" in makefile


def test_pre_egress_proxy_smoke_assets_are_wired():
    makefile = (ROOT / "Makefile").read_text()
    workflow = (ROOT / ".github" / "workflows" / "baseline.yml").read_text()
    script = (ROOT / "tests" / "e2e" / "test_pre_egress_proxy_non_egress.sh").read_text()
    compose = (
        ROOT / "tests" / "e2e" / "docker-compose.pre-egress-proxy.yml"
    ).read_text()
    config = (
        ROOT / "tests" / "e2e" / "litellm-config.pre-egress-proxy.yaml"
    ).read_text()

    assert "test-pre-egress-proxy:" in makefile
    assert "bash tests/e2e/test_pre_egress_proxy_non_egress.sh" in makefile
    assert "bash -n tests/e2e/test_pre_egress_proxy_non_egress.sh" in workflow
    assert "make -n test-pre-egress-proxy" in workflow
    assert "mock-upstream" in compose
    assert "PRE_EGRESS_POLICY_MODE=block" in compose
    assert "litellm_guardrails.pii_guardrail.RuPIIGuardrail" in config
    assert "/v1/chat/completions" in script
    assert "provider_requests" in script
    assert "analyzer_requests" in script
