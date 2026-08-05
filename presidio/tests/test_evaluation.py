"""Tests for the sanitized NER migration quality harness."""

import json
from pathlib import Path

import pytest

from presidio.evaluation import run_baseline
from presidio.evaluation.corpus import (
    DEFAULT_CORPUS_PATH,
    TARGET_ENTITY_TYPES,
    CorpusCase,
    EntitySpan,
    load_corpus,
    parse_annotated_text,
)
from presidio.evaluation.metrics import evaluate_predictions
from presidio.evaluation.run_baseline import (
    evaluate_quality_gate,
    render_markdown_report,
)
from presidio.evaluation.review_deterministic_rules import (
    DEFAULT_CORPUS_PATH as DETERMINISTIC_CORPUS_PATH,
    DEFAULT_INVENTORY_PATH as DETERMINISTIC_INVENTORY_PATH,
    DEFAULT_MANIFEST_PATH as DETERMINISTIC_MANIFEST_PATH,
    DEFAULT_REPORT_PATH as DETERMINISTIC_REPORT_PATH,
    load_corpus as load_deterministic_corpus,
)


def _case(
    case_id: str,
    text: str,
    expected: tuple[EntitySpan, ...],
    *,
    critical: bool = False,
) -> CorpusCase:
    return CorpusCase(
        case_id=case_id,
        text=text,
        expected=expected,
        critical=critical,
        tags=("positive", "unit"),
    )


def test_parse_annotated_text_derives_python_character_offsets():
    annotated = "👩‍💻 Клиент {{PERSON:Ива\u0301н Петров}}, login={{LOGIN:user.01}}"

    text, entities = parse_annotated_text(annotated)

    assert text == "👩‍💻 Клиент Ива\u0301н Петров, login=user.01"
    assert [text[entity.start : entity.end] for entity in entities] == [
        "Ива\u0301н Петров",
        "user.01",
    ]
    assert [entity.entity_type for entity in entities] == ["PERSON", "LOGIN"]


@pytest.mark.parametrize(
    "annotated",
    [
        "text {{PERSON:}}",
        "text {{UNKNOWN:value}}",
        "text {{PERSON:value}",
        "text PERSON:value}}",
    ],
)
def test_parse_annotated_text_rejects_invalid_markers(annotated):
    with pytest.raises(ValueError):
        parse_annotated_text(annotated)


def test_default_corpus_covers_all_target_types_and_risk_classes():
    cases = load_corpus(DEFAULT_CORPUS_PATH)
    covered_types = {
        entity.entity_type
        for case in cases
        for entity in case.expected
    }

    assert len(cases) >= 50
    assert covered_types == TARGET_ENTITY_TYPES
    assert any(case.critical for case in cases)
    assert any("negative" in case.tags for case in cases)
    assert any("unicode" in case.tags for case in cases)
    assert any("long_input" in case.tags for case in cases)
    assert all(
        case.text[entity.start : entity.end]
        for case in cases
        for entity in case.expected
    )


def test_load_corpus_rejects_duplicate_ids(tmp_path):
    record = {
        "id": "duplicate",
        "annotated_text": "{{PERSON:Иван Петров}}",
        "tags": ["positive"],
    }
    corpus_path = tmp_path / "corpus.jsonl"
    corpus_path.write_text(
        json.dumps(record, ensure_ascii=False)
        + "\n"
        + json.dumps(record, ensure_ascii=False)
        + "\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="unique"):
        load_corpus(corpus_path)


def test_metrics_distinguish_exact_typed_and_masking_coverage():
    login_text = "login=alice"
    login_expected = EntitySpan("LOGIN", 6, 11)
    token_text = "AUTH_TOKEN=synthetic-token-01"
    token_expected = EntitySpan("AUTH_TOKEN", 11, len(token_text))
    cases = (
        _case("login", login_text, (login_expected,)),
        _case("token", token_text, (token_expected,), critical=True),
    )
    predictions = {
        "login": [
            {"entity_type": "LOGIN", "start": 0, "end": len(login_text), "text": login_text}
        ],
        "token": [
            {"entity_type": "API_KEY", "start": 0, "end": len(token_text), "text": token_text}
        ],
    }

    report = evaluate_predictions(cases, predictions)

    assert report["aggregate"]["true_positives"] == 0
    assert report["aggregate"]["typed_covered"] == 1
    assert report["aggregate"]["masking_covered"] == 2
    assert report["aggregate"]["critical_covered"] == 1
    assert report["aggregate"]["critical_coverage_recall"] == 1.0
    assert report["prediction_type_counts"] == {"API_KEY": 1, "LOGIN": 1}
    assert report["critical_misses"] == []


def test_metrics_report_critical_miss_without_raw_value():
    raw_value = "do-not-copy-this-synthetic-value"
    text = f"secret={raw_value}"
    expected = EntitySpan("SECRET_KEY", len("secret="), len(text))
    cases = (_case("critical_secret", text, (expected,), critical=True),)

    report = evaluate_predictions(cases, {"critical_secret": []})
    serialized = json.dumps(report, ensure_ascii=False)

    assert report["critical_misses"] == [
        {"case_id": "critical_secret", "entity_type": "SECRET_KEY"}
    ]
    assert raw_value not in serialized
    assert text not in serialized


def test_metrics_reject_prediction_outside_source_text():
    cases = (_case("person", "Иван", (EntitySpan("PERSON", 0, 4),)),)

    with pytest.raises(ValueError, match="outside"):
        evaluate_predictions(
            cases,
            {"person": [{"entity_type": "PERSON", "start": 0, "end": 5}]},
        )


def test_evaluation_accepts_clean_versioned_worktree(monkeypatch):
    monkeypatch.setattr(run_baseline, "_git_revision", lambda: "a" * 40)
    monkeypatch.setattr(run_baseline, "_git_worktree_dirty", lambda: False)

    metadata = run_baseline._validated_git_metadata(
        allow_dirty_worktree=False,
    )

    assert metadata == {
        "git_revision": "a" * 40,
        "git_worktree_dirty": False,
    }


def test_evaluation_rejects_dirty_worktree_by_default(monkeypatch):
    monkeypatch.setattr(run_baseline, "_git_revision", lambda: "a" * 40)
    monkeypatch.setattr(run_baseline, "_git_worktree_dirty", lambda: True)

    with pytest.raises(RuntimeError, match="uncommitted changes"):
        run_baseline._validated_git_metadata(allow_dirty_worktree=False)


def test_evaluation_allows_explicit_dirty_diagnostic_run(monkeypatch):
    monkeypatch.setattr(run_baseline, "_git_revision", lambda: "a" * 40)
    monkeypatch.setattr(run_baseline, "_git_worktree_dirty", lambda: True)

    metadata = run_baseline._validated_git_metadata(
        allow_dirty_worktree=True,
    )

    assert metadata["git_worktree_dirty"] is True


@pytest.mark.parametrize(
    "revision, dirty, message",
    [
        ("unknown", False, "revision is unavailable"),
        ("a" * 40, None, "worktree state is unavailable"),
    ],
)
def test_evaluation_rejects_unknown_git_provenance(
    monkeypatch,
    revision,
    dirty,
    message,
):
    monkeypatch.setattr(run_baseline, "_git_revision", lambda: revision)
    monkeypatch.setattr(run_baseline, "_git_worktree_dirty", lambda: dirty)

    with pytest.raises(RuntimeError, match=message):
        run_baseline._validated_git_metadata(allow_dirty_worktree=True)


def test_markdown_report_contains_metrics_but_no_case_text():
    raw_value = "synthetic-password-value"
    text = f"password={raw_value}"
    cases = (
        _case(
            "password_case",
            text,
            (EntitySpan("PASSWORD", len("password="), len(text)),),
            critical=True,
        ),
    )
    report = {
        "metadata": {
            "system": "unit",
            "git_revision": "abc123",
            "git_worktree_dirty": True,
            "corpus_sha256": "0" * 64,
            "case_count": 1,
            "generated_at": "2026-01-01T00:00:00+00:00",
            "analyzer_health": {
                "status": "ok",
                "ner": "loaded",
                "ner_state": "ready",
                "ner_warmed_up": True,
            },
        },
        "metrics": evaluate_predictions(cases, {"password_case": []}),
        "case_errors": [
            {"case_id": "password_case", "error_type": "RuntimeError"}
        ],
    }

    markdown = render_markdown_report(report)

    assert "`PASSWORD`" in markdown
    assert "password_case" in markdown
    assert "`RuntimeError`" in markdown
    assert "с незакоммиченными изменениями" in markdown
    assert raw_value not in markdown
    assert text not in markdown


def test_checked_in_candidate_passes_explicit_quality_gate():
    reports_dir = DEFAULT_CORPUS_PATH.parent.parent / "reports"
    candidate = json.loads(
        (reports_dir / "huggingface-candidate.json").read_text(encoding="utf-8")
    )
    baseline = json.loads(
        (reports_dir / "deeppavlov-baseline.json").read_text(encoding="utf-8")
    )

    checks = evaluate_quality_gate(candidate["metrics"], baseline["metrics"])

    assert checks
    assert all(check["passed"] for check in checks)


def test_quality_gate_reports_credential_recall_regression():
    reports_dir = DEFAULT_CORPUS_PATH.parent.parent / "reports"
    candidate = json.loads(
        (reports_dir / "huggingface-candidate.json").read_text(encoding="utf-8")
    )
    baseline = json.loads(
        (reports_dir / "deeppavlov-baseline.json").read_text(encoding="utf-8")
    )
    candidate["metrics"]["per_entity"]["AUTH_TOKEN"]["recall"] = 0.94

    checks = evaluate_quality_gate(candidate["metrics"], baseline["metrics"])

    failed = [check["name"] for check in checks if not check["passed"]]
    assert failed == ["AUTH_TOKEN.recall"]


def test_deterministic_rule_source_manifest_pins_reviewed_files_and_licenses():
    manifest = json.loads(DETERMINISTIC_MANIFEST_PATH.read_text(encoding="utf-8"))

    assert manifest["model_repository"]["revision"] == (
        "52b5b0745aac14f73fcf2ac0f91d9b5001a85ae4"
    )
    assert manifest["model_repository"]["declared_license"] == "Apache-2.0"
    assert manifest["model_repository"]["license_file_present"] is False
    assert len(manifest["model_repository"]["files"]) == 7
    assert all(
        len(file_record["sha256"]) == 64
        for file_record in manifest["model_repository"]["files"]
    )
    assert manifest["gitleaks_upstream"]["license"] == "MIT"
    assert manifest["gitleaks_upstream"]["matches_model_repository_file"] is True
    assert manifest["review_policy"]["runtime_integration_approved"] is False


def test_deterministic_rule_review_corpus_covers_all_risk_families():
    cases = load_deterministic_corpus(DETERMINISTIC_CORPUS_PATH)

    assert len(cases) >= 70
    assert {case.family for case in cases} == {
        "all",
        "cli",
        "contract",
        "gitleaks",
        "kv",
        "opaque",
    }
    assert any("placeholder" in case.tags for case in cases)
    assert any("stress" in case.tags for case in cases)
    assert any("hash" in case.tags for case in cases)
    assert any("unsupported_source_command" in case.tags for case in cases)
    assert max(len(case.text) for case in cases) >= 4000


def test_deterministic_rule_inventory_reviews_every_source_rule_without_regex_copy():
    inventory = json.loads(DETERMINISTIC_INVENTORY_PATH.read_text(encoding="utf-8"))
    records = inventory["rules"]

    assert inventory["counts"]["source_rules"] == 222
    assert len(records) == 222
    assert len({record["source_id"] for record in records}) == 222
    assert sum(inventory["counts"]["recommendations"].values()) == 222
    assert inventory["counts"]["model_loader"] == {
        "excluded": 31,
        "incompatible": 23,
        "loaded": 168,
    }
    assert {record["recommendation"] for record in records} == {
        "adapt",
        "already_covered",
        "reject",
        "requires_evidence",
    }
    assert all("regex" not in record and "description" not in record for record in records)
    assert inventory["policy"]["active_runtime_rules_changed"] is True
    assert inventory["policy"]["selected_formats_adapted"] is True
    assert inventory["policy"]["approval_required_before_integration"] is False


def test_deterministic_rule_report_records_isolated_risks_without_values():
    report = json.loads(DETERMINISTIC_REPORT_PATH.read_text(encoding="utf-8"))
    serialized = json.dumps(report, ensure_ascii=False)

    assert report["metadata"]["case_count"] >= 70
    assert report["metadata"]["source_verified"] is True
    assert report["metadata"]["active_runtime_rules_changed"] is True
    assert report["source_loader"]["loaded_gitleaks_rules"] == 168
    assert report["source_loader"]["global_allowlist_applied"] is False
    assert report["source_loader"]["rule_allowlists_applied"] is False
    assert report["per_family"]["cli"]["model_repository_family_only"][
        "exact_recall"
    ] < 1.0
    assert report["per_family"]["opaque"]["model_repository_family_only"][
        "false_positive_predictions"
    ] >= 5
    assert report["per_family"]["kv"]["current_project_rules"]["exact_f1"] == 1.0
    assert report["per_family"]["gitleaks"]["current_project_rules"]["exact_f1"] == 1.0
    assert report["per_family"]["cli"]["current_project_rules"]["exact_recall"] == 1.0
    assert report["per_family"]["opaque"]["current_project_rules"]["predicted_count"] == 0
    assert "SyntheticCurlPass" not in serialized
    assert "550e8400-e29b-41d4-a716-446655440000" not in serialized
