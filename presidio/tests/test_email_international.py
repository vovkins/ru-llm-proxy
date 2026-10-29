"""Complete Russian/ASCII email values, without requiring an email label."""
import itertools
import json
import sys
from pathlib import Path

import pytest
from presidio_analyzer import RecognizerResult

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from recognizers import ALL_RECOGNIZERS
from recognizers.ru_email import RuEmailRecognizer
from result_merging import (
    DETECTION_SOURCE_METADATA_KEY, SOURCE_NER, SOURCE_STRUCTURAL, merge_results,
)


EMAILS = [
    'marina.kuznetsova@inbox.ru', 'USER_42+work@example.test',
    'first-last%dept@example.test', 'a@b.ru',
    'марина.кузнецова@inbox.ru', 'МАРИНА.КУЗНЕЦОВА@INBOX.RU',
    'марина.test@inbox.ru', 'marina.кузнецова+work@inbox.ru',
    'фёдор@пример.рф', 'ФЁДОР@ПРИМЕР.РФ', 'user@пример.рф',
    'марина@sub.пример.рф', 'користувач@приклад.укр',
    'user@xn--e1afmkfd.xn--p1ai', 'марина@xn--e1afmkfd.xn--p1ai',
    'USER@XN--E1AFMKFD.XN--P1AI', 'user@xn--e1afmkfd.ru',
    'user@пример.xn--p1ai',
]


def direct(text):
    return RuEmailRecognizer().analyze(text, ['EMAIL_ADDRESS'])


def rules(text):
    findings = [r for cls in ALL_RECOGNIZERS
                for r in cls().analyze(text, ['EMAIL_ADDRESS'], None)]
    return merge_results(text, findings, requested_entities=['EMAIL_ADDRESS'], score_threshold=.35).results


@pytest.mark.parametrize('value', EMAILS)
@pytest.mark.parametrize('prefix', ['', 'Напишите на почту: '])
def test_complete_email_with_and_without_context(value, prefix):
    text = prefix + value
    found = direct(text)
    assert [(r.start, r.end, r.entity_type, r.score) for r in found] == [
        (len(prefix), len(text), 'EMAIL_ADDRESS', .85),
    ]


def test_actual_k04_is_a_complete_email_not_a_login():
    value = 'марина.кузнецова@inbox.ru'
    text = 'блин не работает ничего!!! верните доступ к аккаунту ' + value + ' срочно нужно'
    assert [(r.start, r.end, r.entity_type) for r in direct(text)] == [
        (text.index(value), text.index(value) + len(value), 'EMAIL_ADDRESS'),
    ]


@pytest.mark.parametrize(('prefix', 'suffix'), [
    ('<', '>'), ('(', ')'), ('"', '"'), ("'", "'"),
    ('', ', пожалуйста'), ('', '. Дальше обычный текст'), ('', '!'),
])
def test_surrounding_delimiters_do_not_belong_to_the_email(prefix, suffix):
    value = 'марина.test@пример.рф'
    text = prefix + value + suffix
    assert [text[r.start:r.end] for r in direct(text)] == [value]


@pytest.mark.parametrize('text', [
    'matrix @ vector', 'user@@example.test', '@username', 'email:',
    'user@ example.test', 'user @example.test', 'user@\nexample.test',
    'марина.кузнецова@', 'марина@пример', 'user [at] example [dot] test',
    '{"first":"марина@","second":"пример.рф"}',
    '{"first":"марина","second":"@пример.рф"}',
])
def test_no_email_without_a_complete_local_value(text):
    assert direct(text) == []


def test_multiple_values_do_not_absorb_the_person_or_each_other():
    first, second = 'марина.test@inbox.ru', 'фёдор@пример.рф'
    text = 'Клиент Михаил Соколов <' + first + '>; другой: ' + second + '.'
    assert [text[r.start:r.end] for r in direct(text)] == [first, second]


@pytest.mark.parametrize('ascii_only', [False, True])
@pytest.mark.parametrize('value', [
    'марина.кузнецова@inbox.ru', 'фёдор@пример.рф',
    'марина@xn--e1afmkfd.xn--p1ai',
])
def test_json_offsets_and_exact_round_trip(ascii_only, value):
    text = json.dumps({'nested': [{'text': 'Почта: <' + value + '>', 'next': 'ordinary'}]}, ensure_ascii=ascii_only)
    raw = json.dumps(value, ensure_ascii=ascii_only)[1:-1]
    start = text.index(raw)
    spans = rules(text)
    assert [(r.start, r.end) for r in spans] == [(start, start + len(raw))]
    masked = text[:start] + '<EMAIL_ADDRESS_1>' + text[start + len(raw):]
    assert json.loads(masked)['nested'][0]['next'] == 'ordinary'
    assert masked.replace('<EMAIL_ADDRESS_1>', raw).encode() == text.encode()


@pytest.mark.parametrize('ascii_only', [False, True])
def test_json_does_not_join_fragments_from_other_values(ascii_only):
    text = json.dumps({'email': 'марина@', 'next': 'пример.рф'}, ensure_ascii=ascii_only)
    assert rules(text) == ()


def test_quoted_sql_value_keeps_its_boundaries():
    value = 'фёдор@пример.рф'
    text = "INSERT INTO clients (email, note) VALUES ('" + value + "', 'ordinary');"
    assert [text[r.start:r.end] for r in direct(text)] == [value]


@pytest.mark.parametrize('prefix', ['ßİ 😀 ', 'ordinary ' * 16000], ids=['unicode', 'long'])
def test_original_coordinates_after_unicode_or_long_prefix(prefix):
    value = 'фёдор@пример.рф'
    text = prefix + value + ' remains unchanged'
    assert [(r.start, r.end) for r in direct(text)] == [(len(prefix), len(prefix) + len(value))]


def test_k05_preserves_the_person_before_a_structural_international_email():
    value = 'марина.test@inbox.ru'
    text = 'Михаил Соколов ' + value
    start = text.index(value)
    ner = RecognizerResult('PERSON', 0, start + len('марина'), .6,
                           recognition_metadata={DETECTION_SOURCE_METADATA_KEY: SOURCE_NER})
    email = direct(text)[0]
    email.recognition_metadata[DETECTION_SOURCE_METADATA_KEY] = SOURCE_STRUCTURAL
    for findings in itertools.permutations([ner, email]):
        result = merge_results(text, findings, score_threshold=.35).results
        assert any(r.entity_type == 'EMAIL_ADDRESS' and r.start == start and r.end == len(text) for r in result)
        assert all(any(r.start <= i < r.end for r in result) for i in range(start) if not text[i].isspace())


@pytest.mark.parametrize('policy', ['content', 'logistic'])
def test_api_class_filter_and_threshold_are_preserved(monkeypatch, policy):
    from fastapi.testclient import TestClient
    from presidio import analyzer_server

    monkeypatch.setenv('PRESIDIO_ANALYZER_BASE64_POLICY', policy)
    monkeypatch.setattr(analyzer_server.ner_recognizer, 'require_ready', lambda: None)
    monkeypatch.setattr(analyzer_server.ner_recognizer, 'analyze', lambda *_a, **_k: [])
    text = 'марина.кузнецова@inbox.ru'
    api = TestClient(analyzer_server.app)
    payload = {'text': text, 'entities': ['EMAIL_ADDRESS'], 'score_threshold': .35}
    response = api.post('/api/v1/analyze', json=payload)
    assert response.status_code == 200
    assert any(r['start'] == 0 and r['end'] == len(text) and r['score'] == .85
               for r in response.json()['entities'])
    for changes in ({'score_threshold': .86}, {'entities': ['RU_SNILS']}):
        response = api.post('/api/v1/analyze', json={**payload, **changes})
        assert response.status_code == 200
        assert response.json()['entities'] == []
