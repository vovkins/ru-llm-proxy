"""The guarded mode needs a complete response from the matching PERSON control."""
import threading

import pytest

from presidio.ner import triton_recognizer as ner
from presidio.ner.boundary_values import BoundaryContext
from presidio.tests.test_person_control_window import SOURCE, VALUE, setup


def enable(instance):
    instance.boundary_mode = "guarded"


@pytest.mark.parametrize("vote", ["o", "partial", "edge", "weak", "other"])
def test_complete_inconclusive_control_keeps_positive_mask(monkeypatch, vote):
    instance, context, calls, _ = setup(monkeypatch, vote)
    enable(instance)
    findings = instance.analyze(SOURCE, boundary_context=context)
    assert len(calls) == 1
    assert [(result.entity_type, SOURCE[result.start : result.end]) for result in findings] == [
        ("PERSON", VALUE),
        ("ORGANIZATION", "y" * 10),
    ]
    assert any((result.recognition_metadata or {}).get("boundary_required_spans_v1")
               for result in findings)


@pytest.mark.parametrize("vote", ["timeout", "malformed"])
def test_missing_or_invalid_control_keeps_existing_503(monkeypatch, vote):
    instance, context, calls, _ = setup(monkeypatch, vote)
    enable(instance)
    with pytest.raises(ner.NERProcessingError) as caught:
        instance.analyze(SOURCE, boundary_context=context)
    assert caught.value.failure_class == "window_boundary_unresolved"
    assert caught.value.windows_processed == 4
    assert len(calls) == 1
    assert instance.is_ready()


@pytest.mark.parametrize("reason", ["no_control", "not_enough_time", "multiple", "organization"])
def test_skipped_control_cannot_authorize_new_fallback(monkeypatch, reason):
    instance, context, calls, _ = setup(
        monkeypatch, "o", kind="ORGANIZATION" if reason == "organization" else "PERSON",
        another=reason == "multiple",
    )
    enable(instance)
    if reason == "no_control":
        context = BoundaryContext(context.values)
    elif reason == "not_enough_time":
        monkeypatch.setattr(ner, "remaining_seconds", lambda: .01)
    with pytest.raises(ner.NERProcessingError) as caught:
        instance.analyze(SOURCE, boundary_context=context)
    assert caught.value.failure_class == "window_boundary_unresolved"
    assert calls == []


def test_success_does_not_carry_the_control_to_another_chunk(monkeypatch):
    instance, context, calls, _ = setup(monkeypatch, "o")
    enable(instance)
    assert instance.analyze(SOURCE, boundary_context=context)
    assert context.person_window.used
    with pytest.raises(ner.NERProcessingError) as caught:
        instance.analyze(SOURCE, boundary_context=context)
    assert caught.value.failure_class == "window_boundary_unresolved"
    assert len(calls) == 1


@pytest.mark.parametrize("length", [64, 65])
def test_length_of_completed_source_span_keeps_full_word_or_503(monkeypatch, length):
    value = "Б" + "я" * (length - 1)
    source = SOURCE.replace(VALUE, value)
    instance, context, calls, _ = setup(monkeypatch, "o", source=source, value=value)
    enable(instance)
    if length == 65:
        with pytest.raises(ner.NERProcessingError) as caught:
            instance.analyze(source, boundary_context=context)
        assert caught.value.failure_class == "window_boundary_unresolved"
    else:
        findings = instance.analyze(source, boundary_context=context)
        person, = [result for result in findings if result.entity_type == "PERSON"]
        assert source[person.start : person.end] == value
    assert len(calls) == 1


def test_cancellation_after_control_does_not_emit_a_mask(monkeypatch):
    cancelled = threading.Event()
    instance, context, calls, _ = setup(monkeypatch, cancelled)
    enable(instance)
    with pytest.raises(ner.NERInferenceCancelled):
        instance.analyze(SOURCE, boundary_context=context, cancellation_event=cancelled)
    assert len(calls) == 1
    assert instance.is_ready()


@pytest.mark.parametrize(("value", "fragment"), [
    ("O’Connor", "Connor"),
    ("Anne‑Marie", "Marie"),
    ("Mid\u200dnight", "night"),
])
def test_complete_control_cannot_allow_half_of_a_joined_word(monkeypatch, value, fragment):
    source = SOURCE.replace(VALUE, value)
    instance, context, calls, _ = setup(
        monkeypatch, "o", source=source, value=value, fragment=fragment,
    )
    enable(instance)
    with pytest.raises(ner.NERProcessingError) as caught:
        instance.analyze(source, boundary_context=context)
    assert caught.value.failure_class == "window_boundary_unresolved"
    assert len(calls) == 1
