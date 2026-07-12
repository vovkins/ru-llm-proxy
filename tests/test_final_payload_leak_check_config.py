"""Static checks for final provider-bound leak-check wiring."""

import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _runtime_provider_bound_fields() -> tuple[str, ...]:
    source = (ROOT / "litellm_guardrails" / "pii_guardrail.py").read_text()
    tree = ast.parse(source)
    assignments = {
        node.targets[0].id: ast.literal_eval(node.value)
        for node in tree.body
        if isinstance(node, ast.Assign)
        and len(node.targets) == 1
        and isinstance(node.targets[0], ast.Name)
        and node.targets[0].id.startswith("FINAL_PAYLOAD_LEAK_CHECK_PROVIDER_BOUND")
    }
    return (
        *assignments["FINAL_PAYLOAD_LEAK_CHECK_PROVIDER_BOUND_REQUEST_FIELDS"],
        *assignments["FINAL_PAYLOAD_LEAK_CHECK_PROVIDER_BOUND_FIELDS"],
    )


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
    assert "tool_schema_key_payload" in script
    assert "chat_image_url_payload" in script
    assert "responses_image_url_payload" in script
    assert "extra_body_payload" in script
    assert "extra_body_key_secret_payload" in script
    assert "extra_body_password_payload" in script
    assert "prompt_cache_key_payload" in script
    assert "safety_identifier_payload" in script
    assert "web_search_options_payload" in script
    assert "user_payload" in script
    assert "metadata_payload" in script
    assert "stop_payload" in script
    assert "messages_stop_sequences_payload" in script
    assert "tool_schema_secret_payload" in script
    assert "messages_tool_use_payload" in script
    assert "messages_tool_use_name_payload" in script
    assert "expect_no_provider_posts" in script
    assert "provider_request_paths" in script
    assert "analyzer_saw_canary false" in script
    assert "grep -Fq" in script
    assert "provider_saw_phone_placeholder" in script
    assert "for key, item in value.items()" in mock
    assert '"provider_request_paths": []' in mock
    assert "_record_provider_payload(self.path" in mock
    assert '"/v1/responses"' in mock
    assert '"/v1/messages"' in mock
    assert "provider_saw_private_key_marker" in mock


def test_makefile_and_baseline_wire_final_payload_leak_check_smoke():
    makefile = (ROOT / "Makefile").read_text()
    workflow = (ROOT / ".github" / "workflows" / "baseline.yml").read_text()

    assert "test-final-leak-proxy:" in makefile
    assert "bash tests/e2e/test_final_leak_proxy_non_egress.sh" in makefile
    assert "tests/test_final_payload_leak_check_config.py" in makefile
    assert "bash -n tests/e2e/test_final_leak_proxy_non_egress.sh" in workflow
    assert "make -n test-final-leak-proxy" in workflow
    assert "final-leak-proxy-smoke:" in workflow
    assert "needs: baseline" in workflow
    assert "Run final payload leak-check proxy smoke" in workflow
    assert "run: make test-final-leak-proxy" in workflow


def test_docs_document_final_payload_leak_check():
    readme = (ROOT / "README.md").read_text()
    examples = (ROOT / "docs" / "examples.md").read_text()
    architecture = (ROOT / "docs" / "architecture.md").read_text()
    monitoring = (ROOT / "docs" / "monitoring.md").read_text()
    litellm_config = (ROOT / "litellm-config.yaml").read_text()

    assert "FINAL_PAYLOAD_LEAK_CHECK_MODE" in readme
    assert "docs/examples.md" in readme

    for text in (examples, monitoring):
        assert "FINAL_PAYLOAD_LEAK_CHECK_MODE" in text
        assert "FINAL_PAYLOAD_LEAK_CHECK_CANARIES" in text
        assert "final_payload_leak_check_blocked" in text
        assert "ru_final_payload_leak_check_blocked_total" in text

    assert "FINAL_PAYLOAD_LEAK_CHECK_MODE" in architecture
    assert "final_payload_leak_check_blocked" in architecture

    documented_surfaces = (examples, architecture, litellm_config)
    for field in _runtime_provider_bound_fields():
        for text in documented_surfaces:
            assert field in text, field

    assert "Anthropic Messages `system`" in architecture
    assert "legacy `functions`" in architecture
    assert "extra_body" in architecture
    assert "stop_sequences" in architecture
    assert "env-secret-like" in examples
    assert "откатывает masked text" in architecture
    assert "ключевые вопросы" in monitoring
    assert "10. Если есть regression" in monitoring
    assert "grep -E '^(litellm_|ru_)'" in examples
