"""API-level regression tests for Analyzer threshold-sensitive recognizers."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient
from presidio_analyzer import AnalyzerEngine, RecognizerRegistry

from presidio import analyzer_server
from recognizers.ru_address import RuAddressRecognizer
from recognizers.ru_inn import RuInnRecognizer


def _build_analyzer(*recognizers):
    registry = RecognizerRegistry(supported_languages=["ru"])
    for recognizer in recognizers:
        registry.add_recognizer(recognizer)
    return AnalyzerEngine(
        registry=registry,
        nlp_engine=analyzer_server.nlp_engine,
        supported_languages=["ru"],
    )


def _api_entities(monkeypatch, analyzer, text, score_threshold=0.35):
    monkeypatch.setattr(analyzer_server, "analyzer", analyzer)
    monkeypatch.setattr(analyzer_server.dp_recognizer, "is_loaded", lambda: False)
    client = TestClient(analyzer_server.app)

    response = client.post(
        "/api/v1/analyze",
        json={
            "text": text,
            "language": "ru",
            "score_threshold": score_threshold,
        },
    )

    assert response.status_code == 200
    return response.json()["entities"]


def _entity_texts(entities, entity_type):
    return [entity["text"] for entity in entities if entity["entity_type"] == entity_type]


def test_production_analyzer_wiring_detects_registered_russian_recognizers(
    monkeypatch,
):
    monkeypatch.setenv("PRESIDIO_ANALYZER_DETECT_BARE_INN_BY_CHECKSUM", "true")
    monkeypatch.setattr(analyzer_server.dp_recognizer, "is_loaded", lambda: False)
    client = TestClient(analyzer_server.app)

    response = client.post(
        "/api/v1/analyze",
        json={
            "text": "ИНН: 500100732259. Адрес регистрации: ул Ленина 10",
            "language": "ru",
            "score_threshold": 0.35,
        },
    )

    assert response.status_code == 200
    entities = response.json()["entities"]
    assert _entity_texts(entities, "RU_INN") == ["500100732259"]
    assert any(
        "ул Ленина 10" in address
        for address in _entity_texts(entities, "RU_ADDRESS")
    )


class TestAnalyzerInnThresholdPolicy:
    def test_default_detects_bare_valid_12_digit_inn_by_checksum(self, monkeypatch):
        monkeypatch.setenv("PRESIDIO_ANALYZER_DETECT_BARE_INN_BY_CHECKSUM", "true")
        analyzer = _build_analyzer(RuInnRecognizer())

        entities = _api_entities(monkeypatch, analyzer, "500100732259")

        assert _entity_texts(entities, "RU_INN") == ["500100732259"]

    @pytest.mark.parametrize("bare_inn", ["1234567894", "2026070415"])
    def test_default_requires_context_for_valid_10_digit_inn(
        self,
        monkeypatch,
        bare_inn,
    ):
        monkeypatch.setenv("PRESIDIO_ANALYZER_DETECT_BARE_INN_BY_CHECKSUM", "true")
        analyzer = _build_analyzer(RuInnRecognizer())

        entities = _api_entities(monkeypatch, analyzer, bare_inn)

        assert _entity_texts(entities, "RU_INN") == []

    def test_default_detects_valid_10_digit_inn_with_context(self, monkeypatch):
        monkeypatch.setenv("PRESIDIO_ANALYZER_DETECT_BARE_INN_BY_CHECKSUM", "true")
        analyzer = _build_analyzer(RuInnRecognizer())

        entities = _api_entities(monkeypatch, analyzer, "ИНН: 1234567894")

        assert _entity_texts(entities, "RU_INN") == ["1234567894"]

    def test_strict_mode_requires_context_for_bare_valid_inn(self, monkeypatch):
        monkeypatch.setenv("PRESIDIO_ANALYZER_DETECT_BARE_INN_BY_CHECKSUM", "false")
        analyzer = _build_analyzer(RuInnRecognizer())

        entities = _api_entities(monkeypatch, analyzer, "7707083893")

        assert _entity_texts(entities, "RU_INN") == []

    def test_strict_mode_detects_valid_inn_with_context(self, monkeypatch):
        monkeypatch.setenv("PRESIDIO_ANALYZER_DETECT_BARE_INN_BY_CHECKSUM", "false")
        analyzer = _build_analyzer(RuInnRecognizer())

        entities = _api_entities(monkeypatch, analyzer, "ИНН: 7707083893")

        assert _entity_texts(entities, "RU_INN") == ["7707083893"]

    @pytest.mark.parametrize("detect_bare", ["true", "false"])
    def test_invalid_checksum_is_not_detected(self, monkeypatch, detect_bare):
        monkeypatch.setenv("PRESIDIO_ANALYZER_DETECT_BARE_INN_BY_CHECKSUM", detect_bare)
        analyzer = _build_analyzer(RuInnRecognizer())

        entities = _api_entities(monkeypatch, analyzer, "ИНН: 7707083894")

        assert _entity_texts(entities, "RU_INN") == []


class TestAnalyzerAddressCorpus:
    @pytest.mark.parametrize(
        "text, expected_fragment",
        [
            ("Проживает по адресу: ул. Ленина, д. 10, кв. 5", "ул. Ленина"),
            ("Адрес: проспект Мира, дом 25", "проспект Мира"),
            ("г. Москва, ул. Тверская, д. 1", "г. Москва, ул. Тверская"),
            ("Адрес: ул.Ленина, д.10", "ул.Ленина"),
            ("г.Москва, ул.Тверская, д.1", "г.Москва, ул.Тверская"),
            ("Адрес: ул. ленина, д. 10", "ул. ленина"),
            ("Адрес регистрации: ул Ленина 10", "ул Ленина 10"),
            ("Фактический адрес: Тверская улица, дом 7", "Тверская улица"),
        ],
    )
    def test_supported_address_patterns(self, monkeypatch, text, expected_fragment):
        analyzer = _build_analyzer(RuAddressRecognizer())

        entities = _api_entities(monkeypatch, analyzer, text)

        address_texts = _entity_texts(entities, "RU_ADDRESS")
        assert any(expected_fragment in address for address in address_texts)

    @pytest.mark.parametrize(
        "text",
        [
            "В отчете улица продаж выросла на 10 процентов",
            "Дом культуры провел встречу в 10 часов",
            "Адрес вопроса не изменился",
            "стул Иванова 10 раз ломался",
            "Тверская улица 10 лет была пешеходной",
            "ул Ленина работает 10 лет",
            "ул. Иванова Петрова 10 человек посетили встречу",
            "Улица Ленина 10 лет была главной",
            "Адрес в строке выше\nул Ленина работает 10 лет",
            "Улица Ленина 10 метров была в ремонте",
            "ул Ленина 10 рублей стоит билет",
            "проспект Ленина 10 домов осталось",
            "ул Ленина 10 квартир продали",
            "ул Ленина 10 этажей построили",
            "ул Ленина 10 месяцев обсуждали",
            "Адрес вопроса: ул Ленина 10 рублей стоит билет",
            "Улица Ленина 10% выросла",
            "ул Ленина 10м была перекрыта",
            "ул Ленина 10км была перекрыта",
        ],
    )
    def test_address_false_positive_corpus(self, monkeypatch, text):
        analyzer = _build_analyzer(RuAddressRecognizer())

        entities = _api_entities(monkeypatch, analyzer, text)

        assert _entity_texts(entities, "RU_ADDRESS") == []
