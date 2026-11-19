"""Distinguish a negative PERSON control result from a failed inference."""

from types import SimpleNamespace
import threading

import pytest

from presidio.ner import triton_recognizer as ner
from presidio.ner.boundary_values import BoundaryContext, BoundaryValues, PersonWindowBudget
from presidio.ner.text_processing import TokenWindow, complete_word_edges, normalize_for_ner
from presidio.ner.work_budget import work_scope
from presidio.result_merging import merge_results
from presidio.tests.test_ner_window_value_repair import prediction, recognizer


VALUE = "Mira Meadow"
SOURCE = "x" * 300 + "[log]\nDispatch (label " + VALUE + "\n[tail] " + "y" * 260


def setup(monkeypatch, vote="positive", *, kind="PERSON", another=False,
          source=SOURCE, value=VALUE, fragment=None):
    normalized = normalize_for_ner(source).text
    name = normalize_for_ner(value).text
    at = normalized.index(name)
    start, end = at + 5, at + len(name)
    if fragment is not None:
        start = at + name.index(fragment)
        end = start + len(fragment)
    tail = normalized.index("y" * 10)
    prior = prediction("ORGANIZATION", tail, tail + 10, 1)
    right_cut = end < at + len(name)
    edge = prediction(kind, start, end, left=start > at or not right_cut, right=right_cut)
    initial = [edge, prior]
    if another:
        initial.append(prediction("PERSON", tail + 12, tail + 18, 1, right=True))
    offsets = [(i, i + 1) for i, ch in enumerate(normalized) if not ch.isspace()]
    ids = list(range(len(offsets)))
    windows = (TokenWindow(0, 0, 384), TokenWindow(1, 320, len(ids)))
    instance, _, shifted = recognizer(monkeypatch, initial, [])
    monkeypatch.setattr(instance, "_predict_window_entities", lambda *a, **kw:
                        ner.NERWindowAnalysis(initial, 2, len(ids), (ids, offsets, windows)))
    calls = []

    def infer(batches, spans):
        calls.append((batches, spans))
        if isinstance(vote, Exception):
            raise vote
        if isinstance(vote, threading.Event):
            vote.set()
        raw = []
        begin = at + 7 if vote == "partial" else at
        stop = at + len(name)
        if fragment is not None:
            begin, stop = start, end
        if vote == "edge":
            begin = spans[0][0][0]
        for a, b in spans[0]:
            label = "O"
            if vote in ("positive", "partial", "edge", "weak", "other") and begin <= a < stop:
                prefix = "B" if a == begin else "I"
                label = prefix + ("-LOGIN" if vote == "other" else "-PERSON")
            raw.append(ner.TokenPrediction(label, a, b, .2 if vote == "weak" else .9))
        return [raw[:-1] if vote == "malformed" else raw]

    monkeypatch.setattr(instance, "_infer_window_batch", infer)
    budget = PersonWindowBudget()
    return instance, BoundaryContext(BoundaryValues(source), person_window=budget), calls, shifted


def test_complete_window_preserves_other_findings_and_counts_work(monkeypatch):
    instance, context, calls, shifted = setup(monkeypatch)
    events = []
    output = instance.analyze(SOURCE, boundary_context=context, telemetry_callback=events.append)
    assert [(r.entity_type, SOURCE[r.start:r.end]) for r in output] == [
        ("PERSON", VALUE), ("ORGANIZATION", "y" * 10),
    ]
    assert context.person_window.used
    assert len(calls) == 1 and len(calls[0][0][0]) == 320 and shifted == [True]
    assert events[0].windows_processed == 4
    # The same shared budget is passed to every chunk.
    events.clear()
    fallback, _ = instance.analyze(SOURCE, boundary_context=context, telemetry_callback=events.append)
    assert SOURCE[fallback.start:fallback.end] == "Meadow"
    assert events[0].windows_processed == 3 and len(calls) == 1


@pytest.mark.parametrize("fragment", ["\u0438\u0447", "\u041f\u043e\u043b\u0438\u0432\u0430\u043d\u043e\u0432"])
def test_control_masks_the_whole_split_word(monkeypatch, fragment):
    surname = "\u041f\u043e\u043b\u0438\u0432\u0430\u043d\u043e\u0432\u0438\u0447"
    source = SOURCE.replace(VALUE, surname)
    instance, context, calls, _ = setup(monkeypatch, source=source, value=surname, fragment=fragment)
    fallback, *_ = instance.analyze(source, boundary_context=BoundaryContext(context.values))
    assert source[fallback.start:fallback.end] == surname
    findings = instance.analyze(source, boundary_context=context)
    merged = merge_results(source, findings, score_threshold=.35).results
    assert [source[r.start:r.end] for r in merged if r.entity_type == "PERSON"] == [surname]
    assert len(calls) == 1


@pytest.mark.parametrize("value,fragment,expected", [
    ("Q. Meadow", "Mead", "Meadow"),
    ("O'Connor", "Connor", "O'Connor"),
    ("Q. O\u2019Connor", "Q. O\u2019Con", "Q. O\u2019Connor"),
    ("Anna\u2011Petrova", "Pet", "Anna\u2011Petrova"),
    ("Se\u0301verin", "ver", "Se\u0301verin"),
])
def test_control_closes_source_words_without_masking_neighbours(monkeypatch, value, fragment, expected):
    source = SOURCE.replace(VALUE, "before " + value + ", after")
    instance, context, calls, _ = setup(monkeypatch, source=source, value=value, fragment=fragment)
    findings = instance.analyze(source, boundary_context=context)
    merged = merge_results(source, findings, score_threshold=.35).results
    person, = [r for r in merged if r.entity_type == "PERSON"]
    assert source[person.start:person.end] == expected
    assert source[person.end:person.end + 7] == ", after"
    assert "before " in source[:person.start]
    if value.startswith("Q. ") and not expected.startswith("Q. "):
        assert source[person.start - 3:person.start] == "Q. "
    assert len(calls) == 1


@pytest.mark.parametrize("value,fragment", [
    ("user_Meadow", "Meadow"),
    ("Meadow42", "Meadow"),
    ("Mid\u200dnight", "night"),
])
def test_ambiguous_word_edge_keeps_only_original_evidence(monkeypatch, value, fragment):
    source = SOURCE.replace(VALUE, value)
    instance, context, calls, _ = setup(monkeypatch, source=source, value=value, fragment=fragment)
    fallback, _ = instance.analyze(source, boundary_context=context)
    assert source[fallback.start:fallback.end] == fragment
    assert len(calls) == 1


@pytest.mark.parametrize("fragment,first,last", [
    ("Connor", 8, 22),     # The left side of O'Connor is outside the window.
    ("O", 0, 9),           # The joiner continues to a letter beyond the window.
    ("Con", 0, 14),        # The word is cut by the right edge.
    ("Con", 0, 15),        # The word would end on the window edge.
])
def test_word_completion_does_not_reach_the_window_edge(fragment, first, last):
    source = "prefix O'Connor suffix"
    start = source.index(fragment)
    assert complete_word_edges(source, start, start + len(fragment), first, last) is None


@pytest.mark.parametrize("vote", ["o", "partial", "edge", "weak", "other"])
def test_inconclusive_control_does_not_remove_evidence(monkeypatch, vote):
    instance, context, calls, _ = setup(monkeypatch, vote)
    events = []
    fallback, _ = instance.analyze(SOURCE, boundary_context=context, telemetry_callback=events.append)
    assert SOURCE[fallback.start:fallback.end] == "Meadow"
    assert events[0].windows_processed == 4
    assert len(calls) == 1 and instance.is_ready()


@pytest.mark.parametrize("failure_class,ready,retry_after", [
    ("triton_unavailable", False, "5"),
    ("triton_overloaded", True, "7"),
    ("triton_contract_invalid", False, None),
    ("analysis_deadline_exceeded", True, None),
    ("forward_pass_failed", False, None),
    ("malformed", False, None),
])
def test_failed_control_window_returns_analyzer_503(
    monkeypatch, caplog, failure_class, ready, retry_after,
):
    from fastapi.testclient import TestClient
    from presidio import analyzer_server as server
    from presidio.tests import test_ner_window_value_repair
    from capacity import AnalyzerCapacityLimiter
    from ner import NERProcessingError, NERUnavailableError

    kind = "triton_contract_invalid" if failure_class == "malformed" else failure_class
    if failure_class == "forward_pass_failed":
        vote = TimeoutError("private-control-error")
    elif failure_class == "malformed":
        vote = "malformed"
    elif failure_class == "analysis_deadline_exceeded":
        vote = NERProcessingError(phase="inference", failure_class=kind)
    else:
        vote = NERUnavailableError(phase="inference", failure_class=kind)
    monkeypatch.setattr(test_ner_window_value_repair, "make_recognizer",
                        lambda **kwargs: server.TritonNERRecognizer(o_logit_bias=0.0, **kwargs))
    instance, _, calls, shifted = setup(monkeypatch, vote)
    monkeypatch.setattr(server, "ner_recognizer", instance)
    monkeypatch.setattr(server, "capacity_limiter", AnalyzerCapacityLimiter(queue_timeout_seconds=7))
    monkeypatch.setattr(server.analyzer, "analyze", lambda **kwargs: [])
    monkeypatch.setattr(server.nlp_engine, "get_supported_languages", lambda: [])
    events = []
    monkeypatch.setattr(server, "_record_ner_inference", events.append)

    response = TestClient(server.app).post("/api/v1/analyze", json={"text": SOURCE})
    assert response.status_code == 503
    detail = response.json()["detail"]
    assert detail["code"] == "required_ner_unavailable"
    assert detail["failure_class"] == kind and detail["phase"] == "inference"
    assert response.headers.get("retry-after") == retry_after
    if kind in {"triton_contract_invalid", "analysis_deadline_exceeded"}:
        assert detail["retryable"] is False
    assert instance.is_ready() is ready
    assert instance.failure_class() == (None if ready else kind)
    assert len(calls) == 1 and shifted == [True]
    assert len(events) == 1 and events[0].windows_processed == 3
    assert events[0].failure_class == kind and events[0].outcome in {"failure", "unavailable"}
    assert "private-control-error" not in response.text + caplog.text
    assert VALUE not in response.text + caplog.text


@pytest.mark.parametrize("scope", ["other", "multiple", "json", "budget", "margin"])
def test_ineligible_request_does_not_call_control(monkeypatch, scope):
    instance, context, calls, _ = setup(
        monkeypatch, kind="ORGANIZATION" if scope == "other" else "PERSON", another=scope == "multiple",
    )
    if scope == "json":
        context = BoundaryContext(context.values)
    if scope == "margin":
        predict = instance._predict_window_entities

        def clipped(*args, **kwargs):
            out = predict(*args, **kwargs)
            ids, offsets, windows = out.window_input
            return ner.NERWindowAnalysis(out.predictions, 2, out.input_tokens,
                                         (ids[:370], offsets[:370], windows))

        monkeypatch.setattr(instance, "_predict_window_entities", clipped)
    if scope == "budget":
        monkeypatch.setattr(ner, "remaining_seconds", lambda: .01)
    events = []
    results = instance.analyze(SOURCE, boundary_context=context, telemetry_callback=events.append)
    assert any(SOURCE[r.start:r.end] == "Meadow" for r in results)
    assert events[0].windows_processed == 3
    assert calls == []


@pytest.mark.parametrize("wrap", [
    'Some text {"owner":"%s"} continues',
    'Some text https://example.test/?owner=%s continues',
    'Some text `%s` continues',
    'Some text\n```\n%s\n```\ncontinues',
    'Some text `unfinished\n%s continues',
])
def test_structured_source_is_not_plain_text(wrap):
    source = wrap % "Meadow"
    at = source.index("Meadow")
    assert not BoundaryValues(source).is_plain_text_span(at, at + len("Meadow"))


def test_cancellation_after_inference_is_not_success(monkeypatch):
    cancelled = threading.Event()
    instance, context, calls, _ = setup(monkeypatch, cancelled)
    with pytest.raises(ner.NERInferenceCancelled):
        instance.analyze(SOURCE, boundary_context=context, cancellation_event=cancelled)
    assert len(calls) == 1 and instance.is_ready()


def test_cancellation_during_failed_control_does_not_change_readiness(monkeypatch):
    cancelled = threading.Event()
    instance, context, _, shifted = setup(monkeypatch)

    def fail(*args):
        cancelled.set()
        raise ner.NERUnavailableError(phase="inference", failure_class="triton_unavailable")

    monkeypatch.setattr(instance, "_infer_window_batch", fail)
    events = []
    with pytest.raises(ner.NERInferenceCancelled):
        instance.analyze(SOURCE, boundary_context=context, cancellation_event=cancelled,
                         telemetry_callback=events.append)
    assert instance.is_ready() and shifted == [True]
    assert not events


def test_expiration_after_inference_is_not_success(monkeypatch):
    from presidio.ner import work_budget

    now = [0.]
    monkeypatch.setattr(work_budget, "time", SimpleNamespace(monotonic=lambda: now[0]))
    instance, context, calls, _ = setup(monkeypatch)
    infer = instance._infer_window_batch

    def expire(*args):
        out = infer(*args)
        now[0] = 2.
        return out

    monkeypatch.setattr(instance, "_infer_window_batch", expire)
    with work_scope(seconds=1), pytest.raises(ner.NERProcessingError) as failure:
        instance.analyze(SOURCE, boundary_context=context)
    assert failure.value.failure_class == "analysis_deadline_exceeded"
    assert failure.value.windows_processed == 3
    assert len(calls) == 1 and instance.is_ready()
