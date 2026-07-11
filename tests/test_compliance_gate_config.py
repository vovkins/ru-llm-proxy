"""Static checks for compliance-oriented egress and observability gates."""

from pathlib import Path
import re


ROOT = Path(__file__).resolve().parents[1]


def _make_target_body(makefile: str, target: str) -> str:
    match = re.search(
        rf"^{re.escape(target)}:\n(?P<body>(?:\t.*\n)+)",
        makefile,
        re.MULTILINE,
    )
    assert match is not None, f"{target} target not found"
    return match.group("body")


def test_makefile_exposes_separate_compliance_gates():
    makefile = (ROOT / "Makefile").read_text()

    egress_body = _make_target_body(makefile, "test-egress-security")
    observability_body = _make_target_body(makefile, "test-observability-gates")

    assert "test-egress-security" in makefile
    assert "test-observability-gates" in makefile
    assert "test-pre-egress-proxy" in egress_body
    assert "test-final-leak-proxy" in egress_body
    assert "tests/test_compliance_gate_config.py" in observability_body
    assert "tests/test_compliance_gate_config.py" in makefile


def test_ci_keeps_egress_and_observability_statuses_separate():
    workflow = (ROOT / ".github" / "workflows" / "baseline.yml").read_text()

    assert "make -n test-egress-security" in workflow
    assert "make -n test-observability-gates" in workflow
    assert "\n  pre-egress-proxy:" in workflow
    assert "\n  final-leak-proxy-smoke:" in workflow
    assert "\n  observability-gates:" in workflow
    assert "run: make test-pre-egress-proxy" in workflow
    assert "run: make test-final-leak-proxy" in workflow
    assert "run: make test-observability-gates" in workflow


def test_mock_smokes_assert_provider_capture_and_log_safety():
    pre_egress = (
        ROOT / "tests" / "e2e" / "test_pre_egress_proxy_non_egress.sh"
    ).read_text()
    final_leak = (
        ROOT / "tests" / "e2e" / "test_final_leak_proxy_non_egress.sh"
    ).read_text()
    mock = (ROOT / "tests" / "e2e" / "mock_openai_upstream.py").read_text()

    for script in (pre_egress, final_leak):
        assert "capture_counts" in script
        assert "expect_no_provider_posts" in script
        assert "assert_litellm_logs_do_not_contain" in script
        assert "docker compose -p \"$PROJECT_NAME\"" in script
        assert "logs --no-color litellm" in script

    assert "blocked-chat-access-log" in pre_egress
    assert "log_or_stacktrace_payload" in pre_egress
    assert "provider_saw_raw_phone false" in final_leak
    assert "provider_saw_phone_placeholder true" in final_leak
    assert '"provider_requests": 0' in mock
    assert '"provider_request_paths": []' in mock
    assert '"provider_saw_raw_phone": False' in mock


def test_docs_define_compliance_gate_boundaries():
    readme = (ROOT / "README.md").read_text()
    monitoring = (ROOT / "docs" / "monitoring.md").read_text()
    compliance = (ROOT / "docs" / "compliance.md").read_text()

    for text in (readme, monitoring):
        assert "docs/compliance.md" in text
        assert "test-egress-security" in text
        assert "test-observability-gates" in text

    for required in (
        "Egress-security gate",
        "Observability gate",
        "Live-provider smoke",
        "make test-egress-security",
        "make test-observability-gates",
        "make guardrails-smoke",
        "не доказывает отсутствие утечки",
        "pii-full-profile",
        "config-env-block",
        "logs-block",
        "dlp-canary-leak",
        "gateway_guardrail_audit",
        "policy_result",
        "redaction_count",
        "error_code",
        "#29",
        "#31",
    ):
        assert required in compliance

    assert "будет реализован в #29" not in compliance
