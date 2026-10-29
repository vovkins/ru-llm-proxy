"""Static contract for the real-model NER integration gate."""

from dataclasses import replace
import io
import json
from pathlib import Path
import urllib.error

import pytest
import yaml

from presidio.evaluation.corpus import CorpusCase, EntitySpan, TARGET_ENTITY_TYPES, load_corpus
from presidio.evaluation.metrics import evaluate_predictions
from tests.e2e.ner_instruction_checks import CORPUS
from tests.e2e.ner_model_limits import (
    accepted_coverage_cases,
    accepted_registry,
    check_case_result,
    check_login_filter,
)

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
    smoke = _read(ROOT / "tests/e2e/model_smoke.py")
    assert "verify_cpu_runtime.py" in smoke
    assert "verify_triton_service.py" in smoke
    assert "ANALYZER_PROFILE" not in makefile
    assert '"--internal"' in smoke
    assert "offline_probe" in smoke
    assert "test-ner-proxy:" in makefile
    assert "$(NER_TEST) proxy" in makefile
    assert "docker-compose.ner-proxy.gpu.yml" not in _read(SCRIPT)
    assert "reject_removed_settings" in _read(SCRIPT)
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
    assert "timeout-minutes: 60" in workflow
    assert "run: make test-hf-model-run" in workflow
    assert "run: make test-ner-proxy" in workflow
    assert "workflow_dispatch:" in workflow
    assert "secrets." not in workflow


@pytest.mark.parametrize("event", ["pull_request", "push"])
def test_ner_workflow_tracks_actual_litellm_image_definition(event):
    workflow = yaml.load(_read(WORKFLOW), Loader=yaml.BaseLoader)
    paths = workflow["on"][event]["paths"]

    assert (ROOT / "litellm/Dockerfile").is_file()
    assert "litellm/Dockerfile" in paths
    assert "Dockerfile" not in paths


def test_proxy_compose_uses_real_analyzer_and_both_pii_modes():
    compose = _read(COMPOSE)
    yaml.safe_load(compose)

    assert "pull_policy: never" in compose
    assert "ru-llm-proxy-ner-tests:analyzer" in compose
    assert "PRESIDIO_ANALYZER_URL=http://presidio-analyzer:5001" in compose
    assert "PII_GUARDRAIL_MODE=mask" in compose
    assert "PII_GUARDRAIL_MODE=block" in compose
    assert "PII_GUARDRAIL_FAILURE_MODE=fail_closed" in compose
    assert "MOCK_ECHO_CHAT_CONTENT=true" in compose
    assert "MOCK_ECHO_RESPONSES_CONTENT=true" in compose
    assert "PRE_EGRESS_POLICY_MODE=off" in compose
    assert "FINAL_PAYLOAD_LEAK_CHECK_MODE=block" in compose


def test_logistic_policy_runs_full_proxy_gate_in_ci():
    workflow = yaml.safe_load(_read(WORKFLOW))
    steps = workflow["jobs"]["real-model-integration"]["steps"]
    assert any(
        step.get("env", {}).get("PRESIDIO_ANALYZER_BASE64_POLICY") == "logistic"
        and step.get("run") == "bash tests/e2e/test_ner_proxy_flow.sh"
        for step in steps
    )
    assert "PRESIDIO_ANALYZER_BASE64_POLICY=${PRESIDIO_ANALYZER_BASE64_POLICY:-content}" in _read(COMPOSE)


def test_base64_regressions_are_in_the_existing_api_ci_gate():
    target = _read(ROOT / "Makefile").split("test-recognizer-api:", 1)[1].split("\ntest-ner-evaluation:", 1)[0]
    for path in (ROOT / "presidio/tests").glob("test_base64_*.py"):
        assert str(path.relative_to(ROOT)) in target


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
    assert "ner_instruction_checks.py" in script
    assert "instruction_regressions.jsonl" in _read(
        ROOT / "tests/e2e/ner_instruction_checks.py"
    )
    assert "mock-responses" in _read(CONFIG)
    assert "tests/e2e/ner_instruction_checks.py" in _read(WORKFLOW)


def test_name_corpora_gate_requires_complete_critical_spans_and_filter_agreement():
    script = _read(ROOT / "tests/e2e/ner_instruction_checks.py")
    assert "name_field_holdout.jsonl" in script
    assert accepted_registry()["name_field_holdout"] == {}
    assert "check_case_result" in script
    assert "combined name filter differs from full scan" in script
    assert "check_accepted_coverage" in script
    assert "owner_accepted_known_coverage_gaps_not_complete_protection" in script


def _dialogue_email_case():
    return next(case for case in load_corpus(CORPUS) if case.case_id == "dialogue_role_email")


def _prediction(entity_type, start, end):
    return {"entity_type": entity_type, "start": start, "end": end}


@pytest.mark.parametrize("extra", [[], [_prediction("LOGIN", 63, 64)]])
def test_instruction_gate_accepts_only_the_agreed_punctuation_extra_span(extra):
    case = _dialogue_email_case()
    assert case.expected == (EntitySpan("EMAIL_ADDRESS", 34, 63),)
    result = [_prediction("EMAIL_ADDRESS", 34, 63), *extra]
    verdict = check_case_result(case, result, "instruction_regressions")
    assert verdict["expected_masked"] is True
    assert verdict["assessment"] == ("accepted_limitation" if extra else "matches_gold")


@pytest.mark.parametrize("extra", [
    _prediction("PERSON", 76, 80),
    _prediction("LOGIN", 63, 65),
    _prediction("LOGIN", 62, 64),
    _prediction("PERSON", 16, 20),
])
def test_instruction_gate_rejects_any_other_extra_span(extra):
    with pytest.raises(AssertionError, match="unaccepted Analyzer"):
        check_case_result(
            _dialogue_email_case(), [_prediction("EMAIL_ADDRESS", 34, 63), extra], "instruction_regressions"
        )


@pytest.mark.parametrize("email", [[], [_prediction("EMAIL_ADDRESS", 34, 62)]])
def test_instruction_gate_does_not_accept_missing_or_partial_sensitive_value(email):
    with pytest.raises(AssertionError, match="unaccepted Analyzer"):
        check_case_result(
            _dialogue_email_case(), [*email, _prediction("LOGIN", 63, 64)], "instruction_regressions"
        )


@pytest.mark.parametrize("changes", [
    {"text": "Changed example"},
    {"expected": ()},
    {"score_threshold": 0.7},
    {"language": "en"},
    {"critical": False},
    {"tags": ("changed",)},
])
def test_instruction_gate_cannot_reuse_the_limitation_for_changed_source(changes):
    with pytest.raises(AssertionError, match="no longer matches source annotation"):
        check_case_result(
            replace(_dialogue_email_case(), **changes),
            [_prediction("EMAIL_ADDRESS", 34, 63), _prediction("LOGIN", 63, 64)],
            "instruction_regressions",
        )


def test_instruction_gate_does_not_apply_limitation_to_other_cases():
    case = CorpusCase("another_case", "Done", (), False, ("negative",))
    with pytest.raises(AssertionError, match="unaccepted Analyzer"):
        check_case_result(case, [_prediction("PERSON", 0, 4)], "instruction_regressions")


def _limitation_cases():
    for corpus, entries in accepted_registry().items():
        if corpus == "accepted_coverage":
            cases = accepted_coverage_cases()
        else:
            cases = load_corpus(
                ROOT / "presidio/evaluation/data" / (corpus + ".jsonl"),
                required_entity_types=frozenset({"PERSON", "ORGANIZATION"}) if corpus != "instruction_regressions" else TARGET_ENTITY_TYPES,
            )
        for case in cases:
            if case.case_id in entries:
                yield corpus, case, entries[case.case_id]


LIMITATION_CASES = list(_limitation_cases())


def test_accepted_registry_is_finite_and_model_bound():
    assert {key: len(value) for key, value in accepted_registry().items()} == {
        "instruction_regressions": 15, "person_organization_regressions": 1,
        "name_context_regressions": 2, "name_field_holdout": 0, "accepted_coverage": 20,
    }
    assert len(LIMITATION_CASES) == 38


@pytest.mark.parametrize("corpus,case,entry", LIMITATION_CASES, ids=[f"{g}:{c.case_id}" for g, c, _ in LIMITATION_CASES])
def test_every_accepted_outcome_preserves_source_truth_and_allows_a_complete_fix(corpus, case, entry):
    expected = case.expected
    results = [_prediction(*span) for span in entry["accepted"]]
    verdict = check_case_result(case, results, corpus)
    assert verdict["assessment"] == "accepted_limitation"
    assert case.expected == expected
    correct = [_prediction(e.entity_type, e.start, e.end) for e in expected]
    assert check_case_result(case, correct, corpus)["assessment"] == "matches_gold"


@pytest.mark.parametrize("corpus,case,entry", LIMITATION_CASES, ids=[f"{g}:{c.case_id}" for g, c, _ in LIMITATION_CASES])
def test_no_accepted_case_allows_additional_masks_or_changed_ids(corpus, case, entry):
    results = [_prediction(*span) for span in entry["accepted"]]
    with pytest.raises(AssertionError):
        check_case_result(case, [*results, _prediction("PASSWORD", 0, 1)], corpus)
    with pytest.raises(AssertionError):
        check_case_result(replace(case, case_id=case.case_id + "_changed"), results, corpus)


def test_split_name_is_full_union_but_not_exact_or_complete_person_filter():
    corpus, case, entry = next(row for row in LIMITATION_CASES if row[1].case_id == "holdout_person_uppercase")
    results = [_prediction(*span) for span in entry["accepted"]]
    verdict = check_case_result(case, results, corpus)
    assert verdict["coverage"][0]["significant_fully_covered"] is True
    assert verdict["coverage"][0]["typed_significant_fully_covered"] is False
    assert evaluate_predictions([case], {case.case_id: results})["aggregate"]["false_negatives"] == 1
    with pytest.raises(AssertionError):
        check_case_result(case, [r for r in results if r["entity_type"] == "PERSON"], corpus)


def test_known_coverage_gaps_remain_false_negatives_in_raw_metrics():
    cases = accepted_coverage_cases()
    predictions = {
        c.case_id: [_prediction(*s) for s in accepted_registry()["accepted_coverage"][c.case_id]["accepted"]]
        for c in cases
    }
    assert evaluate_predictions(cases, predictions)["aggregate"]["false_negatives"] > 0
    assert any(not v["significant_fully_covered"] for c in cases
               for v in check_case_result(c, predictions[c.case_id], "accepted_coverage")["coverage"])


def test_selective_login_filter_keeps_only_source_bound_accepted_punctuation():
    case = _dialogue_email_case()
    results = [_prediction("EMAIL_ADDRESS", 34, 63), _prediction("LOGIN", 63, 64)]
    check_login_filter(case, results, [_prediction("LOGIN", 34, 64)])
    with pytest.raises(AssertionError, match="LOGIN filter"):
        check_login_filter(case, results, [_prediction("LOGIN", 33, 64)])
    with pytest.raises(AssertionError, match="LOGIN filter"):
        check_login_filter(case, results, [_prediction("PERSON", 34, 64)])


@pytest.mark.parametrize("start,end", [(34, 34), (35, 34), (-1, 2), (34, 200), (True, 64)])
def test_selective_login_filter_rejects_invalid_intervals(start, end):
    case = _dialogue_email_case()
    results = [_prediction("EMAIL_ADDRESS", 34, 63), _prediction("LOGIN", 63, 64)]
    with pytest.raises(AssertionError):
        check_login_filter(case, results, [_prediction("LOGIN", start, end)])


@pytest.mark.parametrize("event", ["pull_request", "push"])
def test_ner_workflow_tracks_accepted_limit_test_dependencies(event):
    workflow = yaml.load(_read(WORKFLOW), Loader=yaml.BaseLoader)
    assert {"tests/e2e/ner_model_limits.py", "tests/fixtures/ner-model-limitations.json"} <= set(workflow["on"][event]["paths"])


def test_accepted_limits_cannot_be_reused_for_another_model(monkeypatch):
    from tests.e2e import ner_model_limits as module
    module.accepted_registry.cache_clear()
    monkeypatch.setattr(module, "MODEL_REVISION", "another-revision")
    try:
        with pytest.raises(AssertionError, match="another model"):
            module.accepted_registry()
    finally:
        module.accepted_registry.cache_clear()


def _mock_accepted_proxy_flow(monkeypatch, case, predictions):
    from tests.e2e import ner_instruction_checks as module
    calls, capture = [], {}

    class Client:
        def __init__(self, *_args, **_kwargs):
            pass

        def analyze(self, value):
            assert value == case
            return predictions

    def request(base, path, payload, *, stream=False, **_kwargs):
        if base == "http://analyzer":
            return {"entities": [p for p in predictions if p["entity_type"] in payload["entities"]]}
        if base == "http://capture":
            assert path == "/capture/reset"
            capture.update(provider_requests=0, provider_saw_canary=False,
                           provider_saw_pii_placeholder=False)
            return {}
        assert base in {"http://mask", "http://block"}
        calls.append((base, path, stream))
        if base == "http://block" and predictions:
            raise urllib.error.HTTPError(base, 422, "known detection", {}, None)
        capture.update(provider_requests=1, provider_saw_canary=False,
                       provider_saw_pii_placeholder=bool(predictions))
        return {"text": case.text}

    monkeypatch.setattr(module, "AnalyzerClient", Client)
    monkeypatch.setattr(module, "_request", request)
    monkeypatch.setattr(module, "_restored_text", lambda response, *_args: response["text"])
    monkeypatch.setattr(module.urllib.request, "urlopen", lambda *_a, **_k: io.BytesIO(json.dumps(capture).encode()))
    return module, calls


def test_name_flow_preserves_union_filters_restoration_and_block(monkeypatch, capsys):
    corpus, case, entry = next(row for row in LIMITATION_CASES if row[1].case_id == "holdout_person_uppercase")
    predictions = [_prediction(*s) for s in entry["accepted"]]
    module, calls = _mock_accepted_proxy_flow(monkeypatch, case, predictions)
    monkeypatch.setattr(module, "_name_cases", lambda: [case])
    module.check_names("http://analyzer", "http://mask", "http://block", "http://capture")
    assert len(calls) == 8
    report = json.loads(capsys.readouterr().out)
    assert report["echo_flows"] == report["blocked_flows"] == 4
    assert report["metrics"]["false_negatives"] == 1
    person = next(r for r in report["accepted_filter_coverage"] if r["entities"] == ["PERSON"])
    assert person["coverage"][0]["covered_significant_characters"] == 8
    assert person["coverage"][0]["significant_characters"] == 12


@pytest.mark.parametrize("unexpected", [False, True])
def test_dialogue_flow_requires_validation_before_accepting_false_positive_block(monkeypatch, capsys, unexpected):
    case = next(c for c in load_corpus(CORPUS) if c.case_id == "dialogue_role_which")
    entry = accepted_registry()["instruction_regressions"][case.case_id]
    predictions = [_prediction(*s) for s in entry["accepted"]]
    if unexpected:
        predictions.append(_prediction("PASSWORD", 0, 1))
    module, calls = _mock_accepted_proxy_flow(monkeypatch, case, predictions)
    monkeypatch.syspath_prepend(str(ROOT / "tests/e2e"))
    monkeypatch.setattr(module, "load_corpus", lambda *_a, **_k: [case])
    if unexpected:
        with pytest.raises(AssertionError, match="unaccepted Analyzer"):
            module.check_dialogue_arguments("http://analyzer", "http://mask", "http://block", "http://capture")
        assert not calls
    else:
        module.check_dialogue_arguments("http://analyzer", "http://mask", "http://block", "http://capture")
        report = json.loads(capsys.readouterr().out)
        assert report["blocked_flows"] == report["echo_flows"] == 4
        assert report["clean_block_flows"] == 0
        assert len(calls) == 8


def test_dkb_corpus_is_part_of_real_analyzer_gate():
    workflow = _read(WORKFLOW)
    assert workflow.count('"tests/e2e/dkb_corpus_checks.py"') == 2
    assert workflow.count('"tests/fixtures/dkb/**"') == 2
    assert "dkb_corpus_checks.py" in _read(SCRIPT)
    assert "MOCK_DKB_CORPUS=true" in _read(COMPOSE)


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
    assert "60 минутами" in coverage
