"""Static contract for the real-model NER integration gate."""

from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "ner-model-integration.yml"
COMPOSE = ROOT / "tests" / "e2e" / "docker-compose.ner-proxy.yml"
CONFIG = ROOT / "tests" / "e2e" / "litellm-config.ner-proxy.yaml"
SCRIPT = ROOT / "tests" / "e2e" / "test_ner_proxy_flow.sh"
TEST_COVERAGE = ROOT / "docs" / "research" / "ner-migration-test-coverage.md"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_makefile_exposes_separate_real_model_and_proxy_gates():
    makefile = _read(ROOT / "Makefile")

    assert "test-hf-model-run:" in makefile
    assert "docker run --rm --network none $(ANALYZER_IMAGE)" in makefile
    assert "test-ner-proxy:" in makefile
    assert "bash tests/e2e/test_ner_proxy_flow.sh" in makefile
    assert "test-ner-integration:" in makefile
    assert "tests/test_ner_integration_config.py" in makefile


def test_real_model_workflow_is_cached_bounded_and_secret_free():
    workflow = _read(WORKFLOW)
    yaml.safe_load(workflow)

    assert "docker/setup-buildx-action@v4" in workflow
    assert "docker/build-push-action@v7" in workflow
    assert "target: analyzer" in workflow
    assert "load: true" in workflow
    assert "cache-from: type=gha,scope=presidio-analyzer" in workflow
    assert "cache-to: type=gha,mode=max,scope=presidio-analyzer" in workflow
    assert "timeout-minutes: 45" in workflow
    assert "run: make test-hf-model-run" in workflow
    assert "run: make test-ner-proxy" in workflow
    assert "workflow_dispatch:" in workflow
    assert "secrets." not in workflow


def test_proxy_compose_uses_real_analyzer_and_both_pii_modes():
    compose = _read(COMPOSE)
    yaml.safe_load(compose)

    assert "pull_policy: never" in compose
    assert "ru-llm-proxy-presidio-analyzer:latest" in compose
    assert "PRESIDIO_ANALYZER_URL=http://presidio-analyzer:5001" in compose
    assert "PII_GUARDRAIL_MODE=mask" in compose
    assert "PII_GUARDRAIL_MODE=block" in compose
    assert "PII_GUARDRAIL_FAILURE_MODE=fail_closed" in compose
    assert "MOCK_ECHO_CHAT_CONTENT=true" in compose
    assert "PRE_EGRESS_POLICY_MODE=off" in compose
    assert "FINAL_PAYLOAD_LEAK_CHECK_MODE=block" in compose


def test_proxy_config_enables_pre_and_post_call_hooks():
    config = _read(CONFIG)
    parsed = yaml.safe_load(config)

    assert [item["litellm_params"]["mode"] for item in parsed["guardrails"]] == [
        "pre_call",
        "post_call",
    ]


def test_proxy_script_proves_mask_restore_and_block_non_egress():
    script = _read(SCRIPT)

    for value in (
        "Олег Волков",
        "oleg.volkov",
        "Mix3d-Value!",
        "OV-2026/81",
        "ООО Север",
        "Туле",
        "mixed-auth-token-00073",
        "mixed-secret-key-00084",
    ):
        assert value in script

    assert "expect_restored_response" in script
    assert "provider_saw_canary false" in script
    assert "provider_saw_pii_placeholder true" in script
    assert "provider_requests 0" in script
    assert 'block_status" != "422"' in script


def test_baseline_validates_new_workflow_and_shell_assets():
    baseline = _read(ROOT / ".github" / "workflows" / "baseline.yml")

    assert "bash -n tests/e2e/test_ner_proxy_flow.sh" in baseline
    assert '"tests/e2e/litellm-config.ner-proxy.yaml"' in baseline
    assert '"tests/e2e/docker-compose.ner-proxy.yml"' in baseline
    assert '".github/workflows/ner-model-integration.yml"' in baseline
    assert "make -n test-hf-model-run" in baseline
    assert "make -n test-ner-proxy" in baseline


def test_coverage_matrix_documents_all_test_layers_and_entity_types():
    coverage = _read(TEST_COVERAGE)

    for section in (
        "## Матрица покрытия",
        "### Быстрый обязательный контур",
        "### Контур с настоящей моделью",
        "### Холодная проверка",
        "## Критерии успешности",
    ):
        assert section in coverage

    for entity_type in (
        "PERSON",
        "LOCATION",
        "ORGANIZATION",
        "LOGIN",
        "PASSWORD",
        "AUTH_TOKEN",
        "SECRET_KEY",
        "CONTRACT_NUMBER",
    ):
        assert f"`{entity_type}`" in coverage

    assert "make test-ner-integration" in coverage
    assert "--network none" in coverage
    assert "45 минутами" in coverage
