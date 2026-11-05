"""General container recovery without source-specific words or URL decoding."""

import json
from pathlib import Path
import random

import pytest
from presidio_analyzer import RecognizerResult

from presidio.ner.boundary_values import BoundaryContext, BoundaryValues
from presidio.ner.triton_recognizer import NERProcessingError, NERWindowBoundaryError, merge_window_predictions
from presidio.ner.text_processing import normalize_for_ner
from presidio.result_merging import (
    DETECTION_SOURCE_METADATA_KEY, NER_BOUNDARY_CONTAINER_KEY, SOURCE_NER,
    boundary_coverage_preserved, merge_results,
)
from presidio.tests.test_ner_window_value_repair import prediction, recognizer

CASES = json.loads((Path(__file__).resolve().parents[2] / 'tests/fixtures/ner-boundary-values.json').read_text())


@pytest.mark.parametrize('case', CASES, ids=lambda case: case['id'])
def test_shared_wire_cases_are_verified_against_actual_boundary_and_merge_code(monkeypatch, case):
    source = case['text']
    normalized = normalize_for_ner(source).text
    initial = [prediction(kind, normalized.index(value), normalized.index(value) + len(value),
                          right=True) for value, kind in case['pieces']]
    instance, _, _ = recognizer(monkeypatch, initial, [])
    candidates = instance.analyze(source)
    results = merge_results(source, candidates, score_threshold=.35).results
    assert [[source[r.start:r.end], r.entity_type] for r in results] == case['expected']
    assert boundary_coverage_preserved(candidates, results)


@pytest.mark.parametrize("source,value,fragment", [
    ('See https://example.test/doc/%D0%9F%D0%B5?mode=read next', '%D0%9F%D0%B5', 'D0'),
    ('See /docs/%D0%9F%D0%B5 next', '%D0%9F%D0%B5', 'D0'),
    ('See docs/%D0%9F%D0%B5 next', '%D0%9F%D0%B5', 'D0'),
    ('See https://example.test/?token=ab%2Fcd&other=visible next', 'ab%2Fcd', '2F'),
    ('See https://example.test/#part%D0%B0 next', 'part%D0%B0', 'D0'),
    ('See postgresql://user:pass%2Fword@host/db next', 'pass%2Fword', '2F'),
    ('{"person":"Ivan Petrov","keep":"visible"}', 'Ivan Petrov', 'Pet'),
    ('header\n\nOTHER\n\ntail', 'OTHER', 'THE'),
    ('header\n\nIvan\nPetrov\n\ntail', 'Ivan\nPetrov', 'Ivan'),
    ('header\n\nReference (/docs/one)\n\ntail', '(/docs/one)', ')'),
    ('head\n\n```\nReference (/docs/one)\n```\n\ntail', '(/docs/one)', ')'),
    ('head\nSee <https://example.test/path> after', '<https://example.test/path>', '>'),
    ('See https://example.test/?token=abc) after', 'abc)', 'bc'),
    ('See (https://example.test/?token=abc)) after', 'abc)', 'bc'),
])
def test_smallest_complete_container_keeps_raw_source(source, value, fragment):
    start = source.index(fragment)
    bounds = BoundaryValues(source).resolve(start, start + len(fragment))
    assert bounds is not None
    assert source[slice(*bounds)] == value


@pytest.mark.parametrize("source,fragment", [
    ('only one paragraph', 'one'),
    ('  only one paragraph\n\n', 'one'),
    ('whole/path', 'whole/path'),
    ('{"a":"abc","b":"def"}', 'abc","b":"def'),
    ('head\n\n{"unfinished": abc\n\ntail', 'abc'),
    ('head\n\n```python\nprint(abc)\n```\n\ntail', 'abc'),
    ('head\n\n```python\n\nprint(abc)\n\ntail', 'abc'),
])
def test_unknown_syntax_or_whole_document_is_not_a_fallback(source, fragment):
    start = source.index(fragment)
    assert BoundaryValues(source).resolve(start, start + len(fragment)) is None


def test_url_components_do_not_decode_reserved_delimiters():
    source = 'See https://example.test/?one=ab%26two%3Dsecret&two=visible after'
    start = source.index('secret')
    bounds = BoundaryValues(source).resolve(start, start + 6)
    assert source[slice(*bounds)] == 'ab%26two%3Dsecret'
    assert source[bounds[1]:].startswith('&two=visible')


@pytest.mark.parametrize("kind", ["PERSON", "LOCATION", "ORGANIZATION", "LOGIN", "PASSWORD",
                                  "AUTH_TOKEN", "SECRET_KEY", "CONTRACT_NUMBER"])
def test_all_ner_classes_recover_a_container_without_relabelling(monkeypatch, kind):
    source = '{"value":"Ivan Petrov","keep":"visible"}'
    start = source.index('Pet')
    instance, _, calls = recognizer(monkeypatch, [prediction(kind, start, start + 3, left=True)], [])
    result, = instance.analyze(source)
    assert (result.entity_type, source[result.start:result.end], result.score) == (kind, 'Ivan Petrov', .8)
    assert calls == [True]
    assert instance.is_ready()


def test_shorter_complete_prediction_cannot_discard_cut_evidence():
    initial = prediction('PERSON', 10, 25, left=True)
    shorter = prediction('PERSON', 15, 20, 1)
    with pytest.raises(NERWindowBoundaryError):
        merge_window_predictions([initial, shorter])


def test_overlapping_complete_predictions_keep_union():
    results = merge_window_predictions([
        prediction('PERSON', 10, 20, score=.9),
        prediction('PERSON', 15, 25, 1, score=.8),
    ])
    assert [(r.start, r.end, r.score) for r in results] == [(10, 25, .8)]


def test_shifted_shorter_prediction_expands_instead_of_losing_tails(monkeypatch):
    source = 'header\n\nIvan Petrov\n\ntail'
    start = source.index('Ivan')
    initial = prediction('PERSON', start, start + len('Ivan Petrov'), left=True)
    shifted = prediction('PERSON', start + 2, start + 8, 1)
    instance, _, _ = recognizer(monkeypatch, [initial], [shifted])
    result, = instance.analyze(source)
    assert (result.start, result.end) == (start, start + len('Ivan Petrov'))
    assert result.recognition_metadata[NER_BOUNDARY_CONTAINER_KEY] is True


def test_recovery_retains_other_types_discovered_in_shifted_windows(monkeypatch):
    source = '{"name":"Ivan Petrov","password":"uvwxyz"}'
    start, other = source.index('Ivan'), source.index('uvwxyz')
    instance, _, _ = recognizer(monkeypatch, [prediction('PERSON', start, start + 2, left=True)], [
        prediction('PERSON', start, start + len('Ivan Petrov'), 1),
        prediction('PASSWORD', other, other + 6, 1),
    ])
    results = instance.analyze(source)
    assert {(r.entity_type, source[r.start:r.end]) for r in results} == {
        ('PERSON', 'Ivan Petrov'), ('PASSWORD', 'uvwxyz'),
    }


def test_original_document_bounds_can_cross_analyzer_chunks(monkeypatch):
    source = 'header\n\nIvan Petrov\n\ntail'
    offset = source.index('Petrov')
    local = source[offset:]
    instance, _, _ = recognizer(monkeypatch, [prediction('PERSON', 0, 3, left=True)], [])
    result, = instance.analyze(local, boundary_context=BoundaryContext(BoundaryValues(source), offset))
    assert (offset + result.start, offset + result.end) == (source.index('Ivan'), source.index('\n\ntail'))
    assert result.start < 0


def test_artificial_chunk_edge_is_not_a_confirmed_paragraph_end(monkeypatch):
    source = 'Ivan Petrov and more text in the same paragraph'
    local = source[:11]
    instance, _, _ = recognizer(monkeypatch, [prediction('PERSON', 0, 4, right=True)], [])
    with pytest.raises(NERProcessingError) as error:
        instance.analyze(local, boundary_context=BoundaryContext(BoundaryValues(source)))
    assert error.value.failure_class == 'window_boundary_unresolved'
    assert instance.is_ready()


def test_boundary_mask_survives_name_filters_and_overlap_without_losing_spaces():
    source = 'header\n\nCompany Alpha: Beta.\n\ntail'
    start, end = source.index('Company'), source.index('\n\ntail')
    container = RecognizerResult('ORGANIZATION', start, end, .7, recognition_metadata={
        DETECTION_SOURCE_METADATA_KEY: SOURCE_NER, NER_BOUNDARY_CONTAINER_KEY: True,
    })
    inner = RecognizerResult('SECRET_KEY', start + 7, end - 1, .99)
    result = merge_results(source, [container, inner], score_threshold=.35)
    assert boundary_coverage_preserved([container], result.results)
    covered = {i for r in result.results for i in range(r.start, r.end)}
    assert set(range(start, end)) <= covered


def test_boundary_mask_preserves_escaped_json_value_and_request_filter():
    source = json.dumps({'value': 'first\nsecond"quote', 'keep': 'visible'})
    start, end = source.index('first'), source.index('", "keep"')
    container = RecognizerResult('PERSON', start, end, .7, recognition_metadata={
        DETECTION_SOURCE_METADATA_KEY: SOURCE_NER, NER_BOUNDARY_CONTAINER_KEY: True,
    })
    result = merge_results(source, [container], score_threshold=.35)
    item, = result.results
    assert (item.start, item.end) == (start, end)
    assert json.loads(source[:start] + '<PERSON_1>' + source[end:])['keep'] == 'visible'
    assert merge_results(source, [container], requested_entities=['PASSWORD']).results == ()
    assert merge_results(source, [container], score_threshold=.9).results == ()


def test_coverage_guard_detects_a_lost_boundary_tail():
    candidate = RecognizerResult('PERSON', 0, 6, .7, recognition_metadata={
        DETECTION_SOURCE_METADATA_KEY: SOURCE_NER, NER_BOUNDARY_CONTAINER_KEY: True,
    })
    assert not boundary_coverage_preserved([candidate], [RecognizerResult('PERSON', 1, 6, .7)])
    assert boundary_coverage_preserved([candidate], [RecognizerResult('PERSON', 0, 2, .7),
                                                   RecognizerResult('PASSWORD', 2, 6, .8)])


def test_partial_bare_scalar_cannot_expand_to_the_whole_document(monkeypatch):
    instance, _, _ = recognizer(monkeypatch, [prediction('PASSWORD', 2, 4, right=True)], [])
    with pytest.raises(NERProcessingError) as error:
        instance.analyze('abcdef')
    assert error.value.failure_class == 'window_boundary_unresolved'


def test_url_escape_inside_json_uses_the_complete_literal():
    source = json.dumps({'value': 'https://example.test/x"y', 'keep': 'visible'})
    start = source.index('x')
    bounds = BoundaryValues(source).resolve(start, source.index('y') + 1)
    assert source[slice(*bounds)] == 'https://example.test/x\\"y'
    assert json.loads(source[:bounds[0]] + '<PERSON_1>' + source[bounds[1]:])['keep'] == 'visible'


def test_window_merge_never_loses_evidence_under_many_offset_combinations():
    randomizer = random.Random(110)
    for _ in range(300):
        predictions = [prediction('PERSON', randomizer.randrange(0, 8), randomizer.randrange(9, 20), i,
                                  left=randomizer.choice([False, True]),
                                  right=randomizer.choice([False, True])) for i in range(4)]
        try:
            results = merge_window_predictions(predictions)
        except NERWindowBoundaryError:
            continue
        for p in predictions:
            assert any(r.start <= p.entity.start < p.entity.end <= r.end for r in results)


@pytest.mark.parametrize('separator', ['\n', '\r\n', '\r', '\u2028', '\u2029'])
def test_paragraph_boundaries_preserve_original_line_endings(separator):
    source = f'Header{separator}{separator}OTHER{separator}{separator}Tail'
    start = source.index('THE')
    bounds = BoundaryValues(source).resolve(start, start + 3)
    assert source[slice(*bounds)] == 'OTHER'


def test_a_single_crlf_is_not_a_blank_line_or_a_value_boundary():
    source = 'Ivan\r\nPetrov'
    assert BoundaryValues(source).resolve(0, 4) is None


@pytest.mark.parametrize('separator', ['\n', '\r\n', '\r', '\u2028', '\u2029'])
@pytest.mark.parametrize('marker,closing', [('```', '```'), ('~~~~', '~~~~~')])
@pytest.mark.parametrize('padding', ['', '\n'])
def test_closed_fence_has_bounded_plain_text_paragraphs(separator, marker, closing, padding):
    blank = separator if padding else ''
    source = f'Header{separator}{separator}{marker}text{separator}{blank}OTHER{blank}{separator}{closing}{separator}Tail'
    start = source.index('THE')
    bounds = BoundaryValues(source).resolve(start, start + 3)
    assert bounds is not None
    assert source[slice(*bounds)] == 'OTHER'
    assert bounds[0] <= start < start + 3 <= bounds[1]
    assert marker not in source[slice(*bounds)]


@pytest.mark.parametrize('value', ['OTHER', 'Another ordinary value.', 'first\nsecond',
                                  'Ame\u0301lie Petrov', '\u0418\u041b\u0418', '\u4e00\u4e2a\u503c'])
def test_fenced_paragraph_rule_does_not_depend_on_a_word_or_alphabet(value):
    source = f'Header\n\n```text\n\n{value}\n\n```\n\nTail'
    start = source.index(value)
    bounds = BoundaryValues(source).resolve(start, start + 1)
    assert source[slice(*bounds)] == value


@pytest.mark.parametrize('body,closing', [
    ('plain value', ''),
    ('plain value', '``` trailing text'),
    ('plain value', '~~~'),
    ('plain value', '``'),
    ('print(abc)', '```'),
    ('x = abc', '```'),
    ('abc; exit', '```'),
    ('{"one":abc,"two":def}', '```'),
    ('[abc, def]', '```'),
    ('unfinished "abc', '```'),
])
@pytest.mark.parametrize('separator', ['\n', '\r\n', '\r', '\u2028', '\u2029'])
def test_fenced_fallback_does_not_infer_unknown_syntax_or_unclosed_bodies(body, closing, separator):
    source = f'Header\n\n```python\n\n{body}\n\n{closing}\n\nTail'
    source = source.replace('\n', separator)
    start = source.index(body)
    assert BoundaryValues(source).resolve(start, start + len(body)) is None


def test_fenced_fallback_cannot_join_paragraphs_or_cover_delimiters():
    source = 'Header\n\n```text\nFIRST\n\nSECOND\n```\n\nTail'
    values = BoundaryValues(source)
    assert values.resolve(source.index('FIRST'), source.index('SECOND') + 6) is None
    assert values.resolve(source.index('```'), source.index('FIRST') + 5) is None
    assert values.resolve(source.index('SECOND'), source.rindex('```') + 3) is None
    assert values.resolve(0, len(source)) is None


def test_closed_and_unclosed_fences_are_not_combined():
    source = '```text\nFIRST\n```\n\n```text\n\nSECOND\n\nTail'
    values = BoundaryValues(source)
    start = source.index('FIRST')
    bounds = values.resolve(start, start + 1)
    assert source[slice(*bounds)] == 'FIRST'
    start = source.index('SECOND')
    assert values.resolve(start, start + 1) is None


def test_invalid_backtick_info_does_not_authorize_a_paragraph_fallback():
    source = 'Header\n\n```text`invalid\n\nplain value\n\n```\n\nTail'
    start = source.index('plain value')
    assert BoundaryValues(source).resolve(start, start + 5) is None


@pytest.mark.parametrize('kind', ['PERSON', 'LOCATION', 'ORGANIZATION', 'LOGIN', 'PASSWORD',
                                 'AUTH_TOKEN', 'SECRET_KEY', 'CONTRACT_NUMBER'])
def test_fenced_recovery_preserves_initial_and_shifted_coverage(monkeypatch, kind):
    source = 'Header\n\n```text\n\nFIRST SECOND\n\n```\n\nTail'
    start = source.index('FIRST')
    initial = prediction(kind, start + 1, start + 5, right=True)
    shifted = prediction(kind, start + 3, start + 9, 1, right=True)
    instance, _, _ = recognizer(monkeypatch, [initial], [shifted])
    candidates = instance.analyze(source)
    result, = merge_results(source, candidates, score_threshold=.35).results
    assert (result.entity_type, source[result.start:result.end]) == (kind, 'FIRST SECOND')
    assert result.start <= initial.entity.start < initial.entity.end <= result.end
    assert result.start <= shifted.entity.start < shifted.entity.end <= result.end
    assert boundary_coverage_preserved(candidates, [result])


def test_fenced_container_uses_document_coordinates_across_analyzer_chunks(monkeypatch):
    source = 'Header\n\n```text\n\nFIRST SECOND\n\n```\n\nTail'
    offset = source.index('SECOND')
    local = source[offset:]
    instance, _, _ = recognizer(monkeypatch, [prediction('PERSON', 0, 3, left=True)], [])
    result, = instance.analyze(local, boundary_context=BoundaryContext(BoundaryValues(source), offset))
    assert source[offset + result.start:offset + result.end] == 'FIRST SECOND'
    assert result.start < 0


def test_one_unresolved_cluster_refuses_the_whole_request_without_poisoning_ner(monkeypatch):
    source = '{"good":"Ivan Petrov","a":"abc","b":"def"}'
    name, start, end = source.index('Ivan'), source.index('abc'), source.index('def') + 3
    instance, _, _ = recognizer(monkeypatch, [prediction('PERSON', name, name + 2, left=True),
                                             prediction('LOCATION', start, end, right=True)], [])
    with pytest.raises(NERProcessingError) as error:
        instance.analyze(source)
    assert error.value.failure_class == 'window_boundary_unresolved'
    assert instance.is_ready()
    monkeypatch.setattr(instance, '_predict_window_entities', lambda *_a, **_kw: ([], 1))
    assert instance.analyze('control') == []


def test_source_context_mismatch_is_a_request_scoped_mapping_error(monkeypatch):
    source = '{"name":"Ivan Petrov"}'
    start = source.index('Ivan')
    instance, _, _ = recognizer(monkeypatch, [prediction('PERSON', start, start + 2, right=True)], [])
    with pytest.raises(NERProcessingError) as error:
        instance.analyze(source, boundary_context=BoundaryContext(BoundaryValues('different source')))
    assert error.value.failure_class == 'offset_mapping_failed'
    assert instance.is_ready()


def test_analyzer_keeps_document_container_across_both_chunk_edges(monkeypatch):
    from presidio import analyzer_server as server
    from presidio.ner.triton_recognizer import NERWindowAnalysis
    source = 'Header\n\nIvan Petrov\n\nTail'
    offset = source.index('Petrov')
    chunks = (server.TextChunk(0, 0, offset + 3), server.TextChunk(1, offset, len(source)))
    instance, _, _ = recognizer(monkeypatch, [], [])
    def predict(text, **kwargs):
        start = text.index('Ivan') if 'Ivan' in text else 0
        return NERWindowAnalysis([prediction('PERSON', start, start + 2,
                                            left=start == 0, right=start != 0)], 2, len(text))
    monkeypatch.setattr(instance, '_predict_window_entities', predict)
    monkeypatch.setattr(server, 'ner_recognizer', instance)
    monkeypatch.setattr(server, 'plan_text_chunks', lambda text: chunks)
    monkeypatch.setattr(server.analyzer, 'analyze', lambda **kwargs: [])
    response = server._analyze_sync(server.AnalyzeRequest(text=source))
    assert [(r['entity_type'], r['text']) for r in response.entities] == [('PERSON', 'Ivan Petrov')]
    assert response.entities[0]['start'] == source.index('Ivan')
    assert response.entities[0]['end'] == source.index('\n\nTail')


def test_analyzer_rejects_missing_boundary_coverage_after_merge(monkeypatch):
    import asyncio
    from types import SimpleNamespace
    from fastapi import HTTPException
    from presidio import analyzer_server as server
    source = 'prefix secret suffix'
    candidate = RecognizerResult('PASSWORD', 7, 13, .8, recognition_metadata={
        DETECTION_SOURCE_METADATA_KEY: SOURCE_NER, NER_BOUNDARY_CONTAINER_KEY: True,
    })
    monkeypatch.setattr(server.ner_recognizer, 'require_ready', lambda: None)
    monkeypatch.setattr(server.ner_recognizer, 'analyze', lambda *args, **kwargs: [candidate])
    monkeypatch.setattr(server.analyzer, 'analyze', lambda **kwargs: [])
    monkeypatch.setattr(server, 'merge_results', lambda *args, **kwargs:
                        SimpleNamespace(results=(RecognizerResult('PASSWORD', 8, 13, .8),), decisions=()))
    with pytest.raises(HTTPException) as error:
        asyncio.run(server._analyze_with_capacity(server.AnalyzeRequest(text=source, entities=['PASSWORD'])))
    assert error.value.status_code == 503
    assert error.value.detail['failure_class'] == 'span_postprocessing_failed'


def test_explicit_name_reclassification_survives_the_boundary_coverage_floor():
    source = '\u0424\u0418\u041e: \u0418\u0432\u0430\u043d \u041f\u0435\u0442\u0440\u043e\u0432'
    start = source.index(':') + 2
    candidate = RecognizerResult('ORGANIZATION', start, len(source), .8, recognition_metadata={
        DETECTION_SOURCE_METADATA_KEY: SOURCE_NER, NER_BOUNDARY_CONTAINER_KEY: True,
    })
    results = merge_results(source, [candidate], requested_entities=['PERSON'], score_threshold=.35).results
    assert [(r.entity_type, r.start, r.end) for r in results] == [('PERSON', start, len(source))]
    assert results[0].recognition_metadata[NER_BOUNDARY_CONTAINER_KEY] is True


def test_an_explicit_name_header_cannot_discard_protected_boundary_evidence():
    source = '\u0424\u0418\u041e: \u0418\u0432\u0430\u043d \u041f\u0435\u0442\u0440\u043e\u0432'
    candidate = RecognizerResult('PERSON', 0, 3, .8, recognition_metadata={
        DETECTION_SOURCE_METADATA_KEY: SOURCE_NER, NER_BOUNDARY_CONTAINER_KEY: True,
    })
    results = merge_results(source, [candidate], score_threshold=.35).results
    assert boundary_coverage_preserved([candidate], results)


def test_cancellation_during_container_lookup_does_not_return_partial_masks(monkeypatch):
    import threading
    from presidio.ner.triton_recognizer import NERInferenceCancelled
    source = 'head\n\nIvan Petrov\n\ntail'
    start = source.index('Ivan')
    cancellation = threading.Event()
    values = BoundaryValues(source)
    original = values.resolve
    def resolve(left, right):
        result = original(left, right)
        cancellation.set()
        return result
    monkeypatch.setattr(values, 'resolve', resolve)
    instance, _, _ = recognizer(monkeypatch, [prediction('PERSON', start, start + 2, left=True)], [])
    with pytest.raises(NERInferenceCancelled):
        instance.analyze(source, boundary_context=BoundaryContext(values), cancellation_event=cancellation)
    assert instance.is_ready()
