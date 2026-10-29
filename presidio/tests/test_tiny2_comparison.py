"""Evaluation hygiene is tested without importing either inference engine."""
from dataclasses import replace
import json
from pathlib import Path
import pytest
from types import SimpleNamespace

from presidio.evaluation.corpus import CorpusCase, EntitySpan
from presidio.evaluation.tiny2_comparison import (
    ComparisonCase, coverage, intersection_length, manifest, merged_intervals,
    score_results, write_comparison,
)


def case(text="Анна Соколова", spans=None, *, complete=True):
    if spans is None:
        spans = (EntitySpan("PERSON", 0, len(text)),)
    return ComparisonCase(CorpusCase("test_case", text, spans, False, ("diagnostic",)),
                          "test", frozenset({"PERSON", "ORGANIZATION"}), complete)


def result(*spans):
    return {"test_case": {"status": "ok", "entities": [
        {"entity_type": t, "start": a, "end": b} for t, a, b in spans]}}


def test_union_coverage_does_not_require_single_span():
    item = case()
    scored = score_results([item], result(("ORGANIZATION", 0, 5), ("PERSON", 5, 13)))
    assert scored["counts"]["fully_covered"] == 1
    assert scored["counts"]["typed_fully_covered"] == 0
    assert scored["per_entity"]["PERSON"]["recall"] == 0


def test_gap_is_partial_not_full_coverage():
    scored = score_results([case()], result(("PERSON", 0, 4), ("PERSON", 5, 13)))
    assert scored["counts"].get("fully_covered", 0) == 0
    assert scored["counts"]["partially_covered"] == 1
    assert scored["counts"]["covered_gold_characters"] == 12
    assert scored["counts"]["significant_fully_covered"] == 1
    assert scored["counts"]["covered_significant_characters"] == 12


def test_duplicate_overlaps_do_not_inflate_coverage():
    item = case()
    predictions = result(("PERSON", 0, 13), ("PERSON", 0, 13))["test_case"]["entities"]
    assert coverage(item.case, predictions)["masked_characters"] == 13
    scored = score_results([item], result(("PERSON", 0, 13), ("PERSON", 0, 13)))
    assert scored["per_entity"]["PERSON"]["false_positives"] == 1


def test_json_value_widening_counts_excess_only_when_annotation_complete():
    item = case('"prefix Анна suffix"', (EntitySpan("PERSON", 8, 12),))
    prediction = result(("PASSWORD", 1, 19))
    full = score_results([item], prediction)
    partial = score_results([replace(item, complete_annotation=False)], prediction)
    assert full["counts"]["fully_covered"] == 1
    assert full["counts"]["extra_masked_characters"] == 14
    assert "extra_masked_characters" not in partial["counts"]
    assert partial["counts"]["partial_annotation_cases"] == 1


@pytest.mark.parametrize("failure", [None, {"status": "error", "http_status": 503},
                                     {"status": "error", "error_type": "TimeoutError"}])
def test_missing_failed_timeout_is_not_empty_prediction(failure):
    results = {} if failure is None else {"test_case": failure}
    scored = score_results([case()], results)
    assert scored["counts"]["unavailable_cases"] == 1
    assert scored["counts"]["gold_values_in_unavailable_cases"] == 1
    assert scored["counts"].get("false_negatives", 0) == 0
    assert scored["per_entity"]["PERSON"]["recall"] is None


def test_empty_success_is_a_real_miss():
    scored = score_results([case()], result())
    assert scored["counts"]["successful_cases"] == 1
    assert scored["per_entity"]["PERSON"]["false_negatives"] == 1


def test_selective_filter_exposes_first_name_miss():
    scored = score_results([case()], result(("PERSON", 5, 13)))
    assert scored["counts"]["covered_gold_characters"] == 8
    assert scored["counts"]["partially_covered"] == 1
    assert scored["counts"].get("significant_fully_covered", 0) == 0
    assert scored["counts"]["covered_significant_characters"] == 8


def test_unannotated_class_not_false_positive_but_can_protect_value():
    item = replace(case(), scope=frozenset({"PERSON"}))
    scored = score_results([item], result(("ORGANIZATION", 0, 13)))
    assert scored["counts"]["predicted"] == 0
    assert scored["counts"]["fully_covered"] == 1
    assert scored["per_entity"]["PERSON"]["false_negatives"] == 1


def test_negative_false_mask_and_false_block_are_separate():
    item = case("Done.", ())
    scored = score_results([item], result(("PERSON", 0, 3)))
    assert scored["counts"]["negative_cases_with_in_scope_mask"] == 1
    assert scored["counts"]["false_block_cases"] == 1
    assert scored["counts"]["extra_masked_characters"] == 3


@pytest.mark.parametrize("span", [("PERSON", -1, 2), ("PERSON", 0, 14),
                                  ("PERSON", True, 2), ("PERSON", 2, 2), ("", 0, 1)])
def test_bad_prediction_rejected(span):
    with pytest.raises(ValueError):
        score_results([case()], result(span))


def test_duplicate_case_and_unknown_result_rejected():
    with pytest.raises(ValueError):
        score_results([case(), case()], result())
    with pytest.raises(ValueError):
        score_results([case()], {"unknown": {"status": "ok", "entities": []}})


def test_invalid_gold_and_scope_rejected():
    with pytest.raises(ValueError):
        replace(case(), scope=frozenset({"LOCATION"}))
    with pytest.raises(ValueError):
        case(spans=(EntitySpan("PERSON", 0, 100),))


def test_unicode_offsets_are_python_characters():
    item = case("🙂 Ива́н!", (EntitySpan("PERSON", 2, 7),))
    assert score_results([item], result(("PERSON", 2, 7)))["counts"]["covered_gold_characters"] == 5


def test_union_large_ranges_uses_intervals_not_per_character_sets():
    assert merged_intervals([(0, 50000000), (40000000, 100000000)]) == [(0, 100000000)]
    assert intersection_length([(0, 100000000)], [(100, 200)]) == 100


def test_report_has_hashes_but_no_text_or_values(tmp_path):
    output = tmp_path / "result.json"
    write_comparison(output, [case()], result(), result(("PERSON", 0, 13)), {"reference": "fixed"})
    data = json.loads(output.read_text())
    assert "Анна" not in output.read_text()
    assert len(data["manifest"][0]["text_sha256"]) == 64
    assert manifest([case()]) == data["manifest"]


def test_non_ner_class_is_scored_on_negative_cases_too():
    item = ComparisonCase(CorpusCase("test_case", "Код строки 770801001", (), False, ("negative",)),
                          "context", frozenset({"RU_KPP"}))
    scored = score_results([item], result(("RU_KPP", 11, 20)))
    assert scored["per_entity"]["RU_KPP"]["false_positives"] == 1


def test_explicit_scope_does_not_change_legacy_metrics_by_default():
    from presidio.evaluation.metrics import evaluate_predictions
    from presidio.evaluation.corpus import TARGET_ENTITY_TYPES
    item = case()
    default = evaluate_predictions([item.case], {})
    assert set(default["per_entity"]) == TARGET_ENTITY_TYPES
    scoped = evaluate_predictions([item.case], {}, entity_types=frozenset({"PERSON"}))
    assert set(scoped["per_entity"]) == {"PERSON"}
    with pytest.raises(ValueError):
        evaluate_predictions([item.case], {}, entity_types=frozenset({"LOCATION"}))


def test_internal_corpus_inventory_and_duplicate_hashes_are_explicit():
    from presidio.evaluation.tiny2_comparison_data import load_cases
    root = Path(__file__).resolve().parents[2]
    cases, source = load_cases(root)
    assert source["corpus_counts"]["ner_migration"] == 77
    assert source["corpus_counts"]["tiny2_component_study"] == 32
    assert source["corpus_counts"]["dkb_plain"] == source["corpus_counts"]["dkb_json"] == 18
    assert source["corpus_counts"]["windows"] == 24
    assert len(cases) == source["case_count"]
    assert source["unique_texts"] + source["duplicate_text_occurrences"] == len(cases)
    assert not any(c.complete_annotation for c in cases if c.corpus.startswith("dkb_"))


def test_json_dkb_offsets_are_preserved_without_changing_annotations():
    from presidio.evaluation.tiny2_comparison_data import dkb_cases
    root = Path(__file__).resolve().parents[2]
    cases = dkb_cases(root)
    for plain, encoded in zip(cases[::2], cases[1::2]):
        assert json.loads(encoded.case.text) == {"source": plain.case.text}
        for left, right in zip(plain.case.expected, encoded.case.expected):
            literal = json.dumps(plain.case.text[left.start:left.end], ensure_ascii=False)[1:-1]
            assert encoded.case.text[right.start:right.end] == literal


def test_public_label_mapping_preserves_out_of_scope_and_full_address():
    from presidio.evaluation.tiny2_comparison_data import PII_FILES_SHA256, PII_TYPES
    assert PII_TYPES["ADDRESS"] == "RU_ADDRESS"
    assert PII_TYPES["NAME"] == "PERSON"
    assert PII_TYPES["CVC"] == "CVC_OUT_OF_SCOPE"
    assert len(PII_TYPES) == 13
    assert all(len(value) == 64 for value in PII_FILES_SHA256.values())


def _docker_args(tmp_path):
    packet = tmp_path / "cases.json"
    packet.write_text("{}")
    return SimpleNamespace(case_packet=packet, report_dir=tmp_path / "reports", system="candidate", mode="analyzer",
                           reference_image="reference", analyzer_image="candidate", triton_image="triton",
                           nginx_image="nginx")


def test_docker_study_never_overwrites_an_existing_report(tmp_path, monkeypatch):
    from presidio.evaluation import tiny2_comparison_docker as study
    args = _docker_args(tmp_path)
    args.report_dir.mkdir()
    (args.report_dir / "candidate-analyzer.json").write_text("preserved")
    monkeypatch.setattr(study, "image_id", lambda _: pytest.fail("Docker must not be touched"))
    with pytest.raises(ValueError, match="already exists"):
        study.run(args)
    assert (args.report_dir / "candidate-analyzer.json").read_text() == "preserved"


@pytest.mark.parametrize("write_output", [False, True])
def test_docker_study_is_internal_bounded_and_cleans_only_owned_resources(tmp_path, monkeypatch, write_output):
    from presidio.evaluation import tiny2_comparison_docker as study
    args = _docker_args(tmp_path)
    calls = []
    monkeypatch.setattr(study, "image_id", lambda _: "sha256:" + "a" * 64)
    monkeypatch.setattr(study, "identities", lambda _: {"model": "trusted"})
    monkeypatch.setattr(study, "quiet", lambda *args: calls.append(args) or "")
    monkeypatch.setattr(study, "ready", lambda _: True)
    monkeypatch.setattr(study, "wait_ready", lambda probe, _: probe())
    monkeypatch.setattr(study, "container_states", lambda _: [])

    def fake_run(command, **kwargs):
        calls.append(tuple(command))
        if "evaluate" in command and write_output:
            (args.report_dir / "candidate-analyzer.json").write_text('{"complete":true}')
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(study.subprocess, "run", fake_run)
    if write_output:
        study.run(args)
    else:
        with pytest.raises(RuntimeError, match="complete report"):
            study.run(args)
    assert any(c[:3] == ("network", "create", "--internal") for c in calls)
    launches = [c for c in calls if c and c[0] == "run"]
    assert launches and all("--publish" not in c and "-p" not in c for c in launches)
    assert not any(".env" == word or "prune" == word for c in calls for word in c)
    cleanup = [c for c in calls if len(c) > 3 and c[:3] == ("docker", "rm", "-f")]
    assert cleanup and all(name.startswith("ru-ner-e9-") for name in cleanup[-1][3:])
