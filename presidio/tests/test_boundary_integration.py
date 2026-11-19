"""Exercise the guarded recognizer and negotiated contract with synthetic evidence."""

import asyncio
import json

import httpx
import pytest
from presidio_analyzer import RecognizerResult

from presidio import analyzer_server as server
from presidio.ner import boundary_runtime as runtime
from presidio.ner import triton_recognizer as ner
from presidio.ner.boundary_options import close_cluster
from presidio.tests.test_person_control_window import SOURCE, setup


def close(text, fragment, *, left=True, right=False):
    start = text.index(fragment)
    entity = ner.EntityPrediction("PERSON", start, start + len(fragment), .9)
    edge = ner.WindowEntityPrediction(entity, 0, left, right)
    cluster = ner.UnresolvedWindowEntityCluster("PERSON", (edge,))
    result, observation = close_cluster(text, cluster, lambda: None)
    return (text[result.entity.start:result.entity.end] if result else None), observation


def test_closure_is_directional_and_capped_to_two_words():
    text = "Max Smith, done."
    assert close(text, "Smith")[0] == "Max Smith"
    assert close(text, "Smith", left=False, right=True)[0] == "Smith"
    value, event = close("Record John Ann Ben Smith, done.", "Smith")
    assert value == "Ann Ben Smith"
    assert event["closure_capped"] and "two_word_cap" in event["boundary_reasons"]


@pytest.mark.parametrize("separator", ["\n", "[", "]", ":", "|", "(", ")", "=", "/", ",", ";",
                                       ".", "!", "?", " 4", " lowercase ", " CACHE "])
def test_structural_separators_and_ordinary_words_stop_expansion(separator):
    assert close("Max" + separator + " Smith, done.", "Smith")[0] == "Smith"


@pytest.mark.parametrize("value", ["user_Meadow", "Meadow42"])
def test_mixed_identifier_is_masked_as_one_token(value):
    masked, event = close("prefix " + value + " tail", "Meadow")
    assert masked == value and event["rule"] == "mixed_token"


def test_mixed_syntax_and_cross_literal_keep_refusal():
    result, rule = close("prefix user_Meadow) tail", "Meadow")
    assert result is None and rule["rule"] == "mixed_token_refused"
    result, rule = close('before {"a":"Max Smith"} after', 'Smith"} after')
    assert result is None and rule["reason"] == "cross_literal"
    assert close('before {"a":"Max Smith"} after', "Smith")[0] == "Max Smith"


@pytest.mark.parametrize("condition", ["postprocess_loss", "ledger_overflow"])
def test_guarded_proof_failures_are_request_scoped(monkeypatch, condition):
    instance, context, _, _ = setup(monkeypatch, "o")
    instance.boundary_mode = "guarded"
    if condition == "ledger_overflow":
        monkeypatch.setattr(runtime, "MAX_LEDGER", 0)
    else:
        original = instance._analyze_tokens

        def drop(*args, **kwargs):
            _, windows, tokens = original(*args, **kwargs)
            return [], windows, tokens

        monkeypatch.setattr(instance, "_analyze_tokens", drop)
    with pytest.raises(ner.NERProcessingError) as failure:
        instance.analyze(SOURCE, boundary_context=context)
    assert failure.value.failure_class == "boundary_positive_coverage_failed"
    assert instance.is_ready()
    assert runtime.ACTIVE_EVIDENCE.get() is None


def test_off_reuses_legacy_path_with_zero_ledger(monkeypatch):
    monkeypatch.setattr(runtime, "MAX_LEDGER", 0)
    instance, context, _, _ = setup(monkeypatch, "positive")
    instance.boundary_mode = "off"
    assert any(SOURCE[r.start:r.end] == "Mira Meadow" for r in instance.analyze(
        SOURCE, boundary_context=context,
    ))
    assert runtime.ACTIVE_EVIDENCE.get() is None


@pytest.fixture
def wire_stub(monkeypatch):
    monkeypatch.setattr(server.ner_recognizer, "require_ready", lambda: None)
    monkeypatch.setattr(server.analyzer, "analyze", lambda **_: [])
    monkeypatch.setattr(server, "_analyze_document_base64", lambda _: [])
    monkeypatch.setattr(server.ner_recognizer, "_boundary_recheck", "off")

    def synthetic(text, **kwargs):
        if "Mercier" not in text:
            return []
        anchor = text.index("Mercier")
        return [RecognizerResult(
            "PERSON", anchor, anchor + 7, .9,
            recognition_metadata={runtime.METADATA_KEY: ((anchor, anchor + 7, "PERSON", .9),)},
        )]

    monkeypatch.setattr(server.ner_recognizer, "analyze", synthetic)


def request(payload):
    async def call():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=server.app), base_url="http://local-test",
        ) as client:
            return await client.post("/api/v1/analyze", json=payload)
    return asyncio.run(call())


@pytest.mark.parametrize("kind", ["text", "json_arguments"])
def test_http_text_and_synthetic_json_proof_use_original_coordinates(wire_stub, kind):
    body = "comment Mercier"
    if kind == "json_arguments":
        body = json.dumps({"value": "Élodie Mercier"}, ensure_ascii=True, indent=2)
    answer = request({"text": body, "input_kind": kind, "boundary_coverage_version": 1})
    assert answer.status_code == 200, answer.text
    proof = answer.json()["boundary_coverage"]
    assert proof["text_length"] == len(body) and proof["input_kind"] == kind
    assert [(r["start"], r["end"]) for r in proof["spans"]] == [
        (body.index("Mercier"), body.index("Mercier") + 7),
    ]


def test_http_legacy_omits_coverage_unless_fallback_requires_it(wire_stub):
    legacy = request({"text": "ordinary value"})
    assert legacy.status_code == 200 and "boundary_coverage" not in legacy.json()
    modern = request({"text": "ordinary value", "boundary_coverage_version": 1})
    assert modern.status_code == 200 and modern.json()["boundary_coverage"]["spans"] == []
    unsafe_legacy = request({"text": "comment Mercier"})
    assert unsafe_legacy.status_code == 503
    assert unsafe_legacy.json()["detail"]["failure_class"] == "boundary_positive_coverage_failed"
