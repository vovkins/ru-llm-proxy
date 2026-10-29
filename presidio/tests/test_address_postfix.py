"""A street type after its name still needs a valid house, not a quantity."""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from presidio_analyzer import RecognizerResult
from recognizers import ALL_RECOGNIZERS
from recognizers.ru_address_chain import RuAddressChainRecognizer
from result_merging import DETECTION_SOURCE_METADATA_KEY, SOURCE_NER, merge_results


def direct(text):
    return RuAddressChainRecognizer().analyze(text, ['RU_ADDRESS'])


@pytest.mark.parametrize('value', [
    'Фрунзенская набережная 24', 'Садовая улица 15',
    'Малый Кисловский переулок 3', 'Ленинский проспект 10',
    'Цветной бульвар 7', 'Каширское шоссе 12', 'Садовая ул. 15',
    'Фрунзенская наб. 24', 'Малый Кисловский пер. 3',
    'Фрунзенская набережная, 24', 'Фрунзенская набережная, д. 24',
    'Фрунзенская набережная дом 24', 'Фрунзенская\tнабережная\t24',
    'Фрунзенская набережная 24А', 'Фрунзенская набережная 24/2',
    'Фрунзенская набережная 24-2', 'Фрунзенская набережная 24 к. 2',
    'Фрунзенская набережная 24, корпус 2',
    'Фрунзенская набережная 24, подъезд 2, кв. 12',
])
def test_postfix_type_keeps_the_complete_address_without_a_context_label(value):
    text = 'Жду на ' + value + ' — доставка завтра'
    assert any(text[r.start:r.end] == value and r.score == .75 for r in direct(text))


@pytest.mark.parametrize('text', [
    'Новая набережная 24 метра длиной', 'Тверская улица 15 лет строится',
    'Садовая улица 3 человека', 'Садовая улица 24 руб.',
    'Садовая улица 24%', 'Садовая улица 24 ₽', 'Садовая улица 5 км',
    'Садовая улица 15 минут', 'Садовая улица 2 дома',
    'Садовая улица', 'Садовая улица 12345', 'Садовая улица 24_token',
    'Садовая улица 24.5', 'Садовая улица 24/12345',
    'Садовая улица; участников 15', 'Садовая улица. Участников 15',
    'Садовая улица\n15', 'Садовая улица\r\n15',
    'Садовая улица count=15', 'Садовая улица, count: 15',
    'садовая улица 15',
])
def test_postfix_type_does_not_turn_a_quantity_or_another_field_into_a_house(text):
    assert direct(text) == []


def test_actual_k02_is_an_address_even_when_ner_finds_only_an_organization():
    text = 'Николай Зайцев мой тел 8 915 333 44 55 жду на Фрунзенская набережная 24'
    value = 'Фрунзенская набережная 24'
    start = text.index(value)
    ner = RecognizerResult('ORGANIZATION', start, start + len('Фрунзенская'), .92,
                           recognition_metadata={DETECTION_SOURCE_METADATA_KEY: SOURCE_NER})
    spans = merge_results(text, [ner, *direct(text)], score_threshold=.35).results
    assert all(any(r.entity_type == 'RU_ADDRESS' and r.start <= i < r.end for r in spans)
               for i in range(start, start + len(value)) if not text[i].isspace())


def test_postfix_addresses_remain_separate_and_do_not_include_surrounding_prose():
    first, second = 'Садовая улица 15', 'Фрунзенская набережная 24'
    text = 'Жду на ' + first + '; доставка на ' + second + ' завтра.'
    assert [text[r.start:r.end] for r in direct(text)] == [first, second]


@pytest.mark.parametrize('tail', ['ordinary text', 'подъезд 2'])
def test_postfix_address_has_bounded_work_on_a_long_horizontal_gap(tail):
    value = 'Фрунзенская набережная 24'
    text = value + ' ' * 4000 + tail
    spans = direct(text)
    assert len(spans) == 1
    assert spans[0].start == 0
    assert spans[0].end == (len(text) if tail == 'подъезд 2' else len(value))


@pytest.mark.parametrize('ascii_only', [False, True])
@pytest.mark.parametrize('key', ['address', 'text'])
@pytest.mark.parametrize('value', [
    'Фрунзенская набережная 24',
    'Фрунзенская набережная 24, подъезд 2, кв. 12',
])
def test_json_postfix_address_preserves_offsets_structure_and_restoration(ascii_only, key, value):
    text = json.dumps({'nested': [{key: value, 'next': 'ordinary'}]}, ensure_ascii=ascii_only)
    findings = [r for cls in ALL_RECOGNIZERS for r in cls().analyze(text, ['RU_ADDRESS'], None)]
    spans = merge_results(text, findings, requested_entities=['RU_ADDRESS'], score_threshold=.35).results
    raw = json.dumps(value, ensure_ascii=ascii_only)[1:-1]
    start = text.index(raw)
    assert all(any(r.entity_type == 'RU_ADDRESS' and r.start <= i < r.end for r in spans)
               for i in range(start, start + len(raw)))
    masked, saved = text, {}
    for i, r in reversed(list(enumerate(spans))):
        token = f'<RU_ADDRESS_{i}>'
        saved[token] = text[r.start:r.end]
        masked = masked[:r.start] + token + masked[r.end:]
    assert json.loads(masked)['nested'][0]['next'] == 'ordinary'
    for token, original in saved.items():
        masked = masked.replace(token, original)
    assert masked.encode() == text.encode()


@pytest.mark.parametrize('ascii_only', [False, True])
@pytest.mark.parametrize('obj', [
    {'street': 'Фрунзенская набережная', 'count': 24},
    {'text': 'Фрунзенская набережная', 'count': '24'},
    [{'text': 'Фрунзенская набережная'}, {'text': '24'}],
])
def test_postfix_house_is_not_borrowed_from_another_field(ascii_only, obj):
    text = json.dumps(obj, ensure_ascii=ascii_only)
    assert direct(text) == []


@pytest.mark.parametrize('ascii_only', [False, True])
def test_postfix_entrance_does_not_cross_a_literal_boundary(ascii_only):
    text = json.dumps({'text': 'Фрунзенская набережная 24', 'comment': 'подъезд 2'},
                      ensure_ascii=ascii_only)
    findings = [r for cls in ALL_RECOGNIZERS for r in cls().analyze(text, ['RU_ADDRESS'], None)]
    spans = merge_results(text, findings, requested_entities=['RU_ADDRESS'], score_threshold=.35).results
    raw = json.dumps('подъезд 2', ensure_ascii=ascii_only)[1:-1]
    start = text.index(raw)
    assert spans
    assert not any(r.start < start + len(raw) and start < r.end for r in spans)


def test_sql_postfix_address_keeps_its_own_literal():
    value = 'Фрунзенская набережная 24, подъезд 2'
    text = f"INSERT INTO clients (address, note) VALUES ('{value}', 'подъезд 3');"
    spans = merge_results(text, direct(text), score_threshold=.35).results
    assert [text[r.start:r.end] for r in spans] == [value]


@pytest.mark.parametrize('policy', ['content', 'logistic'])
def test_postfix_address_respects_api_threshold_and_entity_filter(monkeypatch, policy):
    from fastapi.testclient import TestClient
    from presidio import analyzer_server

    monkeypatch.setenv('PRESIDIO_ANALYZER_BASE64_POLICY', policy)
    monkeypatch.setattr(analyzer_server.ner_recognizer, 'require_ready', lambda: None)
    monkeypatch.setattr(analyzer_server.ner_recognizer, 'analyze', lambda *_a, **_k: [])
    api = TestClient(analyzer_server.app)
    text = 'Жду на Фрунзенская набережная 24'
    payload = {'text': text, 'entities': ['RU_ADDRESS'], 'score_threshold': .35}
    response = api.post('/api/v1/analyze', json=payload)
    assert response.status_code == 200
    assert any(r['start'] == 7 and r['end'] == len(text) and r['score'] == .75
               for r in response.json()['entities'])
    for changes in ({'score_threshold': .76}, {'entities': ['PERSON']}):
        response = api.post('/api/v1/analyze', json={**payload, **changes})
        assert response.status_code == 200
        assert response.json()['entities'] == []


BOUNDARY_ADDRESS = 'Новосибирск ул. Красный проспект 44 кв. 12'
BOUNDARY_PREFIX = 'Абонент Максим Лебедев'
BOUNDARY_TEXT = ('Хочу подключить интернет на новом адресе. ' + BOUNDARY_PREFIX
                 + ', ' + BOUNDARY_ADDRESS + ', для связи +7-913-444-55-66')


@pytest.mark.parametrize('prefix', [
    BOUNDARY_PREFIX, 'Клиент Анна Смирнова', 'Петр Сергеев', 'Служебное Примечание',
])
def test_multiple_street_components_do_not_borrow_another_capitalized_prefix(prefix):
    text = prefix + ', ' + BOUNDARY_ADDRESS
    assert [text[r.start:r.end] for r in direct(text)] == [BOUNDARY_ADDRESS]


@pytest.mark.parametrize('address', [
    'Москва, Садовая улица 15',
    'Санкт-Петербург, Малый Кисловский переулок 3',
    'г. Новосибирск, Красный проспект 44, кв. 12',
    BOUNDARY_ADDRESS,
    'Новосибирск ул. Красный проспект 44, подъезд 2, кв. 12',
])
def test_boundary_fix_retains_existing_cities_names_and_entrance(address):
    assert any(address[r.start:r.end] == address for r in direct(address))


def test_actual_boundary_keeps_person_and_address_classes_separate():
    text = BOUNDARY_TEXT
    start = text.index(BOUNDARY_PREFIX)
    ner = RecognizerResult('PERSON', start, start + len(BOUNDARY_PREFIX), .98,
                           recognition_metadata={DETECTION_SOURCE_METADATA_KEY: SOURCE_NER})
    spans = merge_results(text, [ner, *direct(text)], score_threshold=.35).results
    assert [(r.entity_type, text[r.start:r.end]) for r in spans] == [
        ('PERSON', BOUNDARY_PREFIX), ('RU_ADDRESS', BOUNDARY_ADDRESS),
    ]


@pytest.mark.parametrize('entities', [None, ['PERSON'], ['RU_ADDRESS']])
def test_boundary_fix_respects_api_entity_filters(monkeypatch, entities):
    from fastapi.testclient import TestClient
    from presidio import analyzer_server

    text = BOUNDARY_PREFIX + ', ' + BOUNDARY_ADDRESS
    ner = RecognizerResult('PERSON', 0, len(BOUNDARY_PREFIX), .98,
                           recognition_metadata={DETECTION_SOURCE_METADATA_KEY: SOURCE_NER})
    monkeypatch.setattr(analyzer_server.ner_recognizer, 'require_ready', lambda: None)
    monkeypatch.setattr(analyzer_server.ner_recognizer, 'analyze', lambda *_a, **_k: [ner])
    payload = {'text': text, 'score_threshold': .35}
    if entities is not None:
        payload['entities'] = entities
    response = TestClient(analyzer_server.app).post('/api/v1/analyze', json=payload)
    assert response.status_code == 200
    spans = response.json()['entities']
    expected = [('PERSON', BOUNDARY_PREFIX), ('RU_ADDRESS', BOUNDARY_ADDRESS)]
    assert [(r['entity_type'], text[r['start']:r['end']]) for r in spans] == [
        (kind, value) for kind, value in expected if entities is None or kind in entities
    ]


@pytest.mark.parametrize('ascii_only', [False, True])
def test_boundary_fix_preserves_json_offsets_without_absorbing_the_name(ascii_only):
    text = json.dumps({'text': BOUNDARY_PREFIX + ', ' + BOUNDARY_ADDRESS}, ensure_ascii=ascii_only)
    findings = [r for cls in ALL_RECOGNIZERS for r in cls().analyze(text, ['RU_ADDRESS'], None)]
    spans = merge_results(text, findings, requested_entities=['RU_ADDRESS'], score_threshold=.35).results
    raw_name = json.dumps(BOUNDARY_PREFIX, ensure_ascii=ascii_only)[1:-1]
    raw_address = json.dumps(BOUNDARY_ADDRESS, ensure_ascii=ascii_only)[1:-1]
    name_start, address_start = text.index(raw_name), text.index(raw_address)
    assert not any(r.start < name_start + len(raw_name) and name_start < r.end for r in spans)
    assert all(any(r.start <= i < r.end for r in spans)
               for i in range(address_start, address_start + len(raw_address)))


def test_boundary_fix_keeps_sql_literal_and_following_address_separate():
    value = BOUNDARY_PREFIX + ', ' + BOUNDARY_ADDRESS
    text = f"INSERT INTO clients (note) VALUES ('{value}'); Фрунзенская набережная 24"
    spans = merge_results(text, direct(text), score_threshold=.35).results
    assert [text[r.start:r.end] for r in spans] == [BOUNDARY_ADDRESS, 'Фрунзенская набережная 24']
