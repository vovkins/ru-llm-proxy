"""Opt-in technical references never waive independent sensitive findings."""
import json

import pytest
from presidio_analyzer import RecognizerResult

from entity_types import SUPPORTED_ENTITY_TYPES
from ner.public_identifiers import parse_public_identifiers
from result_merging import (
    DETECTION_SOURCE_METADATA_KEY, NER_BOUNDARY_CONTAINER_KEY,
    SOURCE_NER, merge_results,
)

PUBLIC = frozenset({'trackPWA'})


def finding(text, word='track', kind='ORGANIZATION', source=SOURCE_NER):
    at = text.index(word)
    return RecognizerResult(kind, at, at + len(word), .9,
                            recognition_metadata={DETECTION_SOURCE_METADATA_KEY: source})


def cover(results):
    return {i for r in results for i in range(r.start, r.end)}


@pytest.mark.parametrize('text', [
    'trackPWA', 'Output exactly this text and nothing else: trackPWA',
    '- Аналитика PWA — `trackPWA`, вызывай её в widgets.',
    'Используй "trackPWA" для аналитики.',
])
def test_exact_configured_reference_removes_only_partial_ner_org(text):
    old = finding(text)
    assert old.start in cover(merge_results(text, [old]).results)
    outcome = merge_results(text, [old], public_identifiers=PUBLIC)
    assert not outcome.results
    assert 'ner_public_identifier_fragment_suppressed' in {d.reason for d in outcome.decisions}


def test_two_fragments_of_one_reference_are_removed_together():
    text = 'trackPWA'
    assert not merge_results(text, [finding(text), finding(text, 'WA')], public_identifiers=PUBLIC).results


@pytest.mark.parametrize('name,fragment', [
    ('renderPortal','render'), ('handleEvent_42','handle'), ('emitMetrics','emit'),
])
def test_configuration_is_generic_and_not_a_hardcoded_trackpwa_rule(name, fragment):
    text = 'Use ' + name
    old = finding(text, fragment)
    assert not merge_results(text, [old], public_identifiers=frozenset({name})).results


@pytest.mark.parametrize('whole', [False, True])
def test_whole_or_crossing_org_candidate_keeps_original_merge_behavior(whole):
    text = 'Use trackPWA today'
    old = finding(text)
    other = finding(text, 'trackPWA' if whole else 'trackPWA today')
    baseline = merge_results(text, [old, other])
    out = merge_results(text, [old, other], public_identifiers=PUBLIC)
    assert [(r.start,r.end,r.entity_type) for r in out.results] == [
        (r.start,r.end,r.entity_type) for r in baseline.results]
    assert 'ner_public_identifier_fragment_suppressed' not in {d.reason for d in out.decisions}


@pytest.mark.parametrize('kind', sorted(SUPPORTED_ENTITY_TYPES))
def test_every_native_type_vetoes_the_exception(kind):
    text = 'trackPWA'
    old = finding(text)
    independent = finding(text, 'PWA', kind, 'structural')
    out = merge_results(text, [old, independent], public_identifiers=PUBLIC)
    # Existing per-type policy may reject an invalid independent candidate,
    # but the public-name exception still must not remove the old ORG fragment.
    assert set(range(old.start, old.end)) <= cover(out.results)


@pytest.mark.parametrize('kind', sorted(SUPPORTED_ENTITY_TYPES - {'ORGANIZATION'}))
def test_every_other_ner_type_vetoes_the_exception(kind):
    text = 'trackPWA'
    old = finding(text)
    out = merge_results(text, [old, finding(text, 'PWA', kind)], public_identifiers=PUBLIC)
    assert set(range(old.start, old.end)) <= cover(out.results)


@pytest.mark.parametrize('kind', sorted(SUPPORTED_ENTITY_TYPES))
def test_whole_findings_and_other_types_are_not_exempt(kind):
    text = 'trackPWA'
    old = finding(text, 'trackPWA', kind)
    baseline = merge_results(text, [old])
    out = merge_results(text, [old], public_identifiers=PUBLIC)
    assert [(r.start, r.end, r.entity_type) for r in out.results] == [
        (r.start, r.end, r.entity_type) for r in baseline.results]


@pytest.mark.parametrize('text', [
    'password=trackPWA', 'auth_token="trackPWA"', 'Логин пользователя: trackPWA',
    'Пароль — trackPWA', 'trackPWA используется как пароль',
    'Название организации: trackPWA', 'Company called trackPWA', 'ООО trackPWA',
    'value=trackPWA', 'value="trackPWA"', "value='trackPWA'", 'value=`trackPWA`',
    '{"content":"trackPWA"}', '{"company":{"name":"trackPWA"}}',
    'Данные: {"company":{"name":"trackPWA"}}',
    'Данные: ["trackPWA"]', 'Данные: ["ordinary", "trackPWA"]',
    'send("trackPWA")', "send('trackPWA')",
])
def test_sensitive_or_assignment_context_keeps_even_partial_org(text):
    old = finding(text)
    assert set(range(old.start, old.end)) <= cover(
        merge_results(text, [old], public_identifiers=PUBLIC).results)


@pytest.mark.parametrize('text,fragment', [
    ('TrackPWA', 'Track'), ('trackPWAExtra', 'track'), ('pretrackPWA', 'track'),
    ('trackPWA-prod', 'track'), ('ЖtrackPWA', 'track'), ('$trackPWA', 'track'),
    ('trackPWA2', 'track'),
])
def test_identifier_match_is_exact_and_case_sensitive(text, fragment):
    old = finding(text, fragment)
    assert set(range(old.start, old.end)) <= cover(
        merge_results(text, [old], public_identifiers=PUBLIC).results)


def test_boundary_container_is_not_exempt():
    text = 'trackPWA'
    old = finding(text)
    old.recognition_metadata[NER_BOUNDARY_CONTAINER_KEY] = True
    assert set(range(old.start, old.end)) <= cover(
        merge_results(text, [old], public_identifiers=PUBLIC).results)


def test_unknown_source_is_not_exempt():
    text = 'trackPWA'
    old = finding(text, source='unknown')
    assert cover(merge_results(text, [old], public_identifiers=PUBLIC).results) == cover(
        merge_results(text, [old]).results)


def test_scope_is_source_position_not_every_equal_fragment():
    text = 'Use trackPWA.\nНазвание организации: track'
    first = finding(text)
    second = finding(text)
    second.start = text.rindex('track')
    second.end = second.start + len('track')
    out = merge_results(text, [first, second], public_identifiers=PUBLIC)
    assert not set(range(first.start, first.end)) & cover(out.results)
    assert set(range(second.start, second.end)) <= cover(out.results)


@pytest.mark.parametrize('raw', ['{}', 'null', 'true', '"trackPWA"', '[1]', '[null]',
    '["trackPWA "]', '["trackPWA.*"]', '["track-PWA"]', '["трекинг"]',
    '["' + 'a' * 129 + '"]', 'not json', json.dumps(['a'] * 257), ' ' * 65537])
def test_invalid_admin_configuration_is_rejected(raw):
    with pytest.raises(ValueError):
        parse_public_identifiers(raw)


def test_empty_default_and_exact_spelling():
    assert not parse_public_identifiers('[]')
    assert parse_public_identifiers('["trackPWA","TrackPWA","trackPWA"]') == frozenset({'trackPWA','TrackPWA'})


def test_policy_changes_analysis_fingerprint(monkeypatch):
    import analyzer_server
    monkeypatch.setenv('PRESIDIO_ANALYZER_PUBLIC_IDENTIFIERS_JSON', '[]')
    before = analyzer_server._build_analysis_signature()
    monkeypatch.setenv('PRESIDIO_ANALYZER_PUBLIC_IDENTIFIERS_JSON', '["trackPWA"]')
    assert analyzer_server._build_analysis_signature() != before


def test_request_cannot_set_the_server_owned_public_list():
    from analyzer_server import AnalyzeRequest
    request = AnalyzeRequest.model_validate({
        'text': 'trackPWA', 'public_identifiers': ['trackPWA'],
        'PRESIDIO_ANALYZER_PUBLIC_IDENTIFIERS_JSON': '["trackPWA"]',
    })
    assert 'public_identifiers' not in request.model_dump()
    assert 'PRESIDIO_ANALYZER_PUBLIC_IDENTIFIERS_JSON' not in request.model_dump()
