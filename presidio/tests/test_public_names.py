"""Explicit disclosure of names preserves credentials and independent findings."""
import json

import pytest

from entity_types import SUPPORTED_ENTITY_TYPES
from ner.public_names import parse_public_names
from result_merging import NER_BOUNDARY_CONTAINER_KEY, merge_results
from test_public_identifiers import cover, finding

PUBLIC = frozenset({'goinvest', 'GoInvest', 'goinvest-продукт',
                    'GoInvestUI', 'GoInvestUITests', 'GoInvestUITestPlans'})


@pytest.mark.parametrize('text,word', [
    ('goinvest', 'goinvest'), ('Output exactly this text and nothing else: goinvest', 'goinvest'),
    ('goinvest-продукт', 'goinvest-продукт'), ('Компания GoInvest', 'GoInvest'),
    ('код приложения и UI-тестов (GoInvest)', 'GoInvest'),
    ('code/Projects/GoInvest/GoInvestUITests/Screens/', 'GoInvestUITests'),
    ('GoInvestUITestPlans', 'GoInvestUITestPlans'), ('// GoInvestUI', 'GoInvestUI'),
    ('{"project":"GoInvest"}', 'GoInvest'), ('const app = "goinvest";', 'goinvest'),
])
@pytest.mark.parametrize('kind', ['LOGIN', 'ORGANIZATION'])
@pytest.mark.parametrize('partial', [False, True])
def test_exact_approved_name_can_remove_whole_or_partial_ner_finding(text, word, kind, partial):
    old = finding(text, word[:4] if partial else word, kind)
    out = merge_results(text, [old], public_names=PUBLIC)
    assert not out.results
    assert 'ner_public_name_suppressed' in {d.reason for d in out.decisions}
    # Empty configuration preserves the original result, including data literals.
    assert cover(merge_results(text, [old], public_names=frozenset()).results) == cover(
        merge_results(text, [old]).results)


@pytest.mark.parametrize('text', [
    'password=goinvest', 'DB_PASS=GoInvest', 'AUTH_TOKEN="goinvest"',
    'API_KEY=GoInvest', 'apiKey="goinvest"', 'APIKey=GoInvest',
    'APIKEY=GoInvest', 'clientSecret=GoInvest', '{"accessToken":"GoInvest"}',
    'Пароль: goinvest', 'Логин: goinvest', 'Bearer GoInvest',
    'Authorization:\nGoInvest', '{"password":\n  "GoInvest"}',
    'goinvest — это секрет', 'confidential: GoInvest', 'ключ доступа: GoInvest',
    'user@goinvest.example', 'goinvest@example.org',
])
@pytest.mark.parametrize('kind', ['LOGIN', 'ORGANIZATION'])
def test_sensitive_context_or_email_component_is_not_exempt(text, kind):
    word = 'goinvest' if 'goinvest' in text else 'GoInvest'
    old = finding(text, word, kind)
    assert cover(merge_results(text, [old], public_names=PUBLIC).results) == cover(
        merge_results(text, [old]).results)


@pytest.mark.parametrize('source,kind', [('structural', k) for k in sorted(SUPPORTED_ENTITY_TYPES)]
    + [('ner', k) for k in sorted(SUPPORTED_ENTITY_TYPES - {'ORGANIZATION', 'LOGIN'})])
def test_independent_finding_of_every_type_vetoes_public_name(source, kind):
    text = 'goinvest'
    old = finding(text, 'goinvest', 'LOGIN')
    other = finding(text, 'goin', kind, source)
    assert cover(merge_results(text, [old, other], public_names=PUBLIC).results) == cover(
        merge_results(text, [old, other]).results)


def test_whole_email_is_preserved():
    text = 'goinvest@example.org'
    old = finding(text, 'goinvest', 'LOGIN')
    email = finding(text, text, 'EMAIL_ADDRESS', 'structural')
    assert set(range(len(text))) <= cover(merge_results(text, [old, email], public_names=PUBLIC).results)


@pytest.mark.parametrize('text', ['gointra', 'GOINVEST', 'Xgoinvest', 'goinvestExtra',
    'goinvest2', 'goinvest-prod', '$goinvest', 'Жgoinvest', 'GoInvestUITestsExtra'])
def test_no_implicit_alias_prefix_suffix_case_or_other_name(text):
    old = finding(text, text, 'LOGIN')
    assert cover(merge_results(text, [old], public_names=PUBLIC).results) == cover(
        merge_results(text, [old]).results)


@pytest.mark.parametrize('container', [False, True])
def test_boundary_container_and_crossing_finding_keep_original_coverage(container):
    text = 'Use goinvest today'
    old = finding(text, 'goinvest', 'LOGIN')
    other = finding(text, 'goinvest' if container else 'goinvest today')
    if container:
        other.recognition_metadata[NER_BOUNDARY_CONTAINER_KEY] = True
    assert cover(merge_results(text, [old, other], public_names=PUBLIC).results) == cover(
        merge_results(text, [old, other]).results)


def test_duplicate_login_and_org_on_the_same_public_name_can_both_disappear():
    text = 'goinvest'
    assert not merge_results(text, [finding(text, text, 'LOGIN'), finding(text, 'goin')],
                             public_names=PUBLIC).results


@pytest.mark.parametrize('raw', ['{}', 'null', '"goinvest"', '[true]', '["*"]',
    '["goinvest@example.org"]', '["a/b"]', '["GoInvest "]', '[""]',
    json.dumps(['a'] * 257), json.dumps(['a' * 129]), ' ' * 65537])
def test_invalid_server_configuration_is_rejected(raw):
    with pytest.raises(ValueError):
        parse_public_names(raw)


def test_configuration_is_exact_generic_and_defaults_empty():
    assert parse_public_names('[]') == frozenset()
    assert parse_public_names('["goinvest","GoInvest","goinvest-продукт"]') == frozenset(
        {'goinvest', 'GoInvest', 'goinvest-продукт'})
    text = 'PublicProduct42'
    assert not merge_results(text, [finding(text, text, 'LOGIN')],
                             public_names=frozenset({text})).results


def test_configuration_changes_fingerprint(monkeypatch):
    import analyzer_server
    monkeypatch.setenv('PRESIDIO_ANALYZER_PUBLIC_NAMES_JSON', '[]')
    original = analyzer_server._build_analysis_signature()
    monkeypatch.setenv('PRESIDIO_ANALYZER_PUBLIC_NAMES_JSON', '["goinvest"]')
    assert analyzer_server._build_analysis_signature() != original


def test_request_cannot_approve_public_names():
    from analyzer_server import AnalyzeRequest
    request = AnalyzeRequest.model_validate({'text': 'goinvest', 'public_names': ['goinvest'],
        'PRESIDIO_ANALYZER_PUBLIC_NAMES_JSON': '["goinvest"]'})
    assert 'public_names' not in request.model_dump()
    assert 'PRESIDIO_ANALYZER_PUBLIC_NAMES_JSON' not in request.model_dump()


@pytest.mark.parametrize('value', ['GoInvest/', '/GoInvest', '\\GoInvest\\', '\"GoInvest\"'])
def test_only_adjacent_path_and_quote_delimiters_may_be_suppressed(value):
    text = 'Use ' + value
    old = finding(text, value, 'ORGANIZATION')
    assert not merge_results(text, [old], public_names=PUBLIC).results


@pytest.mark.parametrize('value', ['GoInvest today', 'GoInvest/private', 'GoInvest-secret', 'GoInvest' + '/' * 9])
def test_wider_values_still_veto_the_exception(value):
    text = 'Use ' + value
    old = finding(text, value, 'LOGIN')
    assert cover(merge_results(text, [old], public_names=PUBLIC).results) == cover(
        merge_results(text, [old]).results)


@pytest.mark.parametrize('prefix,exempt', [('Компания ', True), ('Компания: ', False)])
def test_existing_org_prefix_normalization_is_used_without_extending_it(prefix, exempt):
    text = 'Output exactly this text and nothing else: ' + prefix + 'GoInvest'
    old = finding(text, prefix + 'GoInvest')
    baseline = cover(merge_results(text, [old]).results)
    assert baseline
    outcome = merge_results(text, [old], public_names=PUBLIC).results
    if exempt:
        assert not outcome
    else:
        assert cover(outcome) == baseline
