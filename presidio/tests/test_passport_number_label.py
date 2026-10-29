"""A six-digit passport fragment needs its own explicit label, not nearby prose."""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from recognizers import ALL_RECOGNIZERS
from recognizers.ru_passport_ext import RuPassportExtRecognizer
from result_merging import merge_results


NARRATIVE = 'потерял паспорт, срочно нужна замена! Серию не помню, но номер был 122648'


def direct(text):
    return RuPassportExtRecognizer().analyze(text, ['RU_PASSPORT'])


def short(text):
    return [r for r in direct(text) if (r.recognition_metadata or {}).get('pattern', '').startswith('short_')]


def rules(text):
    findings = [r for cls in ALL_RECOGNIZERS for r in cls().analyze(text, ['RU_PASSPORT'], None)]
    return merge_results(text, findings, requested_entities=['RU_PASSPORT'], score_threshold=.35).results


@pytest.mark.parametrize('label', [
    'Номер паспорта:', 'номер паспорта =', 'номер паспорта №', 'Номер паспорта',
    'Паспортный номер:', 'ПАСПОРТНЫЙ НОМЕР =', 'Паспорт №', 'Паспорт:', 'паспорт =',
])
@pytest.mark.parametrize('value', ['122648', '001234'])
def test_explicit_label_protects_only_the_six_digits(label, value):
    text = label + ' ' + value + ', заявка: 654321'
    start = text.index(value)
    assert [(r.start, r.end, r.score, r.entity_type) for r in short(text)] == [
        (start, start + 6, .75, 'RU_PASSPORT'),
    ]


@pytest.mark.parametrize(('opening', 'closing'), [('"', '"'), ("'", "'"), ('', '')])
def test_direct_value_quotes_are_not_masked(opening, closing):
    value = '001234'
    text = 'Паспорт: ' + opening + value + closing + ', заявка: 654321'
    assert [text[r.start:r.end] for r in short(text)] == [value]


@pytest.mark.parametrize('text', [
    NARRATIVE, 'Заявка № 122648', 'Код подтверждения: 122648',
    'Паспорт потерян. Номер обращения: 122648', 'Паспорт 122648',
    'В паспорте код подразделения 770-001', 'Документ: 122648',
    'Серия: 122648', 'Выдан: 122648', 'Загранпаспорт: 122648',
    'Номер паспорта:\n122648', 'Номер паспорта: ; 122648',
    'Паспорт: заявка 122648', 'Номер паспорта: номер заявки 122648',
    'Номер паспорта123456', 'Мойпаспорт: 122648',
    'Номер паспорта: 1226487', 'Номер паспорта: 122648ABC',
    'Номер паспорта: 122648_ABC', 'Номер паспорта: 122648.0',
    'Номер паспорта: -122648', 'Номер паспорта: +122648',
    'Номер паспорта: 122648e0', 'Номер паспорта: 122 648',
    'Номер паспорта: 122648/7', 'Номер паспорта: 122648-7',
    '{"label":"Номер паспорта:","value":"122648"}',
    '{"passport":null,"ticket":"122648"}',
])
def test_fragment_does_not_borrow_context_or_a_number_prefix(text):
    assert short(text) == []


@pytest.mark.parametrize('ascii_only', [False, True])
@pytest.mark.parametrize('key', ['паспорт', 'номер паспорта', 'passport', 'passport_number'])
@pytest.mark.parametrize('value', ['001234', 122648])
def test_key_confirms_only_its_own_string_or_integer_scalar(ascii_only, key, value):
    text = json.dumps({'nested': [{key: value, 'ticket': '654321'}]}, ensure_ascii=ascii_only)
    number = str(value)
    spans = short(text)
    assert [text[r.start:r.end] for r in spans] == [number]
    assert all(r.score == .75 for r in spans)


@pytest.mark.parametrize('value', [None, True, -122648, 122648.0, '122648ABC', '122 648', ['122648'], {'value': '122648'}])
def test_key_never_confirms_another_scalar(value):
    text = json.dumps({'passport_number': value, 'ticket': '122648'}, ensure_ascii=True)
    assert short(text) == []


@pytest.mark.parametrize('text', [
    '{"passport_number":1.22648e5,"ticket":122648}',
    '{"passport_number":1226487,"ticket":122648}',
    '{"my_passport":"122648"}',
    '[{"passport":null},{"ticket":"122648"}]',
    '{"passport":{"ticket":"122648"},"other":{"ticket":"122648"}}',
])
def test_numeric_tokens_and_container_boundaries_are_complete(text):
    assert short(text) == []


def test_repeated_keys_stay_with_their_own_values():
    text = '[{"passport_number":"122648","ticket":"654321"},{"passport_number":"001234","ticket":"987654"}]'
    assert [text[r.start:r.end] for r in short(text)] == ['122648', '001234']


@pytest.mark.parametrize('ascii_only', [False, True])
def test_decoded_json_value_reuses_the_text_rule(ascii_only):
    text = json.dumps({'text': 'Номер паспорта: 001234', 'next': '654321'}, ensure_ascii=ascii_only)
    assert [text[r.start:r.end] for r in rules(text)] == ['001234']


def test_escaped_digits_keep_source_coordinates_and_exact_round_trip():
    raw = ''.join('\\u%04x' % ord(c) for c in '001234')
    text = '{"passport_number":"' + raw + '","ticket":"654321"}'
    start = text.index(raw)
    found = rules(text)
    assert [(r.start, r.end) for r in found] == [(start, start + len(raw))]
    masked = text[:start] + '<RU_PASSPORT_1>' + text[start + len(raw):]
    assert json.loads(masked)['ticket'] == '654321'
    assert masked.replace('<RU_PASSPORT_1>', raw).encode() == text.encode()


def test_sql_literal_keeps_the_other_value_unrelated():
    text = "INSERT INTO clients (note, ticket) VALUES ('Номер паспорта: 001234', '654321');"
    assert [text[r.start:r.end] for r in short(text)] == ['001234']
    assert short("INSERT INTO clients (label, value) VALUES ('Номер паспорта:', '122648');") == []


@pytest.mark.parametrize('prefix', ['ßİ 😀 ', 'ordinary ' * 16000], ids=['unicode', 'long'])
def test_coordinates_remain_in_the_original_text(prefix):
    text = prefix + 'Номер паспорта: 001234'
    assert [(r.start, r.end) for r in short(text)] == [(len(text) - 6, len(text))]


@pytest.mark.parametrize('value', ['4510123456', '4510-123456', '45 10 123 456', '75-1234567'])
def test_existing_full_passports_are_not_split_into_a_fragment(value):
    text = 'Паспорт: ' + value
    assert any(r.start == text.index(value) and r.end == len(text) for r in direct(text))
    assert short(text) == []


@pytest.mark.parametrize('policy', ['content', 'logistic'])
def test_api_threshold_and_class_filter_are_preserved(monkeypatch, policy):
    from fastapi.testclient import TestClient
    from presidio import analyzer_server

    monkeypatch.setenv('PRESIDIO_ANALYZER_BASE64_POLICY', policy)
    monkeypatch.setattr(analyzer_server.ner_recognizer, 'require_ready', lambda: None)
    monkeypatch.setattr(analyzer_server.ner_recognizer, 'analyze', lambda *_a, **_k: [])
    text = 'Номер паспорта: 001234'
    api = TestClient(analyzer_server.app)
    payload = {'text': text, 'entities': ['RU_PASSPORT'], 'score_threshold': .35}
    response = api.post('/api/v1/analyze', json=payload)
    assert response.status_code == 200
    assert any(r['start'] == len(text) - 6 and r['end'] == len(text) and r['score'] == .75
               for r in response.json()['entities'])
    for changes in ({'score_threshold': .76}, {'entities': ['RU_SNILS']}):
        response = api.post('/api/v1/analyze', json={**payload, **changes})
        assert response.status_code == 200
        assert response.json()['entities'] == []
