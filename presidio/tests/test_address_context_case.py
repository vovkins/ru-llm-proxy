"""Lowercase street names need an explicit, local address label."""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from recognizers import ALL_RECOGNIZERS
from recognizers.ru_address_chain import RuAddressChainRecognizer
from result_merging import merge_results


def direct(text):
    return RuAddressChainRecognizer().analyze(text, ['RU_ADDRESS'])


def merged(text):
    findings = [r for cls in ALL_RECOGNIZERS for r in cls().analyze(text, ['RU_ADDRESS'], None)]
    return merge_results(text, findings, requested_entities=['RU_ADDRESS'], score_threshold=.35).results


@pytest.mark.parametrize('label', [
    'Адрес:', 'адрес =', 'Адрес доставки:', 'АДРЕС ПРОЖИВАНИЯ:',
    'адрес регистрации =', 'address:', 'addr =',
])
@pytest.mark.parametrize('value', [
    'москва, проспект вернадского 30', 'фрунзенская набережная 24',
])
def test_explicit_label_accepts_lowercase_address_but_not_the_label(label, value):
    text = label + ' ' + value + ', доставка завтра'
    assert any(text[r.start:r.end] == value for r in direct(text))


@pytest.mark.parametrize('value', [
    'малый кисловский переулок 3', 'ул. ленина 5, подъезд 2, кв. 12',
    'ул. ленина 5, корпус 2', 'ул. королёва 5а',
    'проспект вернадского 30/2', 'проспект Вернадского 30',
    'москва, ул. ленина, дом 5',
    'г. москва, проспект вернадского 30',
])
def test_context_reuses_names_numbers_and_supported_address_tails(value):
    text = 'Адрес: ' + value
    assert any(text[r.start:r.end] == value for r in direct(text))


def test_actual_k03_does_not_absorb_the_passport_or_comment():
    value = 'москва, проспект вернадского 30'
    text = 'кандидат оставил адрес: ' + value + ', но паспорт 5000 917038 из другого региона'
    assert [text[r.start:r.end] for r in direct(text)] == [value]


@pytest.mark.parametrize('text', [
    'проспект вернадского 30', 'Доставка: проспект вернадского 30',
    'живу проспект вернадского 30', 'по адресу проспект вернадского 30',
    'адрес электронной почты: проспект вернадского 30',
    'Адрес: раздел 30', 'Адрес: подъезд 2', 'Адрес: москва 30',
    'Адрес: проспект развития проекта на 30 лет',
    'Адрес: улица ленина 5 человек', 'Адрес: улица ленина 3 человека',
    'Адрес: набережная длиной 24 метра', 'Адрес: улица ленина 5 ₽',
    'Адрес: фрунзенская набережная 24 метра',
    'Адрес: проспект вернадского 12345', 'Адрес: проспект вернадского 30.5',
    'Адрес: проспект вернадского 30_token',
    'Адрес:\nпроспект вернадского 30', 'Адрес: ; проспект вернадского 30',
    'Адрес: null, проспект вернадского 30',
    'Адрес: comment=проспект вернадского 30',
    'Адрес: пусто. проспект вернадского 30',
    'ip_address: проспект вернадского 30', 'myaddress: проспект вернадского 30',
    'Адрес: нужно идти проспект вернадского 30',
])
def test_new_context_does_not_create_an_address_from_ordinary_or_unrelated_data(text):
    assert direct(text) == []


@pytest.mark.parametrize('tail', ['; note=ordinary', '\nобычный текст', '. Доставка завтра', ', доставка завтра'])
def test_context_stops_at_the_end_of_the_address(tail):
    value = 'проспект вернадского 30'
    text = 'Адрес: ' + value + tail
    assert [text[r.start:r.end] for r in direct(text)] == [value]


def test_owner_accepted_wider_name_stays_inside_the_explicit_value():
    value = 'пожалуйста доставьте фрунзенская набережная 24'
    text = 'Адрес: ' + value + ', доставка завтра'
    assert [text[r.start:r.end] for r in direct(text)] == [value]
    assert direct(value) == []


@pytest.mark.parametrize('ascii_only', [False, True])
@pytest.mark.parametrize('key', ['text', 'address'])
def test_json_context_keeps_original_escapes_and_round_trips(ascii_only, key):
    value = 'москва, проспект вернадского 30, подъезд 2'
    source = 'Адрес: ' + value if key == 'text' else value
    text = json.dumps({'nested': [{key: source, 'next': 'ordinary'}]}, ensure_ascii=ascii_only)
    spans = merged(text)
    raw = json.dumps(value, ensure_ascii=ascii_only)[1:-1]
    start = text.index(raw)
    assert all(any(r.start <= i < r.end for r in spans) for i in range(start, start + len(raw)))
    masked, saved = text, {}
    for i, r in reversed(list(enumerate(spans))):
        token = f'<RU_ADDRESS_{i}>'
        saved[token] = text[r.start:r.end]
        masked = masked[:r.start] + token + masked[r.end:]
    assert json.loads(masked)['nested'][0]['next'] == 'ordinary'
    for token, original in saved.items():
        masked = masked.replace(token, original)
    assert masked.encode() == text.encode()


@pytest.mark.parametrize('obj', [
    {'address': None, 'comment': 'проспект вернадского 30'},
    {'label': 'адрес', 'value': 'проспект вернадского 30'},
    {'label': 'Адрес:', 'value': 'проспект вернадского 30'},
    [{'text': 'Адрес:'}, {'text': 'проспект вернадского 30'}],
])
@pytest.mark.parametrize('ascii_only', [False, True])
def test_context_does_not_cross_json_values(obj, ascii_only):
    assert merged(json.dumps(obj, ensure_ascii=ascii_only)) == ()


def test_context_keeps_name_and_two_addresses_separate():
    first, second = 'москва, проспект вернадского 30', 'фрунзенская набережная 24'
    text = 'Клиент Максим Лебедев; адрес: ' + first + '; адрес доставки: ' + second
    assert [text[r.start:r.end] for r in direct(text)] == [first, second]


def test_context_does_not_cross_sql_literals():
    value = 'проспект вернадского 30'
    text = f"INSERT INTO clients (note, other) VALUES ('Адрес: {value}', 'проспект вернадского 31');"
    assert [text[r.start:r.end] for r in direct(text)] == [value]
    assert direct("INSERT INTO clients (label, note) VALUES ('Адрес:', 'проспект вернадского 30');") == []


@pytest.mark.parametrize('prefix', ['ßİ 😀 ', 'x' * 65536 + ' '])
def test_context_preserves_coordinates_after_unicode_and_long_input(prefix):
    value = 'ул. королёва 5'
    text = prefix + 'Адрес: ' + value + ', ßİ stays unchanged'
    start = text.index(value)
    assert any(r.start == start and r.end == start + len(value) for r in direct(text))


def test_context_does_not_accept_a_partial_number_at_its_scan_limit():
    value = 'ул. ' + 'а' * 192 + ' 12345'
    assert direct('Адрес: ' + value) == []


def test_long_gaps_and_repeated_inert_labels_keep_context_local():
    value = 'проспект вернадского 30'
    text = ('Адрес: null; ' * 4000) + 'Адрес:' + ' ' * 4000 + value
    spans = direct(text)
    assert len(spans) == 1
    assert text[spans[0].start:spans[0].end] == value


@pytest.mark.parametrize('policy', ['content', 'logistic'])
def test_context_respects_api_threshold_and_filter(monkeypatch, policy):
    from fastapi.testclient import TestClient
    from presidio import analyzer_server

    monkeypatch.setenv('PRESIDIO_ANALYZER_BASE64_POLICY', policy)
    monkeypatch.setattr(analyzer_server.ner_recognizer, 'require_ready', lambda: None)
    monkeypatch.setattr(analyzer_server.ner_recognizer, 'analyze', lambda *_a, **_k: [])
    text = 'Адрес: москва, проспект вернадского 30'
    api = TestClient(analyzer_server.app)
    payload = {'text': text, 'entities': ['RU_ADDRESS'], 'score_threshold': .35}
    response = api.post('/api/v1/analyze', json=payload)
    assert response.status_code == 200
    assert any(r['start'] == 7 and r['end'] == len(text) and r['score'] == .6
               for r in response.json()['entities'])
    for changes in ({'score_threshold': .61}, {'entities': ['PERSON']}):
        response = api.post('/api/v1/analyze', json={**payload, **changes})
        assert response.status_code == 200
        assert response.json()['entities'] == []
