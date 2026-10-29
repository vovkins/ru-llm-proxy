"""Research tools must not weaken runtime scoring or hide missing coverage."""

from pathlib import Path

import numpy as np

from presidio.evaluation.corpus import CorpusCase, EntitySpan, load_corpus
from presidio.evaluation.tiny2_components import classify_for_study, load_diagnostic_cases, scoped_metrics, union_coverage, windows_from_metrics
from presidio.ner.decoding import classify_logits


def test_diagnostic_compensation_changes_selection_not_delivered_scores_or_input():
    logits = np.zeros((1, 1, 17), dtype=np.float32)
    logits[..., 1] = 1.5
    snapshot = logits.copy()
    ids, scores = classify_for_study(logits, 0, compensate_names=True)
    assert ids.item() == 0
    assert classify_logits(logits, 0)[0].item() == 1
    expected = 1 / (16 + np.exp(np.float32(1.5)))
    assert np.isclose(scores.item(), expected)
    np.testing.assert_array_equal(logits, snapshot)


def test_study_without_compensation_is_exact_runtime_classifier():
    logits = np.random.default_rng(5).normal(size=(2, 10, 17)).astype(np.float32)
    for bias in (0, 1):
        expected = classify_logits(logits, bias)
        actual = classify_for_study(logits, bias)
        for a, b in zip(expected, actual):
            np.testing.assert_array_equal(a, b)


def test_name_compensation_does_not_modify_credential_label_logits():
    logits = np.zeros((1, 1, 17), dtype=np.float32)
    logits[..., 9] = 3
    before = classify_logits(logits, 1)
    after = classify_for_study(logits, 1, compensate_names=True)
    for a, b in zip(before, after):
        np.testing.assert_array_equal(a, b)


def test_union_coverage_includes_adjacent_masks_but_reports_uncovered_punctuation():
    case = CorpusCase("value", "abc!?", (EntitySpan("PASSWORD", 0, 5),), True, ("positive",))
    spans = [{"start": 0, "end": 3}, {"start": 3, "end": 5}]
    assert union_coverage(case, spans)["fully_masked_values"] == 1
    spans[-1]["end"] = 4
    result = union_coverage(case, spans)
    assert result["fully_masked_values"] == 0
    assert result["masked_expected_characters"] == 4


def test_study_split_covers_classes_without_text_duplicates():
    path = Path(__file__).resolve().parents[1] / "evaluation/data/tiny2_component_study.jsonl"
    cases = load_corpus(path)
    assert len(cases) == 32
    texts = {}
    for split in ("development", "control"):
        subset = [case for case in cases if split in case.tags]
        assert len(subset) == 16
        assert len({e.entity_type for case in subset for e in case.expected}) == 8
        assert any(not case.expected for case in subset)
        texts[split] = {case.text for case in subset}
    assert not texts["development"] & texts["control"]


def test_partial_research_corpus_keeps_schema_validation_without_migration_gate():
    path = Path(__file__).resolve().parents[1] / "evaluation/data/deterministic_rules_review.jsonl"
    cases = load_diagnostic_cases(path)
    assert len(cases) == 75
    assert not any(case.critical for case in cases)


def test_partial_scope_excludes_unannotated_classes_but_checks_negative_pairs():
    positive = CorpusCase("positive", "Ann", (EntitySpan("PERSON", 0, 3),), True, ("positive",))
    negative = CorpusCase("negative", "Done", (), False, ("negative",))
    predictions = {
        "positive": [{"entity_type": "PERSON", "start": 0, "end": 3}],
        "negative": [{"entity_type": "LOGIN", "start": 0, "end": 4},
                     {"entity_type": "PERSON", "start": 0, "end": 3}],
    }
    result = scoped_metrics([positive, negative], predictions)
    assert result["evaluation_scope"] == ["PERSON"]
    assert result["aggregate"]["true_positives"] == 1
    assert result["aggregate"]["false_positives"] == 1


def test_window_count_includes_failures_but_not_bucket_or_count_samples():
    text = '\n'.join([
        '# HELP ru_presidio_analyzer_ner_windows_processed windows',
        'ru_presidio_analyzer_ner_windows_processed_sum{outcome="success"} 10',
        'ru_presidio_analyzer_ner_windows_processed_sum{outcome="failure"} 3',
        'ru_presidio_analyzer_ner_windows_processed_count{outcome="success"} 2',
    ])
    assert windows_from_metrics(text) == 13
