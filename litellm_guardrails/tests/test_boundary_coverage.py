"""Exercise the ON proof contract without requiring a live Analyzer."""

import asyncio
import json

import pytest

from litellm_guardrails import guard_boundary as boundary
from litellm_guardrails import pii_guardrail as pg
from litellm_guardrails.boundary_contract import Coverage, MAX_SPANS
from litellm_guardrails.tests.test_pii_guardrail import _MemoryRedis

TEXT = "Example sample"
SIGNATURE = "c" * 64
THRESHOLD = pg.ANALYSIS_CACHE_SCORE_THRESHOLD
MASK = {"start": 0, "end": 7, "entity_type": "PERSON", "score": .9}
REQUIRED = [(0, 7, "PERSON", .9)]
CONTEXT = {"analyzer_signature": SIGNATURE, "scope": "local", "model": "echo"}


def coverage(text=TEXT, spans=REQUIRED):
    return Coverage.create(text, spans, threshold=THRESHOLD, signature=SIGNATURE)


@pytest.fixture
def guard():
    instance = pg.RuPIIGuardrail(dictionary_substitutions_enabled=False)
    instance._boundary_coverage_enabled = True
    instance._analysis_cache_secret = b"test-cache-secret"
    instance._redis = _MemoryRedis()
    return instance


def test_round_trip_checks_exact_text_and_union_of_masks():
    proof = Coverage.parse(
        coverage().wire(), text=TEXT, expected_kind="text", expected_threshold=THRESHOLD,
    )
    assert proof == coverage()
    assert proof.missing([(0, 3), (3, 7)]) == ()
    assert proof.missing([(0, 3)]) == tuple(REQUIRED)
    assert proof.missing([(0, 3)], [(3, 7)]) == ()
    with pytest.raises(ValueError):
        proof.bind("Changed sample")


@pytest.mark.parametrize("field,value", [
    ("text_length", 1), ("text_sha256", "f" * 64), ("score_threshold", .31),
    ("input_kind", "json_arguments"), ("analysis_signature", "invalid"),
    ("extra", 0), ("spans", [MASK] * (MAX_SPANS + 1)),
])
def test_reject_incompatible_proof(field, value):
    packet = coverage().wire()
    packet[field] = value
    with pytest.raises(ValueError):
        Coverage.parse(packet, text=TEXT, expected_kind="text", expected_threshold=THRESHOLD)


def test_missing_field_and_over_budget_creation():
    packet = coverage().wire()
    del packet["analysis_signature"]
    with pytest.raises(ValueError):
        Coverage.parse(packet, text=TEXT)
    with pytest.raises(ValueError):
        Coverage.create("x" * (MAX_SPANS + 1), [(i, i + 1, "PERSON", .9) for i in range(MAX_SPANS + 1)])


def test_absent_proof_and_wrong_signature_fail_closed():
    with pytest.raises(pg.AnalyzerUnavailableError) as error:
        boundary.decode_response(TEXT, {"entities": []}, "text", THRESHOLD, pg.AnalyzerUnavailableError)
    assert error.value.failure_class == "boundary_positive_coverage_failed"
    token = boundary.CACHE_CONTEXT.set(boundary.BoundaryCacheContext(TEXT, "k", b"secret", "b" * 64))
    try:
        with pytest.raises(pg.AnalyzerUnavailableError) as error:
            boundary.decode_response(
                TEXT, {"entities": [], "boundary_coverage": coverage().wire()},
                "text", THRESHOLD, pg.AnalyzerUnavailableError,
            )
        assert error.value.boundary_cache_key == "k"
    finally:
        boundary.CACHE_CONTEXT.reset(token)


def test_final_render_consumes_emitted_ranges_and_allowlist_cannot_drop_proof(guard):
    original = boundary.AnalysisEntities([MASK], coverage(), "cache-key")
    result = boundary.retain_result(original, [])
    assert result and len(result) == 0 and result.coverage == original.coverage
    with pytest.raises(pg.AnalyzerUnavailableError) as error:
        guard._mask_text(TEXT, result)
    assert error.value.boundary_cache_key == "cache-key"
    assert guard._mask_text(TEXT, result, exclusion_spans=((0, 7),)) == (TEXT, {})
    masked, mapping = guard._mask_text(TEXT, original)
    assert "Example" not in masked and pg.restore_text(masked, mapping) == TEXT
    assert not hasattr(mapping, "boundary_mask_ranges")


@pytest.mark.parametrize("corruption", [None, "bad_mac", "wrong_binding", "legacy"])
def test_cache_round_trip_and_corruption_refetch(guard, monkeypatch, corruption):
    async def run():
        calls = 0

        async def analyze(text):
            nonlocal calls
            calls += 1
            assert text == TEXT
            return boundary.AnalysisEntities([{**MASK, "text": "Example"}], coverage())

        monkeypatch.setattr(guard, "_analyze_text", analyze)
        first = await guard._analyze_text_with_cache(TEXT, CONTEXT, "request1", 0)
        raw = guard._redis.values[first.cache_key]
        packet = json.loads(raw)
        assert packet["schema"] == 2 and "proof_mac" in packet
        assert "text_sha256" not in raw and "Example" not in raw and TEXT not in raw
        if corruption == "bad_mac":
            packet["proof_mac"] = "bad"
        elif corruption == "wrong_binding":
            packet["cache_binding"] = "wrong"
        elif corruption == "legacy":
            packet = {"schema": pg.ANALYSIS_CACHE_SCHEMA_VERSION, "text_length": len(TEXT),
                      "entities": [MASK]}
        if corruption:
            guard._redis.values[first.cache_key] = json.dumps(packet)
        again = await guard._analyze_text_with_cache(TEXT, CONTEXT, "request2", 0)
        assert calls == (2 if corruption else 1)
        assert again is not first and again.coverage == first.coverage
        assert boundary.CACHE_CONTEXT.get() is None

    asyncio.run(run())


def test_cancelled_waiter_keeps_shared_analysis_and_other_waiter_has_a_copy(guard, monkeypatch):
    async def run():
        started = asyncio.Event()
        release = asyncio.Event()
        calls = 0

        async def analyze(text):
            nonlocal calls
            calls += 1
            started.set()
            await release.wait()
            return boundary.AnalysisEntities([MASK], coverage())

        monkeypatch.setattr(guard, "_analyze_text", analyze)
        tasks = [asyncio.create_task(guard._analyze_text_with_cache(TEXT, CONTEXT, str(i), 0))
                 for i in range(3)]
        await started.wait()
        await asyncio.sleep(0)
        tasks[0].cancel()
        await asyncio.gather(tasks[0], return_exceptions=True)
        release.set()
        one, two = await asyncio.gather(*tasks[1:])
        assert calls == 1 and one is not two and one.coverage == two.coverage == coverage()
        assert boundary.CACHE_CONTEXT.get() is None

    asyncio.run(run())


def test_pre_call_evicts_cache_when_final_mask_loses_required_span(guard, monkeypatch):
    async def run():
        async def prep(*args, **kwargs):
            return CONTEXT

        async def analyze(text):
            assert text == TEXT
            return boundary.AnalysisEntities([MASK], coverage())

        monkeypatch.setattr(guard, "_prepare_analysis_cache_context", prep)
        monkeypatch.setattr(guard, "_analyze_text", analyze)
        monkeypatch.setattr(guard, "_normalize_entities", lambda *a, **k: [])
        request = {"model": "echo", "messages": [{"role": "user", "content": TEXT}]}
        with pytest.raises(pg.ProxyException) as error:
            await guard.async_pre_call_hook(None, None, request, "completion")
        assert error.value.status_code == 503
        assert guard._analysis_cache_key(TEXT, CONTEXT) not in guard._redis.values
        assert request["messages"][0]["content"] == TEXT

    asyncio.run(run())


def test_off_keeps_plain_cache_payload_and_mask_bytes(guard):
    guard._boundary_coverage_enabled = False
    raw = guard._serialize_analysis_cache_entry(TEXT, [MASK])
    assert raw == '{"entities":[{"end":7,"entity_type":"PERSON","score":0.9,"start":0}],"schema":1,"text_length":14}'
    assert guard._deserialize_analysis_cache_entry(raw, len(TEXT)) == [MASK]
    assert guard._mask_text(TEXT, [MASK])[0] == "<PERSON_1> sample"
