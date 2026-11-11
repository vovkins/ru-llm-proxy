"""Only complete positive evidence may recover a PERSON window failure."""

from types import SimpleNamespace
import threading

import pytest

from presidio.ner import triton_recognizer as ner
from presidio.ner.boundary_values import BoundaryContext, BoundaryValues, PersonWindowBudget
from presidio.ner.text_processing import TokenWindow, normalize_for_ner
from presidio.ner.work_budget import work_scope
from presidio.tests.test_ner_window_value_repair import prediction, recognizer


VALUE = "Mira Meadow"
SOURCE = "x" * 300 + "[log]\nDispatch (label " + VALUE + "\n[tail] " + "y" * 260


def setup(monkeypatch, vote="positive", *, kind="PERSON", another=False):
    normalized = normalize_for_ner(SOURCE).text
    at = normalized.index(VALUE)
    tail = normalized.index("y" * 10)
    prior = prediction("ORGANIZATION", tail, tail + 10, 1)
    edge = prediction(kind, at + 5, at + len(VALUE), left=True)
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
        if vote == "timeout":
            raise TimeoutError()
        if isinstance(vote, threading.Event):
            vote.set()
        raw = []
        begin = at + 7 if vote == "partial" else at
        if vote == "edge":
            begin = spans[0][0][0]
        for a, b in spans[0]:
            label = "O"
            if vote in ("positive", "partial", "edge", "weak", "other") and begin <= a < at + len(VALUE):
                prefix = "B" if a == begin else "I"
                label = prefix + ("-LOGIN" if vote == "other" else "-PERSON")
            raw.append(ner.TokenPrediction(label, a, b, .2 if vote == "weak" else .9))
        return [raw[:-1] if vote == "malformed" else raw]

    monkeypatch.setattr(instance, "_infer_window_batch", infer)
    budget = PersonWindowBudget()
    return instance, BoundaryContext(BoundaryValues(SOURCE), person_window=budget), calls, shifted


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
    with pytest.raises(ner.NERProcessingError, match="backend is unavailable") as failure:
        instance.analyze(SOURCE, boundary_context=context)
    assert failure.value.failure_class == "window_boundary_unresolved"
    assert failure.value.windows_processed == 3 and len(calls) == 1


@pytest.mark.parametrize("vote", ["o", "partial", "edge", "weak", "other", "malformed", "timeout"])
def test_inconclusive_control_does_not_remove_evidence(monkeypatch, vote):
    instance, context, calls, _ = setup(monkeypatch, vote)
    with pytest.raises(ner.NERProcessingError) as failure:
        instance.analyze(SOURCE, boundary_context=context)
    assert failure.value.failure_class == "window_boundary_unresolved"
    assert failure.value.windows_processed == 4
    assert len(calls) == 1 and instance.is_ready()


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
    with pytest.raises(ner.NERProcessingError) as failure:
        instance.analyze(SOURCE, boundary_context=context)
    assert failure.value.failure_class == "window_boundary_unresolved"
    assert failure.value.windows_processed == 3
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


def test_expiration_and_cancellation_after_inference_are_not_success(monkeypatch):
    cancelled = threading.Event()
    instance, context, calls, _ = setup(monkeypatch, cancelled)
    with pytest.raises(ner.NERInferenceCancelled):
        instance.analyze(SOURCE, boundary_context=context, cancellation_event=cancelled)
    assert len(calls) == 1 and instance.is_ready()

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
    assert len(calls) == 1 and instance.is_ready()
