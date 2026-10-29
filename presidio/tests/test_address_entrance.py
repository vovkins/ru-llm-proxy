"""Entrance tails extend existing addresses, never establish a new core."""
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


@pytest.mark.parametrize('text,value,score', [
    ('Дмитрий Соколов, адрес Московская 156 подъезд 2', 'Московская 156 подъезд 2', .6),
    ('Адрес: Московская 156, подъезд № 2', 'Московская 156, подъезд № 2', .6),
    ('Адрес: Московская 156 подъезд 3, кв. 12', 'Московская 156 подъезд 3, кв. 12', .6),
    ('ул. Ленина, д. 5, подъезд 3', 'ул. Ленина, д. 5, подъезд 3', .75),
    ('ул. Ленина, д. 5, ПОДЪЕЗД№3, кв. 12', 'ул. Ленина, д. 5, ПОДЪЕЗД№3, кв. 12', .75),
    ('ул. Ленина, д. 5\tподъезд\t12', 'ул. Ленина, д. 5\tподъезд\t12', .75),
    ('ул. Ленина, д. 5 подъезд 999', 'ул. Ленина, д. 5 подъезд 999', .75),
])
def test_entrance_completes_only_a_confirmed_address(text, value, score):
    start = text.index(value)
    assert any(r.start == start and r.end == start + len(value)
               and r.entity_type == 'RU_ADDRESS' and r.score == score for r in direct(text))


@pytest.mark.parametrize('tail', [
    ' подъезд', ' подъезд №', ' подъезд 1234', ' подъезд 12345',
    ' подъезд 2/3', ' подъезд 2-3', ' подъезд 2.5', ' подъезд 2а', ' подъезд 2_token',
    ' подъезд2', ' под. 2', ' у подъезда 2', '\nподъезд 2', '\r\nподъезд 2',
    '; подъезд 2', '. Подъезд 2', ' note=подъезд 2', ', comment: подъезд 2',
])
def test_entrance_tail_does_not_cross_a_boundary_or_accept_a_partial_number(tail):
    core = 'Адрес: Московская 156'
    text = core + tail
    spans = direct(text)
    assert spans
    assert all(r.end <= len(core) for r in spans)


@pytest.mark.parametrize('text', [
    'Подъезд 2 закрыт для ремонта', 'подъезд 2, кв. 12',
    'ул. Ленина подъезд 2', 'Адрес: подъезд 2',
    'подъезд 2, ул. Ленина',
])
def test_entrance_does_not_supply_missing_address_evidence(text):
    assert direct(text) == []


def test_existing_capitalized_informal_label_limitation_is_unchanged():
    # Owner explicitly retained this pre-K01 false positive, not a new tail core.
    text = 'Адрес: Подъезд 2'
    assert [text[r.start:r.end] for r in direct(text)] == ['Подъезд 2']


@pytest.mark.parametrize('text,value', [
    ('Адрес: Подъездная 2', 'Подъездная 2'),
    ('ул. Подъезд, д. 2', 'ул. Подъезд, д. 2'),
])
def test_existing_street_names_are_not_filtered(text, value):
    assert any(text[r.start:r.end] == value for r in direct(text))


def test_entrance_stops_before_ordinary_text_and_keeps_each_address_separate():
    first = 'ул. Ленина, д. 5 подъезд 2'
    second = 'ул. Мира, д. 7, подъезд №3, кв. 12'
    text = first + ' закрыт для ремонта; ' + second + ' — доставка завтра'
    assert [text[r.start:r.end] for r in direct(text)] == [first, second]


def test_entrance_before_a_core_is_not_a_new_prefix_component():
    core = 'ул. Ленина, д. 5'
    text = 'подъезд 2, ' + core
    assert [text[r.start:r.end] for r in direct(text)] == [core]


@pytest.mark.parametrize('tail', ['ordinary text', 'подъезд 2'])
def test_long_horizontal_gap_keeps_tail_parsing_bounded(tail):
    core = 'Адрес: Московская 156'
    text = core + ' ' * 4000 + tail
    spans = direct(text)
    assert len(spans) == 1
    assert spans[0].end == (len(text) if tail == 'подъезд 2' else len(core))


@pytest.mark.parametrize('ascii_only', [False, True])
@pytest.mark.parametrize('value', [
    'Московская 156 подъезд 2',
    'ул. Ленина, д. 5, подъезд № 3, кв. 12',
])
@pytest.mark.parametrize('key', ['address', 'text'])
def test_json_entrance_preserves_source_offsets_structure_and_restoration(ascii_only, value, key):
    source = value if key == 'address' else 'Адрес: ' + value
    text = json.dumps({'nested': [{key: source, 'next': 'ordinary'}]}, ensure_ascii=ascii_only)
    findings = [r for cls in ALL_RECOGNIZERS for r in cls().analyze(text, ['RU_ADDRESS'], None)]
    spans = merge_results(text, findings, requested_entities=['RU_ADDRESS'], score_threshold=.35).results
    raw = json.dumps(value, ensure_ascii=ascii_only)[1:-1]
    start = text.index(raw)
    assert all(any(r.start <= i < r.end for r in spans) for i in range(start, start + len(raw)))
    masked = text
    saved = {}
    for i, r in reversed(list(enumerate(spans))):
        token = f'<RU_ADDRESS_{i}>'
        saved[token] = text[r.start:r.end]
        masked = masked[:r.start] + token + masked[r.end:]
    assert json.loads(masked)['nested'][0]['next'] == 'ordinary'
    for token, value in saved.items():
        masked = masked.replace(token, value)
    assert masked.encode() == text.encode()


@pytest.mark.parametrize('ascii_only', [False, True])
@pytest.mark.parametrize('obj', [
    {'address': 'ул. Ленина, д. 5', 'comment': 'подъезд 2 закрыт'},
    {'address': 'Московская 156', 'nested': {'comment': 'подъезд 2'}},
    [{'address': 'Московская 156'}, {'note': 'подъезд 2'}],
    {'address': 'ул. Ленина, д. 5', 'entrance': '2'},
])
def test_entrance_is_not_borrowed_from_a_sibling_field(ascii_only, obj):
    text = json.dumps(obj, ensure_ascii=ascii_only)
    findings = [r for cls in ALL_RECOGNIZERS for r in cls().analyze(text, ['RU_ADDRESS'], None)]
    spans = merge_results(text, findings, requested_entities=['RU_ADDRESS'], score_threshold=.35).results
    value = json.dumps('подъезд 2' if 'подъезд' in str(obj) else '2', ensure_ascii=ascii_only)[1:-1]
    start = text.rindex(value)
    assert not any(r.start < start + len(value) and start < r.end for r in spans)


def test_sql_address_tail_remains_inside_its_own_literal():
    value = 'Московская 156 подъезд 2'
    text = f"INSERT INTO clients (address, note) VALUES ('{value}', 'подъезд 3');"
    spans = direct(text)
    assert [text[r.start:r.end] for r in spans] == [value]


@pytest.mark.parametrize('policy', ['content', 'logistic'])
def test_address_entrance_respects_api_threshold_and_entity_filter(monkeypatch, policy):
    from fastapi.testclient import TestClient
    from presidio import analyzer_server

    monkeypatch.setenv('PRESIDIO_ANALYZER_BASE64_POLICY', policy)
    monkeypatch.setattr(analyzer_server.ner_recognizer, 'require_ready', lambda: None)
    monkeypatch.setattr(analyzer_server.ner_recognizer, 'analyze', lambda *_a, **_k: [])
    api = TestClient(analyzer_server.app)
    text = 'Адрес: Московская 156 подъезд 2'
    payload = {'text': text, 'entities': ['RU_ADDRESS'], 'score_threshold': .35}
    response = api.post('/api/v1/analyze', json=payload)
    assert response.status_code == 200
    assert any(r['start'] == 7 and r['end'] == len(text) and r['score'] == .6
               for r in response.json()['entities'])
    for changes in ({'score_threshold': .61}, {'entities': ['PERSON']}):
        response = api.post('/api/v1/analyze', json={**payload, **changes})
        assert response.status_code == 200
        assert response.json()['entities'] == []
