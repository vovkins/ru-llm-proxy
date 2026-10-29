"""Controlled NER findings: coverage survives value repair and final merging."""

import itertools
import json
import random
from pathlib import Path

import pytest
from presidio_analyzer import RecognizerResult

from presidio.ner.span_postprocessing import postprocess_spans
from presidio.result_merging import (
    DETECTION_SOURCE_METADATA_KEY,
    SOURCE_NER,
    SOURCE_STRUCTURAL,
    merge_results,
)


def repaired_results(text, spans, *, entities=None, threshold=0.35):
    eligible = [s for s in spans if s[3] >= threshold and (entities is None or s[2] in entities)]
    repaired = postprocess_spans(text, eligible, "secrets-contracts")
    findings = [RecognizerResult(label, start, end, score, recognition_metadata={
        DETECTION_SOURCE_METADATA_KEY: SOURCE_NER,
    }) for start, end, label, score in repaired]
    return merge_results(text, findings, requested_entities=entities, score_threshold=threshold).results


def finding(text, value, kind="PASSWORD", score=0.9):
    start = text.index(value)
    return start, start + len(value), kind, score


CASES = json.loads((Path(__file__).resolve().parents[2] / "tests/fixtures/ner-value-repair.json").read_text())


@pytest.mark.parametrize("case", CASES, ids=lambda c: c["id"])
def test_shared_wire_cases_match_actual_postprocessing_and_merging(case):
    text = case["text"]
    results = repaired_results(text, [finding(text, value, kind) for value, kind in case["pieces"]])
    assert [[text[r.start:r.end], r.entity_type] for r in results] == case["expected"]
    mask_restore(text, results)


def test_native_db_url_contains_repaired_components_without_weakening_egress_checks():
    from presidio.recognizers.infra_secrets import CredentialUrlRecognizer
    case = next(case for case in CASES if case["id"] == "uri-components")
    text = case["text"]
    ner = repaired_results(text, [finding(text, value, kind) for value, kind in case["pieces"]])
    native = CredentialUrlRecognizer().analyze(text, entities=["DB_URL"])
    for result in native:
        result.recognition_metadata[DETECTION_SOURCE_METADATA_KEY] = SOURCE_STRUCTURAL
    results = merge_results(text, [*ner, *native]).results
    assert [[text[r.start:r.end], r.entity_type] for r in results] == case["analyzer_expected"]
    mask_restore(text, results)


def mask_restore(text, results):
    assert all(a.end <= b.start for a, b in zip(results, results[1:]))
    mapping = {f"<E4_{i}>": text[r.start:r.end] for i, r in enumerate(results)}
    masked = text
    for i, r in reversed(list(enumerate(results))):
        masked = masked[:r.start] + f"<E4_{i}>" + masked[r.end:]
    restored = masked
    for token, value in mapping.items():
        restored = restored.replace(token, value)
    assert restored == text
    return masked


def test_cross_type_expansion_never_loses_the_original_person_prefix():
    text = "Иван Петровsecret12"
    inputs = [finding(text, "Иван Петров", "PERSON"), finding(text, "secret12")]
    for order in itertools.permutations(inputs):
        results = repaired_results(text, list(order))
        covered = {i for r in results for i in range(r.start, r.end)}
        assert all(text[i].isspace() or i in covered for i in range(len(text)))
        assert "Иван" not in mask_restore(text, results)


@pytest.mark.parametrize("value", ["a/b+c?d!", "Alp42!", "Alp42?", "Alp42.", "Alp42!?.", "abc=="])
def test_bare_assignment_preserves_all_significant_symbols(value):
    text = f"PASSWORD={value} next=visible"
    results = repaired_results(text, [finding(text, value[:2])])
    assert [(r.start, r.end) for r in results] == [(text.index(value), text.index(value) + len(value))]
    assert mask_restore(text, results).endswith(" next=visible")


@pytest.mark.parametrize("value", ["Alp/ha!?", "two words!", "ab\"cd\\!", "Ива\u0301н🙂!?", "a=b=="])
def test_quoted_value_expands_without_consuming_json_syntax(value):
    text = json.dumps({"password": value, "next": "visible"}, ensure_ascii=False)
    start = text.index(': "') + 3
    results = repaired_results(text, [(start, start + 1, "PASSWORD", 0.9)])
    assert json.loads(mask_restore(text, results)) == {"password": "<E4_0>", "next": "visible"}


@pytest.mark.parametrize("quoted", [False, True])
def test_uri_user_and_password_are_never_joined(quoted):
    value = "postgresql://alice:Pa%40ss!@db:5432/app"
    text = json.dumps({"dsn": value, "next": "visible"}) if quoted else "DB=" + value
    spans = [finding(text, "ali", "LOGIN"), finding(text, "40s")]
    results = repaired_results(text, spans)
    assert [(text[r.start:r.end], r.entity_type) for r in results] == [("alice", "LOGIN"), ("Pa%40ss!", "PASSWORD")]
    masked = mask_restore(text, results)
    assert "://<E4_0>:<E4_1>@db:5432/app" in masked
    if quoted:
        assert json.loads(masked)["next"] == "visible"


def test_adjacent_fields_and_array_values_remain_separate():
    text = '{"values":["abc/def!","ghi?jkl"],"next":"visible"}'
    results = repaired_results(text, [finding(text, "def"), finding(text, "ghi")])
    assert json.loads(mask_restore(text, results)) == {"values": ["<E4_0>", "<E4_1>"], "next": "visible"}


@pytest.mark.parametrize("text,value", [
    ("PASSWORD:abc:def! next=visible", "abc:def!"),
    ("PASSWORD=secret:two! next=visible", "secret:two!"),
    ("unlabelled:secret! next=visible", "unlabelled:secret!"),
    ("PASSWORD='ab/c!?' next=visible", "ab/c!?"),
    ("SELECT 'ab/c!?' AS password, 'visible' AS other", "ab/c!?"),
])
def test_colons_and_single_quoted_values_keep_only_the_proven_value(text, value):
    start = text.index(value) + len(value) - 2
    results = repaired_results(text, [(start, start + 1, "PASSWORD", .9)])
    assert [text[r.start:r.end] for r in results] == [value]
    assert "visible" in mask_restore(text, results)


def test_uri_query_values_and_credentials_are_not_joined():
    text = "https://alice:Pa%40ss!@service.local/run?key=Ab/C+!?&other=Second%21#visible"
    results = repaired_results(text, [finding(text, "40s"),
                                     finding(text, "C+", "AUTH_TOKEN"),
                                     finding(text, "Second", "SECRET_KEY")])
    assert [text[r.start:r.end] for r in results] == ["Pa%40ss!", "Ab/C+!?", "Second%21"]
    assert mask_restore(text, results).endswith("#visible")


def test_invalid_uri_does_not_crash_or_shrink_original_finding():
    text = "PASSWORD=https://[invalid/abc!? next=visible"
    span = finding(text, "abc")
    repaired = postprocess_spans(text, [span], "secrets")
    assert any(a <= span[0] < span[1] <= b for a, b, _, _ in repaired)
    assert all(b <= text.index(" next") for _, b, _, _ in repaired)


def test_score_and_type_are_not_chosen_by_fragment_length():
    text = "token=abc+defghijk"
    spans = [finding(text, "abc", "PASSWORD", 0.95), finding(text, "defghijk", "SECRET_KEY", 0.6)]
    results = repaired_results(text, spans)
    assert [(r.entity_type, r.score) for r in results] == [("PASSWORD", 0.95)]
    assert text[results[0].start:results[0].end] == "abc+defghijk"


def test_entity_and_score_filters_do_not_promote_excluded_fragments():
    text = "password=alpha42! token=beta43?"
    spans = [finding(text, "alpha", "PASSWORD", .35), finding(text, "beta", "AUTH_TOKEN", .9)]
    results = repaired_results(text, spans, entities=["PASSWORD"], threshold=.35)
    assert [text[r.start:r.end] for r in results] == ["alpha42!"]
    assert repaired_results(text, spans, entities=["PASSWORD"], threshold=.350001) == ()


def test_contract_slashes_and_repeated_values_keep_distinct_offsets():
    text = 'Договор № "2026/АГ/6581"; договор № "2026/АГ/6581"'
    spans = [(i, i + 4, "CONTRACT_NUMBER", .9)
             for i in range(len(text)) if text.startswith("2026", i)]
    results = repaired_results(text, spans)
    assert [text[r.start:r.end] for r in results] == ["2026/АГ/6581"] * 2
    assert mask_restore(text, results).count("Договор") == 1


def test_nested_and_chained_overlaps_preserve_eligible_nonspace_coverage():
    text = "Иван Петровabc+def! next=visible"
    inputs = [(0, 11, "PERSON", .9), (5, 14, "PASSWORD", .8),
              (12, 17, "AUTH_TOKEN", .7), (14, 18, "SECRET_KEY", .6)]
    expected = {i for start, end, _, _ in inputs for i in range(start, end) if not text[i].isspace()}
    for order in itertools.permutations(inputs):
        results = repaired_results(text, list(order))
        covered = {i for r in results for i in range(r.start, r.end)}
        assert expected <= covered
        assert mask_restore(text, results).endswith(" next=visible")


def test_generated_value_repair_never_shrinks_or_relabels_an_input_finding():
    text = 'PASSWORD=abc==; {"password":"a/b+c?d!","next":"visible"} postgres://u:p%40ss!@db'
    rng = random.Random(72)
    inputs = []
    for _ in range(250):
        start = rng.randrange(len(text))
        end = rng.randrange(start + 1, len(text) + 1)
        inputs.append((start, end, rng.choice(["PASSWORD", "AUTH_TOKEN", "PERSON"]), rng.random()))
    output = postprocess_spans(text, inputs, "secrets-contracts")
    for start, end, label, score in inputs:
        assert any(a <= start < end <= b and kind == label and value == score
                   for a, b, kind, value in output)


def test_long_shared_value_is_indexed_once_not_rescanned_per_fragment(monkeypatch):
    from presidio.ner import span_postprocessing as module
    text = "PASSWORD=" + "a" * 100000 + "! next=visible"
    spans = [(i, i + 2, "PASSWORD", .9) for i in range(10, 90000, 30)]
    calls = []
    original = module._ASSIGNMENT

    class Probe:
        def match(self, *args):
            calls.append(1)
            return original.match(*args)

    monkeypatch.setattr(module, "_ASSIGNMENT", Probe())
    assert postprocess_spans(text, spans, "secrets") == [(9, 100010, "PASSWORD", .9)]
    assert len(calls) == 1
