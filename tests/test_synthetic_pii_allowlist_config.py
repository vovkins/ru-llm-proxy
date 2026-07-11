"""Static checks for synthetic/test PII allowlist wiring."""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_env_compose_setup_and_litellm_config_wire_synthetic_pii_allowlist():
    env_example = (ROOT / ".env.example").read_text()
    compose = (ROOT / "docker-compose.yml").read_text()
    setup_script = (ROOT / "scripts" / "setup_env.sh").read_text()
    config = (ROOT / "litellm-config.yaml").read_text()

    assert "SYNTHETIC_PII_ALLOWLIST_MODE=off" in env_example
    assert "SYNTHETIC_PII_ALLOWLIST_JSON=[]" in env_example
    assert (
        "SYNTHETIC_PII_ALLOWLIST_MODE=${SYNTHETIC_PII_ALLOWLIST_MODE:-off}"
        in compose
    )
    assert (
        "SYNTHETIC_PII_ALLOWLIST_JSON=${SYNTHETIC_PII_ALLOWLIST_JSON:-[]}"
        in compose
    )
    assert 'ensure_key_exists "SYNTHETIC_PII_ALLOWLIST_MODE" "off"' in setup_script
    assert 'ensure_key_exists "SYNTHETIC_PII_ALLOWLIST_JSON" "[]"' in setup_script
    assert "synthetic_pii_allowlist_mode" in config
    assert "synthetic_pii_allowlist_rules" in config
    assert "SYNTHETIC_PII_ALLOWLIST_MODE" in config
    assert "SYNTHETIC_PII_ALLOWLIST_JSON" in config
    assert "off by default" in config


def test_guardrail_contains_safe_synthetic_pii_allowlist_runtime():
    guardrail = (ROOT / "litellm_guardrails" / "pii_guardrail.py").read_text()

    for required in (
        'SYNTHETIC_PII_ALLOWLIST_MODES = {"allow", "off"}',
        'SYNTHETIC_PII_ALLOWLIST_POLICIES = {"pii"}',
        "SYNTHETIC_PII_ALLOWLIST_SAFE_PATTERN_MARKERS",
        "SYNTHETIC_PII_ALLOWLIST_MAX_PATTERN_LENGTH",
        "ru_synthetic_pii_allowlist_hits",
        "_load_synthetic_pii_allowlist_rules",
        "_is_safe_synthetic_allowlist_pattern",
        "_filter_synthetic_pii_allowlisted_entities",
        "_synthetic_pii_allowlist_audit_fields",
        "synthetic_pii_allowlist_applied",
        "SYNTHETIC_PII_ALLOWLIST_JSON",
    ):
        assert required in guardrail

    assert '^.*$' in guardrail
    assert "pattern.fullmatch(value)" in guardrail


def test_docs_explain_synthetic_pii_allowlist_boundaries():
    docs = {
        "README.md": (ROOT / "README.md").read_text(),
        "docs/architecture.md": (ROOT / "docs" / "architecture.md").read_text(),
        "docs/examples.md": (ROOT / "docs" / "examples.md").read_text(),
        "docs/monitoring.md": (ROOT / "docs" / "monitoring.md").read_text(),
        "docs/compliance.md": (ROOT / "docs" / "compliance.md").read_text(),
    }

    for path, text in docs.items():
        assert "SYNTHETIC_PII_ALLOWLIST_MODE" in text, path
        assert "SYNTHETIC_PII_ALLOWLIST_JSON" in text, path
        assert "ru_synthetic_pii_allowlist_hits" in text, path
        assert "raw" in text, path

    for path, text in docs.items():
        assert "production" in text.lower(), path
        assert "synthetic/test" in text.lower(), path

    assert "example.test" in docs["README.md"]
    assert "example.test" in docs["docs/examples.md"]
    assert "^.*$" in docs["docs/examples.md"]


def test_static_suite_runs_synthetic_pii_allowlist_regression():
    makefile = (ROOT / "Makefile").read_text()

    assert "tests/test_synthetic_pii_allowlist_config.py" in makefile
