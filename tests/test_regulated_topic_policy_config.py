"""Static checks for regulated-topic AML/CFT policy wiring."""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_env_compose_setup_and_litellm_config_wire_regulated_topic_policy():
    configuration = (ROOT / "docs" / "configuration.md").read_text()
    compose = (ROOT / "docker-compose.yml").read_text()
    setup_script = (ROOT / "scripts" / "setup_env.sh").read_text()
    config = (ROOT / "litellm-config.yaml").read_text()

    assert "`REGULATED_TOPIC_POLICY_MODE` | `off`" in configuration
    assert "`REGULATED_TOPIC_POLICY_EXTRA_RULES_JSON` | Пусто" in configuration
    assert "REGULATED_TOPIC_POLICY_MODE=${REGULATED_TOPIC_POLICY_MODE:-off}" in compose
    assert (
        "REGULATED_TOPIC_POLICY_EXTRA_RULES_JSON=${REGULATED_TOPIC_POLICY_EXTRA_RULES_JSON:-}"
        in compose
    )
    assert 'ensure_key_exists "REGULATED_TOPIC_POLICY_MODE"' not in setup_script
    assert 'ensure_key_exists "REGULATED_TOPIC_POLICY_EXTRA_RULES_JSON"' not in setup_script
    assert "docs/configuration.md" in setup_script
    assert "regulated_topic_policy_mode" in config
    assert "REGULATED_TOPIC_POLICY_MODE" in config
    assert "regulated_topic_policy_extra_rules" in config
    assert "REGULATED_TOPIC_POLICY_EXTRA_RULES_JSON" in config
    assert "по умолчанию off" in config
    assert "до анализа Presidio и вызова провайдера" in config


def test_guardrail_contains_block_only_regulated_topic_policy_pack():
    guardrail = (ROOT / "litellm_guardrails" / "pii_guardrail.py").read_text()

    for required in (
        "REGULATED_TOPIC_POLICY_MODES = {\"block\", \"off\"}",
        "REGULATED_TOPIC_POLICY_ACTIONS = {\"block\"}",
        "REGULATED_TOPIC_POLICY_DEFAULT_RULES",
        "aml_cft_internal_controls",
        "sanctions_watchlist_matching",
        "transaction_monitoring_thresholds",
        "suspicious_activity_playbook",
        "compliance_bypass_procedure",
        "regulated_topic_policy_blocked",
        "ru_regulated_topic_policy_blocked",
        "_classify_regulated_topic_policy_targets",
        "_raise_regulated_topic_policy_blocked",
        "_load_regulated_topic_policy_extra_rules",
    ):
        assert required in guardrail


def test_e2e_non_egress_smoke_covers_regulated_topic_blocks():
    compose = (
        ROOT / "tests" / "e2e" / "docker-compose.pre-egress-proxy.yml"
    ).read_text()
    script = (
        ROOT / "tests" / "e2e" / "test_pre_egress_proxy_non_egress.sh"
    ).read_text()

    assert "REGULATED_TOPIC_POLICY_MODE=block" in compose
    assert "assert_regulated_topic_blocked_error" in script
    assert "run_regulated_topic_blocked_case" in script
    assert "regulated_topic_policy_blocked" in script
    assert "provider_requests" in script
    assert "analyzer_requests 0" in script
    assert "blocked-chat-regulated-topic" in script
    assert "sanctions_watchlist_matching" in script


def test_docs_explain_regulated_topic_policy_boundaries():
    docs = {
        "README.md": (ROOT / "README.md").read_text(),
        "docs/architecture.md": (ROOT / "docs" / "architecture.md").read_text(),
        "docs/examples.md": (ROOT / "docs" / "examples.md").read_text(),
        "docs/monitoring.md": (ROOT / "docs" / "monitoring.md").read_text(),
        "docs/compliance.md": (ROOT / "docs" / "compliance.md").read_text(),
    }

    for path in ("docs/architecture.md", "docs/examples.md", "docs/monitoring.md", "docs/compliance.md"):
        text = docs[path]
        assert "REGULATED_TOPIC_POLICY_MODE" in text, path
        assert "regulated_topic_policy_blocked" in text, path
        assert "ru_regulated_topic_policy_blocked" in text, path
        assert "ПОД/ФТ" in text or "AML/CFT" in text, path
        assert (
            "не является распознавателем персональных данных" in text
            or "Это не персональные данные" in text
        ), path
        assert "исход" in text, path

    assert "REGULATED_TOPIC_POLICY_MODE" in docs["README.md"]
    assert "docs/compliance.md" in docs["README.md"]


def test_static_suite_runs_regulated_topic_policy_regression():
    makefile = (ROOT / "Makefile").read_text()

    assert "tests/test_regulated_topic_policy_config.py" in makefile
