"""The colleague's technology acronym is not a general organization exemption."""
import pytest

from entity_types import SUPPORTED_ENTITY_TYPES
from result_merging import NER_BOUNDARY_CONTAINER_KEY, merge_results
from test_public_identifiers import cover, finding

PUBLIC = frozenset({'trackPWA', 'PWA'})
REAL_LINE = ('- Аналитика PWA — `trackPWA`, а не `track` из `@terminal/core`. '
             'Вызывай её в features и widgets, не в entities. '
             'Трекинг устройства — `@fingerprintjs/fingerprintjs`.')


@pytest.mark.parametrize('text', [REAL_LINE, 'Приложение PWA', 'Технология PWA',
    'PWA-приложение', 'PWA приложение', 'PWA app', 'Progressive Web App (PWA)',
    'Аналитика `PWA`', 'Используй PWA application.'])
@pytest.mark.parametrize('fragment', ['PWA', 'PW'])
def test_only_explicit_technology_context_can_remove_whole_or_partial_org(text, fragment):
    old = finding(text, fragment)
    assert old.start in cover(merge_results(text, [old]).results)
    assert not merge_results(text, [old], public_identifiers=PUBLIC).results
    assert old.start in cover(merge_results(text, [old], public_identifiers=frozenset({'trackPWA'})).results)


@pytest.mark.parametrize('text', ['PWA', 'Output exactly this text: PWA',
    'Company PWA application', 'Company:\nPWA application', 'ООО PWA-приложение',
    'Аналитика PWA — это пароль', 'password=PWA application',
    'token="PWA application"', 'Логин: PWA приложение',
    'Название организации PWA приложение', 'PWA application, secret',
    'value="PWA application"', 'value=`PWA application`',
    '{"content":"Аналитика PWA"}', 'Данные: ["PWA application"]',
    'send("PWA application")', 'Аналитика PWA-org', 'Аналитика PWA-app-prod',
    'PWA-приложениеX', 'XPWA приложение', 'PWACompany app',
    'Аналитика pwa', 'Аналитика PWA2', 'Аналитика PWA$foo'])
def test_ambiguous_sensitive_structured_or_larger_name_is_not_exempt(text):
    word = 'pwa' if 'pwa' in text else 'PWA'
    old = finding(text, word)
    baseline = cover(merge_results(text, [old]).results)
    assert cover(merge_results(text, [old], public_identifiers=PUBLIC).results) == baseline


@pytest.mark.parametrize('source,kind', [('structural', k) for k in sorted(SUPPORTED_ENTITY_TYPES)]
    + [('ner', k) for k in sorted(SUPPORTED_ENTITY_TYPES - {'ORGANIZATION'})])
def test_all_independent_types_veto_whole_technology_exception(source, kind):
    text = 'Аналитика PWA'
    old = finding(text, 'PWA')
    other = finding(text, 'PW', kind, source)
    baseline = cover(merge_results(text, [old, other]).results)
    assert cover(merge_results(text, [old, other], public_identifiers=PUBLIC).results) == baseline


def test_real_line_all_three_fragments_are_suppressed_together():
    candidates = [finding(REAL_LINE, 'PWA'), finding(REAL_LINE, 'track'), finding(REAL_LINE, 'WA')]
    # The trackPWA suffix, not the first occurrence in PWA.
    candidates[-1].start = REAL_LINE.index('trackPWA') + 6
    candidates[-1].end = candidates[-1].start + 2
    assert not merge_results(REAL_LINE, candidates, public_identifiers=PUBLIC).results


def test_duplicate_whole_and_partial_ner_org_findings_do_not_veto_technology():
    text = 'Аналитика PWA'
    assert not merge_results(text, [finding(text, 'PWA'), finding(text, 'PWA'), finding(text, 'PW')],
                             public_identifiers=PUBLIC).results


@pytest.mark.parametrize('container', [False, True])
def test_crossing_org_or_boundary_container_preserves_baseline(container):
    text = 'Аналитика PWA'
    old = finding(text, 'PWA')
    other = finding(text, 'PWA' if container else 'Аналитика PWA')
    if container:
        other.recognition_metadata[NER_BOUNDARY_CONTAINER_KEY] = True
    assert cover(merge_results(text, [old, other], public_identifiers=PUBLIC).results) == cover(
        merge_results(text, [old, other]).results)


def test_pwa_configuration_does_not_exempt_a_suffix_of_trackpwa():
    text = 'Аналитика trackPWA'
    old = finding(text, 'PWA')
    assert cover(merge_results(text, [old], public_identifiers=frozenset({'PWA'})).results) == cover(
        merge_results(text, [old]).results)
