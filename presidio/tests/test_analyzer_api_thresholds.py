"""API-level regression tests for Analyzer threshold-sensitive recognizers."""

import sys
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("presidio_analyzer")

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient
from presidio_analyzer import AnalyzerEngine

from presidio import analyzer_server
from recognizers.ru_address import RuAddressRecognizer
from recognizers.ru_inn import RuInnRecognizer


def _build_analyzer(*recognizers):
    engine = AnalyzerEngine()
    for recognizer in recognizers:
        engine.registry.add_recognizer(recognizer)
    return engine


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


class TestAnalyzerInnThresholdPolicy:
    def test_default_detects_bare_valid_inn_by_checksum(self, monkeypatch):
        monkeypatch.setenv("PRESIDIO_ANALYZER_DETECT_BARE_INN_BY_CHECKSUM", "true")
        analyzer = _build_analyzer(RuInnRecognizer())

        entities = _api_entities(monkeypatch, analyzer, "7707083893")

        assert _entity_texts(entities, "RU_INN") == ["7707083893"]

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
        ],
    )
    def test_address_false_positive_corpus(self, monkeypatch, text):
        analyzer = _build_analyzer(RuAddressRecognizer())

        entities = _api_entities(monkeypatch, analyzer, text)

        assert _entity_texts(entities, "RU_ADDRESS") == []
