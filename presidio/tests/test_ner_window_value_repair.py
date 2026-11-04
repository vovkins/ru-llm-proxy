"""Keep positive source coverage when window edges cut a bounded value."""

import json
import hashlib
from pathlib import Path
import threading
from types import SimpleNamespace

import pytest

from presidio.ner.triton_recognizer import (
    EntityPrediction, NERInferenceCancelled, NERProcessingError,
    NERWindowAnalysis, NERWindowBoundaryError, WindowEntityPrediction,
)
from presidio.ner.text_processing import normalize_for_ner
from presidio.ner.span_postprocessing import ValueSpanExpansion, expand_value_spans_with_evidence
from presidio.ner.work_budget import work_scope
from presidio.tests.test_ner import FakeModel, FakeTokenizer, make_recognizer


def prediction(kind, start, end, window=0, *, left=False, right=False, score=.8):
    return WindowEntityPrediction(EntityPrediction(kind, start, end, score), window, left, right)


def recognizer(monkeypatch, initial, recovery, *, windows=2, recovery_windows=1):
    model = FakeModel()
    instance = make_recognizer(tokenizer=FakeTokenizer([(0, 1)]), model=model,
                               span_postprocessing="secrets-contracts")
    calls = []
    monkeypatch.setattr(instance, "_predict_window_entities", lambda text, **kw:
                        NERWindowAnalysis(initial, windows, len(text)))
    def recheck(*args, **kwargs):
        calls.append(True)
        return recovery, recovery_windows
    monkeypatch.setattr(instance, "_recover_window_boundaries", recheck)
    return instance, model, calls


def captured_history():
    # Freeze the original recipe independently of future load-generator changes.
    words = ["loaduser0050-wave0090400000-request202@example.test"] + ["контекст"] * 7999
    for index in range(16, len(words) - 2, 32):
        words[index:index + 3] = ["фрагмент", "70220200", str(index)]
    source = " ".join(words)
    assert hashlib.sha256(source.encode()).hexdigest() == "f2b3a2457730a7c436e6dabdee3255a57f9a75047ded13115a574a993732caa2"
    return source


def test_captured_history_accepts_complete_left_value_boundary(monkeypatch):
    source = captured_history()
    initial = [
        prediction("CONTRACT_NUMBER", 15782, 15787, 6, left=True, score=.6945170760154724),
        prediction("CONTRACT_NUMBER", 28846, 28854, 11, score=.7679303884506226),
        prediction("CONTRACT_NUMBER", 47025, 47027, 18, score=.8790338635444641),
        prediction("CONTRACT_NUMBER", 65201, 65203, 25, score=.7892554402351379),
    ]
    instance, model, calls = recognizer(monkeypatch, initial, [], windows=28, recovery_windows=1)
    telemetry = []
    results = instance.analyze(source, telemetry_callback=telemetry.append)
    assert len(results) == 4
    assert (results[0].start, results[0].end) == (15782, 15790)
    for item in initial:
        assert any(r.entity_type == item.entity.entity_type and r.start <= item.entity.start
                   and r.end >= item.entity.end for r in results)
    assert results[0].score == initial[0].entity.score
    assert calls == [True] and model.call_count == 0
    assert telemetry[0].windows_processed == 29 and instance.is_ready()


@pytest.mark.parametrize("kind", ["LOGIN", "PASSWORD", "AUTH_TOKEN", "SECRET_KEY", "CONTRACT_NUMBER"])
@pytest.mark.parametrize("left_offset,right_offset,left_cut,right_cut", [
    (0, 5, True, False), (3, 8, False, True), (0, 8, True, True), (2, 6, True, True),
])
def test_syntax_confirms_aligned_edges_without_losing_source_coverage(
    monkeypatch, kind, left_offset, right_offset, left_cut, right_cut,
):
    source = "prefix 12345678 suffix"
    start = source.index("12345678")
    instance, _, calls = recognizer(monkeypatch, [
        prediction(kind, start + left_offset, start + right_offset, left=left_cut, right=right_cut),
    ], [])
    result, = instance.analyze(source)
    assert (result.start, result.end, result.entity_type, result.score) == (start, start + 8, kind, .8)
    assert instance.is_ready() and calls == [True]


@pytest.mark.parametrize("filename,initial,recovery,windows,recovery_windows", [
    ("2.txt", [prediction("AUTH_TOKEN", 783, 800, 1, left=True)], [], 2, 1),
    ("5.txt", [prediction("AUTH_TOKEN", 281, 461, right=True),
               prediction("AUTH_TOKEN", 387, 635, 1, left=True),
               prediction("SECRET_KEY", 823, 826, 1, right=True)],
     [prediction("AUTH_TOKEN", 163, 605, 5, left=True),
      prediction("AUTH_TOKEN", 236, 678, 6, left=True, right=True),
      prediction("AUTH_TOKEN", 607, 1052, 7, left=True, right=True)], 5, 3),
])
def test_captured_dkb_json_edges_expand_without_discarding_positive_evidence(
    monkeypatch, filename, initial, recovery, windows, recovery_windows,
):
    fixture = Path(__file__).resolve().parents[2] / "tests/fixtures/dkb" / filename
    source = json.dumps({"source": fixture.read_bytes().decode("utf-8")}, ensure_ascii=False)
    instance, model, calls = recognizer(monkeypatch, initial, recovery, windows=windows,
                                        recovery_windows=recovery_windows)
    telemetry = []

    results = instance.analyze(source, telemetry_callback=telemetry.append)

    start, end = source.index(': "') + 3, len(source) - 2
    assert {(r.entity_type, r.start, r.end) for r in results} == {
        (p.entity.entity_type, start, end) for p in initial
    }
    for p in initial + recovery:
        assert any(r.entity_type == p.entity.entity_type
                   and r.start <= p.entity.start < p.entity.end <= r.end for r in results)
    assert json.loads(source[:start] + "<MASK>" + source[end:]) == {"source": "<MASK>"}
    assert instance.is_ready()
    assert calls == [True]
    assert model.call_count == 0
    assert telemetry[0].windows_processed == windows + recovery_windows


@pytest.mark.parametrize("kind", ["PERSON", "ORGANIZATION", "LOCATION"])
def test_value_repair_does_not_reinterpret_unresolved_names(monkeypatch, kind):
    source = '{"value":"abcdef"}'
    start = source.index("bcd")
    initial = [prediction(kind, start, start + 3, right=True)]
    instance, _, _ = recognizer(monkeypatch, initial, [])
    with pytest.raises(NERProcessingError) as error:
        instance.analyze(source)
    assert error.value.failure_class == "window_boundary_unresolved"
    assert instance.is_ready()


@pytest.mark.parametrize("source,start,end", [
    ('{"a":"abc","b":"def"}', 7, 19),
    ("abc def", 0, 7),
    ("abcdef", 0, 6),
])
def test_no_complete_expanding_value_keeps_request_scoped_refusal(monkeypatch, source, start, end):
    initial = [prediction("AUTH_TOKEN", start, end, left=True, right=True)]
    instance, _, _ = recognizer(monkeypatch, initial, [])
    with pytest.raises(NERProcessingError) as error:
        instance.analyze(source)
    assert error.value.failure_class == "window_boundary_unresolved"
    monkeypatch.setattr(instance, "_predict_window_entities", lambda *_a, **_kw: ([], 1))
    assert instance.analyze("ok") == []
    assert instance.is_ready()


def test_recovery_evidence_outside_a_value_cannot_be_lost(monkeypatch):
    source = "key=abc next=def"
    start = source.index("b")
    initial = [prediction("AUTH_TOKEN", start, start + 1, right=True)]
    recovery = [prediction("AUTH_TOKEN", start, len(source), 2, right=True)]
    instance, _, _ = recognizer(monkeypatch, initial, recovery)
    with pytest.raises(NERProcessingError) as error:
        instance.analyze(source)
    assert error.value.failure_class == "window_boundary_unresolved"


@pytest.mark.parametrize("repaired", [
    (1, 4, "AUTH_TOKEN", .8), (2, 5, "AUTH_TOKEN", .8),
    (3, 6, "AUTH_TOKEN", .8), (-1, 6, "AUTH_TOKEN", .8),
    (0, 7, "AUTH_TOKEN", .8), (1, 5, "PASSWORD", .8),
    (1, 5, "AUTH_TOKEN", .9),
])
def test_each_cut_edge_and_original_type_score_are_required(monkeypatch, repaired):
    instance, _, _ = recognizer(monkeypatch, [], [])
    monkeypatch.setattr("presidio.ner.triton_recognizer.expand_value_spans_with_evidence",
                        lambda *_: [ValueSpanExpansion(repaired)])
    with pytest.raises(NERWindowBoundaryError):
        instance._merge_rechecked_window_predictions("abcdef", [
            prediction("AUTH_TOKEN", 2, 4, left=True, right=True),
        ], [])


@pytest.mark.parametrize("selected", [None, ["AUTH_TOKEN"], ["PERSON"]])
def test_filters_and_independent_complete_findings_survive(monkeypatch, selected):
    source = '{"value":"abcdef","person":"Ivan"}'
    start, name = source.index("bcd"), source.index("Ivan")
    initial = [prediction("AUTH_TOKEN", start, start + 3, right=True),
               prediction("PERSON", name, name + 4)]
    instance, _, calls = recognizer(monkeypatch, initial, [])
    results = instance.analyze(source, entities=selected)
    expected = []
    if selected is None or "AUTH_TOKEN" in selected:
        expected.append(("AUTH_TOKEN", "abcdef"))
    if selected is None or "PERSON" in selected:
        expected.append(("PERSON", "Ivan"))
    assert [(r.entity_type, source[r.start:r.end]) for r in results] == expected
    assert len(calls) == (0 if selected == ["PERSON"] else 1)


def test_below_threshold_finding_does_not_trigger_value_repair(monkeypatch):
    source = '{"value":"abcdef"}'
    start = source.index("bcd")
    instance, _, calls = recognizer(monkeypatch, [
        prediction("AUTH_TOKEN", start, start + 3, right=True, score=.34),
    ], [])
    assert instance.analyze(source, score_threshold=.35) == []
    assert calls == []


def test_expansion_uses_the_minimum_original_and_recovery_score(monkeypatch):
    source = '{"value":"abcdef"}'
    start = source.index("bcd")
    instance, _, _ = recognizer(monkeypatch, [
        prediction("AUTH_TOKEN", start, start + 3, right=True, score=.9),
    ], [prediction("AUTH_TOKEN", start - 1, start + 3, 2, right=True, score=.6)])
    result, = instance.analyze(source)
    assert source[result.start:result.end] == "abcdef"
    assert result.score == .6


def test_multiple_clusters_share_one_lexical_parse(monkeypatch):
    from presidio.ner import span_postprocessing
    original = span_postprocessing._ValueBoundaries
    calls = []
    def counted(*args, **kwargs):
        calls.append(True)
        return original(*args, **kwargs)
    monkeypatch.setattr(span_postprocessing, "_ValueBoundaries", counted)
    source = '{"password":"abcdef","key":"uvwxyz"}'
    a, b = source.index("bcd"), source.index("vwx")
    instance, _, _ = recognizer(monkeypatch, [], [])
    results = instance._merge_rechecked_window_predictions(source, [
        prediction("PASSWORD", a, a + 3, right=True),
        prediction("SECRET_KEY", b, b + 3, right=True),
    ], [])
    assert [(r.entity_type, source[r.start:r.end]) for r in results] == [
        ("PASSWORD", "abcdef"), ("SECRET_KEY", "uvwxyz"),
    ]
    assert calls == [True]


def test_repair_preserves_unicode_source_coordinates_and_json_syntax(monkeypatch):
    source = json.dumps({"value": "Ива\u0301н🙂a/b!", "next": "visible"}, ensure_ascii=False)
    normalized = normalize_for_ner(source).text
    start = normalized.index("🙂")
    instance, _, _ = recognizer(monkeypatch, [
        prediction("AUTH_TOKEN", start, start + 1, right=True),
    ], [])
    results = instance.analyze(source)
    assert len(results) == 1
    finding = results[0]
    assert source[finding.start:finding.end] == "Ива\u0301н🙂a/b!"
    masked = source[:finding.start] + "<MASK>" + source[finding.end:]
    assert json.loads(masked) == {"value": "<MASK>", "next": "visible"}


def test_cancellation_during_value_repair_is_not_a_success_or_service_failure(monkeypatch):
    cancelled = threading.Event()
    source = '{"value":"abcdef"}'
    start = source.index("bcd")
    instance, _, _ = recognizer(monkeypatch, [prediction("AUTH_TOKEN", start, start + 3, right=True)], [])
    def cancel(*args):
        result = expand_value_spans_with_evidence(*args)
        cancelled.set()
        return result
    monkeypatch.setattr("presidio.ner.triton_recognizer.expand_value_spans_with_evidence", cancel)
    with pytest.raises(NERInferenceCancelled):
        instance.analyze(source, cancellation_event=cancelled)
    assert instance.is_ready()


def test_repair_respects_the_existing_request_budget(monkeypatch):
    from presidio.ner import work_budget
    now = [0.]
    monkeypatch.setattr(work_budget, "time", SimpleNamespace(monotonic=lambda: now[0]))
    source = '{"value":"abcdef"}'
    start = source.index("bcd")
    instance, _, _ = recognizer(monkeypatch, [prediction("AUTH_TOKEN", start, start + 3, right=True)], [])
    def expire(*args):
        result = expand_value_spans_with_evidence(*args)
        now[0] = 2.
        return result
    monkeypatch.setattr("presidio.ner.triton_recognizer.expand_value_spans_with_evidence", expire)
    with work_scope(seconds=1), pytest.raises(NERProcessingError) as error:
        instance.analyze(source)
    assert error.value.failure_class == "analysis_deadline_exceeded"
    assert error.value.windows_processed == 3
    assert instance.is_ready()


def test_repair_exception_is_request_scoped_with_accurate_window_count(monkeypatch):
    source = '{"value":"abcdef"}'
    start = source.index("bcd")
    instance, _, _ = recognizer(monkeypatch, [prediction("AUTH_TOKEN", start, start + 3, right=True)], [])
    def fail(*args):
        raise ValueError("synthetic parser failure")
    monkeypatch.setattr("presidio.ner.triton_recognizer.expand_value_spans_with_evidence", fail)
    with pytest.raises(NERProcessingError) as error:
        instance.analyze(source)
    assert error.value.phase == "postprocessing"
    assert error.value.failure_class == "span_postprocessing_failed"
    assert error.value.windows_processed == 3
    assert instance.is_ready()


def test_remaining_boundary_log_is_bounded_and_does_not_include_source(monkeypatch, caplog):
    source = "private-source-must-not-be-logged"
    instance, _, _ = recognizer(monkeypatch, [prediction("PERSON", 4, 8, left=True)], [])
    with caplog.at_level("WARNING"), pytest.raises(NERProcessingError):
        instance.analyze(source)
    events = [json.loads(r.getMessage()) for r in caplog.records
              if r.getMessage().startswith('{"cluster_count"')]
    assert events == [{"event": "presidio_ner_window_boundary_unresolved", "phase": "windowing",
        "failure_class": "window_boundary_unresolved", "entity_types": ["PERSON"], "cluster_count": 1,
        "initial_windows_processed": 2, "recovery_windows_processed": 1, "windows_processed_total": 3}]
    assert source not in "\n".join(r.getMessage() for r in caplog.records)
