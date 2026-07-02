"""Static checks for final provider-bound leak-check wiring."""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_env_compose_and_smoke_wire_final_payload_leak_check():
    compose = (
        ROOT / "tests" / "e2e" / "docker-compose.pre-egress-proxy.yml"
    ).read_text()
    script = (
        ROOT / "tests" / "e2e" / "test_final_leak_proxy_non_egress.sh"
    ).read_text()
    mock = (ROOT / "tests" / "e2e" / "mock_openai_upstream.py").read_text()

    assert "FINAL_PAYLOAD_LEAK_CHECK_MODE=block" in compose
    assert "FINAL_PAYLOAD_LEAK_CHECK_CANARIES=RU_PROXY_FINAL_CANARY" in compose
    assert "final_payload_leak_check_blocked" in script
    assert "provider_saw_canary" in script
    assert "tool_schema_payload" in script
    assert "analyzer_saw_canary false" in script
    assert "grep -Fq" in script
    assert "provider_saw_phone_placeholder" in script
    assert '"/v1/responses"' in mock
    assert "provider_saw_private_key_marker" in mock


def test_makefile_and_baseline_wire_final_payload_leak_check_smoke():
    makefile = (ROOT / "Makefile").read_text()
    workflow = (ROOT / ".github" / "workflows" / "baseline.yml").read_text()

    assert "test-final-leak-proxy:" in makefile
    assert "bash tests/e2e/test_final_leak_proxy_non_egress.sh" in makefile
    assert "tests/test_final_payload_leak_check_config.py" in makefile
    assert "bash -n tests/e2e/test_final_leak_proxy_non_egress.sh" in workflow
    assert "make -n test-final-leak-proxy" in workflow


def test_docs_document_final_payload_leak_check():
    readme = (ROOT / "README.md").read_text()
    examples = (ROOT / "docs" / "examples.md").read_text()

    for text in (readme, examples):
        assert "FINAL_PAYLOAD_LEAK_CHECK_MODE" in text
        assert "FINAL_PAYLOAD_LEAK_CHECK_CANARIES" in text
        assert "final_payload_leak_check_blocked" in text
        assert "ru_final_payload_leak_check_blocked_total" in text
