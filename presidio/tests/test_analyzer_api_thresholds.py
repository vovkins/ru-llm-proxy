"""API-level regression tests for Analyzer threshold-sensitive recognizers."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient
from presidio_analyzer import AnalyzerEngine, RecognizerRegistry, RecognizerResult

from presidio import analyzer_server
from recognizers.ru_bank_requisites import (
    RuBikRecognizer,
    RuCorrespondentAccountRecognizer,
    RuKppRecognizer,
    RuOgrnRecognizer,
    RuOgrnipRecognizer,
    RuSettlementAccountRecognizer,
)
from recognizers.infra_secrets import (
    ApiKeyRecognizer,
    BearerTokenRecognizer,
    CredentialUrlRecognizer,
    HostnameRecognizer,
    InternalDomainRecognizer,
    InternalIpRecognizer,
    JwtRecognizer,
    LoginRecognizer,
    PasswordRecognizer,
    PrivateKeyRecognizer,
)
from recognizers.credential_rules import (
    AuthTokenRecognizer,
    CommandLineCredentialRecognizer,
    SecretKeyRecognizer,
)
from recognizers.ru_address import RuAddressRecognizer
from recognizers.ru_inn import RuInnRecognizer
from result_merging import (
    DETECTION_SOURCE_METADATA_KEY,
    SOURCE_NATIVE_CREDENTIAL,
    SOURCE_NER,
    SOURCE_STRUCTURAL,
)


def _build_analyzer(*recognizers):
    registry = RecognizerRegistry(supported_languages=["ru"])
    for recognizer in recognizers:
        registry.add_recognizer(recognizer)
    return AnalyzerEngine(
        registry=registry,
        nlp_engine=analyzer_server.nlp_engine,
        supported_languages=["ru"],
    )


def _stub_loaded_ner(monkeypatch):
    monkeypatch.setattr(analyzer_server.ner_recognizer, "is_loaded", lambda: True)
    monkeypatch.setattr(analyzer_server.ner_recognizer, "is_warmed_up", lambda: True)
    monkeypatch.setattr(analyzer_server.ner_recognizer, "is_ready", lambda: True)
    monkeypatch.setattr(
        analyzer_server.ner_recognizer,
        "analyze",
        lambda *_args, **_kwargs: [],
    )


def _api_entities(monkeypatch, analyzer, text, score_threshold=0.35):
    monkeypatch.setattr(analyzer_server, "analyzer", analyzer)
    _stub_loaded_ner(monkeypatch)
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


class _EmptyAnalyzer:
    def analyze(self, **_kwargs):
        return []


class _FixedAnalyzer:
    def __init__(self, results):
        self.results = results

    def analyze(self, **_kwargs):
        return list(self.results)


def _sourced_result(entity_type, start, end, score, source):
    return RecognizerResult(
        entity_type=entity_type,
        start=start,
        end=end,
        score=score,
        recognition_metadata={DETECTION_SOURCE_METADATA_KEY: source},
    )


def test_api_prefers_native_value_and_hides_internal_source_metadata(monkeypatch):
    text = "curl -H 'Authorization: Bearer SyntheticToken42'"
    value = "SyntheticToken42"
    start = text.index(value)
    monkeypatch.setattr(
        analyzer_server,
        "analyzer",
        _FixedAnalyzer(
            [
                _sourced_result(
                    "BEARER_TOKEN",
                    text.index("Authorization"),
                    start + len(value),
                    0.99,
                    SOURCE_STRUCTURAL,
                ),
                _sourced_result(
                    "AUTH_TOKEN",
                    start,
                    start + len(value),
                    0.7,
                    SOURCE_NATIVE_CREDENTIAL,
                ),
            ]
        ),
    )
    _stub_loaded_ner(monkeypatch)

    response = TestClient(analyzer_server.app).post(
        "/api/v1/analyze",
        json={"text": text, "language": "ru", "score_threshold": 0.35},
    )

    assert response.status_code == 200
    assert response.json()["entities"] == [
        {
            "entity_type": "AUTH_TOKEN",
            "start": start,
            "end": start + len(value),
            "score": 0.7,
            "text": value,
        }
    ]


@pytest.mark.parametrize(
    "text, value, expected_count",
    [
        ("Договор № AB-2026/0042 вступает в силу.", "AB-2026/0042", 1),
        ("Версия приложения AB-2026/0042 используется в тестах.", "AB-2026/0042", 0),
    ],
)
def test_api_requires_contract_context_for_ner_result(
    monkeypatch, text, value, expected_count
):
    start = text.index(value)
    monkeypatch.setattr(analyzer_server, "analyzer", _EmptyAnalyzer())
    monkeypatch.setattr(analyzer_server.ner_recognizer, "is_ready", lambda: True)
    monkeypatch.setattr(
        analyzer_server.ner_recognizer,
        "analyze",
        lambda *_args, **_kwargs: [
            _sourced_result(
                "CONTRACT_NUMBER",
                start,
                start + len(value),
                0.99,
                SOURCE_NER,
            )
        ],
    )

    response = TestClient(analyzer_server.app).post(
        "/api/v1/analyze",
        json={"text": text, "language": "ru", "score_threshold": 0.35},
    )

    assert response.status_code == 200
    assert len(response.json()["entities"]) == expected_count


def test_api_uses_context_fallback_when_ner_misses_contract(monkeypatch):
    text = "Agreement no. AG-2026-0044 was signed."
    value = "AG-2026-0044"
    monkeypatch.setattr(analyzer_server, "analyzer", _EmptyAnalyzer())
    _stub_loaded_ner(monkeypatch)

    response = TestClient(analyzer_server.app).post(
        "/api/v1/analyze",
        json={"text": text, "language": "ru", "score_threshold": 0.35},
    )

    assert response.status_code == 200
    assert _entity_texts(response.json()["entities"], "CONTRACT_NUMBER") == [
        value
    ]


@pytest.mark.parametrize(
    "request_overrides",
    [
        {"entities": ["PERSON"]},
        {"score_threshold": 0.9},
    ],
)
def test_api_contract_context_fallback_respects_filters(
    monkeypatch,
    request_overrides,
):
    text = "Договор № AB-2026/0042 вступает в силу."
    monkeypatch.setattr(analyzer_server, "analyzer", _EmptyAnalyzer())
    _stub_loaded_ner(monkeypatch)
    payload = {"text": text, "language": "ru", "score_threshold": 0.35}
    payload.update(request_overrides)

    response = TestClient(analyzer_server.app).post(
        "/api/v1/analyze",
        json=payload,
    )

    assert response.status_code == 200
    assert response.json()["entities"] == []


def test_api_preserves_original_unicode_text_and_mapped_ner_offsets(monkeypatch):
    text = "Клиент Сергеи\u0306 Петров согласовал документ."
    entity_text = "Сергеи\u0306 Петров"
    start = text.index(entity_text)
    monkeypatch.setattr(analyzer_server, "analyzer", _EmptyAnalyzer())
    monkeypatch.setattr(analyzer_server.ner_recognizer, "is_ready", lambda: True)

    def mapped_ner_results(received_text, **_kwargs):
        assert received_text == text
        return [
            RecognizerResult(
                entity_type="PERSON",
                start=start,
                end=start + len(entity_text),
                score=0.99,
            )
        ]

    monkeypatch.setattr(
        analyzer_server.ner_recognizer,
        "analyze",
        mapped_ner_results,
    )
    response = TestClient(analyzer_server.app).post(
        "/api/v1/analyze",
        json={"text": text, "language": "ru", "score_threshold": 0.35},
    )

    assert response.status_code == 200
    assert response.json()["text"] == text
    assert response.json()["entities"] == [
        {
            "entity_type": "PERSON",
            "start": start,
            "end": start + len(entity_text),
            "score": 0.99,
            "text": entity_text,
        }
    ]


def test_api_accepts_long_request_and_returns_tail_ner_result(monkeypatch):
    prefix = "Служебная запись без чувствительных значений. " * 90
    entity_text = "Анна Соколова"
    text = prefix + "Ответственный: " + entity_text + "."
    start = text.index(entity_text)
    monkeypatch.setattr(analyzer_server, "analyzer", _EmptyAnalyzer())
    monkeypatch.setattr(analyzer_server.ner_recognizer, "is_ready", lambda: True)
    monkeypatch.setattr(
        analyzer_server.ner_recognizer,
        "analyze",
        lambda *_args, **_kwargs: [
            RecognizerResult(
                entity_type="PERSON",
                start=start,
                end=start + len(entity_text),
                score=0.99,
            )
        ],
    )

    response = TestClient(analyzer_server.app).post(
        "/api/v1/analyze",
        json={"text": text, "language": "ru", "score_threshold": 0.35},
    )

    assert response.status_code == 200
    assert _entity_texts(response.json()["entities"], "PERSON") == [entity_text]


def test_production_analyzer_wiring_detects_registered_russian_recognizers(
    monkeypatch,
):
    monkeypatch.setenv("PRESIDIO_ANALYZER_DETECT_BARE_INN_BY_CHECKSUM", "true")
    _stub_loaded_ner(monkeypatch)
    client = TestClient(analyzer_server.app)

    response = client.post(
        "/api/v1/analyze",
        json={
            "text": (
                "ИНН: 500100732259. "
                "Адрес регистрации: ул Ленина 10. "
                "ОГРН 1027700132195. "
                "БИК 044525225. "
                "Расчетный счет 40702810900000000000. "
                "Внутренний IP 10.24.3.7. "
                "Endpoint api.payments.corp.local. "
                "Authorization: Bearer abcdefghijklmnopqrstuvwxyz123456"
            ),
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
    assert _entity_texts(entities, "RU_OGRN") == ["1027700132195"]
    assert _entity_texts(entities, "RU_BIK") == ["044525225"]
    assert _entity_texts(entities, "RU_SETTLEMENT_ACCOUNT") == [
        "40702810900000000000"
    ]
    assert _entity_texts(entities, "INTERNAL_IP") == ["10.24.3.7"]
    assert _entity_texts(entities, "INTERNAL_DOMAIN") == [
        "api.payments.corp.local"
    ]
    assert _entity_texts(entities, "AUTH_TOKEN") == [
        "abcdefghijklmnopqrstuvwxyz123456"
    ]
    assert _entity_texts(entities, "BEARER_TOKEN") == []


class TestAnalyzerCounterpartyRequisiteThresholdPolicy:
    def test_detects_counterparty_and_bank_requisites_with_context(self, monkeypatch):
        analyzer = _build_analyzer(
            RuKppRecognizer(),
            RuOgrnRecognizer(),
            RuOgrnipRecognizer(),
            RuBikRecognizer(),
            RuSettlementAccountRecognizer(),
            RuCorrespondentAccountRecognizer(),
        )
        text = (
            "Реквизиты: КПП 770801001, ОГРН 1027700132195, "
            "ОГРНИП 304500116000157, БИК 044525225, "
            "расчетный счет 40702810900000000000, "
            "к/с 30101810400000000225."
        )

        entities = _api_entities(monkeypatch, analyzer, text)

        assert _entity_texts(entities, "RU_KPP") == ["770801001"]
        assert _entity_texts(entities, "RU_OGRN") == ["1027700132195"]
        assert _entity_texts(entities, "RU_OGRNIP") == ["304500116000157"]
        assert _entity_texts(entities, "RU_BIK") == ["044525225"]
        assert _entity_texts(entities, "RU_SETTLEMENT_ACCOUNT") == [
            "40702810900000000000"
        ]
        assert _entity_texts(entities, "RU_CORRESPONDENT_ACCOUNT") == [
            "30101810400000000225"
        ]

    @pytest.mark.parametrize(
        "text, entity_type",
        [
            ("770801001", "RU_KPP"),
            ("044525225", "RU_BIK"),
            ("40702810900000000000", "RU_SETTLEMENT_ACCOUNT"),
            ("30101810400000000225", "RU_CORRESPONDENT_ACCOUNT"),
        ],
    )
    def test_context_bound_requisites_do_not_match_bare_digit_runs(
        self,
        monkeypatch,
        text,
        entity_type,
    ):
        analyzer = _build_analyzer(
            RuKppRecognizer(),
            RuBikRecognizer(),
            RuSettlementAccountRecognizer(),
            RuCorrespondentAccountRecognizer(),
        )

        entities = _api_entities(monkeypatch, analyzer, text)

        assert _entity_texts(entities, entity_type) == []

    @pytest.mark.parametrize(
        "text, entity_type",
        [
            ("ОГРН 1027700132196", "RU_OGRN"),
            ("ОГРНИП 304500116000158", "RU_OGRNIP"),
            (
                "Расчетный счет 40702810900000000001, БИК 044525225",
                "RU_SETTLEMENT_ACCOUNT",
            ),
            (
                "БИК 044525225, к/с 30101810400000000226",
                "RU_CORRESPONDENT_ACCOUNT",
            ),
        ],
    )
    def test_invalid_checksums_are_rejected(self, monkeypatch, text, entity_type):
        analyzer = _build_analyzer(
            RuOgrnRecognizer(),
            RuOgrnipRecognizer(),
            RuBikRecognizer(),
            RuSettlementAccountRecognizer(),
            RuCorrespondentAccountRecognizer(),
        )

        entities = _api_entities(monkeypatch, analyzer, text)

        assert _entity_texts(entities, entity_type) == []


class TestAnalyzerInfrastructureSecretThresholdPolicy:
    JWT = (
        "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9."
        "eyJzdWIiOiJzdmMtdXNlciIsImlzcyI6ImlkcCJ9."
        "c2lnbmF0dXJl"
    )

    def test_detects_infrastructure_identifiers_and_secrets(self, monkeypatch):
        analyzer = _build_analyzer(
            InternalIpRecognizer(),
            InternalDomainRecognizer(),
            HostnameRecognizer(),
            CredentialUrlRecognizer(),
            JwtRecognizer(),
            BearerTokenRecognizer(),
            PrivateKeyRecognizer(),
            ApiKeyRecognizer(),
            SecretKeyRecognizer(),
            AuthTokenRecognizer(),
            CommandLineCredentialRecognizer(),
            LoginRecognizer(),
            PasswordRecognizer(),
        )
        private_key = (
            "-----BEGIN PRIVATE KEY-----\n"
            "MIIEvQIBADANBgkqhkiG9w0BAQEFAASC\n"
            "-----END PRIVATE KEY-----"
        )
        api_key = "sk-abcdefghijklmnopqrstuvwxyz123456"
        text = (
            "IP 10.24.3.7, endpoint api.payments.corp.local, "
            "hostname=app-prod-01, "
            "DATABASE_URL=postgresql://svc_user:S3curePass42@db.internal:5432/app, "
            f"JWT {self.JWT}, "
            "Authorization: Bearer abcdefghijklmnopqrstuvwxyz123456, "
            f"provider key {api_key}, "
            "login=svc-bot password=S3cure-Value42, "
            "SECRET_KEY=SyntheticSecretKey42, "
            "AUTH_TOKEN=SyntheticAuthToken42\n"
            f"{private_key}"
        )

        entities = _api_entities(monkeypatch, analyzer, text)

        assert _entity_texts(entities, "INTERNAL_IP") == ["10.24.3.7"]
        assert _entity_texts(entities, "INTERNAL_DOMAIN") == [
            "api.payments.corp.local"
        ]
        assert _entity_texts(entities, "HOSTNAME") == ["hostname=app-prod-01"]
        assert _entity_texts(entities, "DB_URL") == [
            "postgresql://svc_user:S3curePass42@db.internal:5432/app"
        ]
        assert _entity_texts(entities, "JWT") == [self.JWT]
        assert "abcdefghijklmnopqrstuvwxyz123456" in _entity_texts(
            entities,
            "AUTH_TOKEN",
        )
        assert _entity_texts(entities, "BEARER_TOKEN") == []
        assert _entity_texts(entities, "PRIVATE_KEY") == [private_key]
        assert _entity_texts(entities, "API_KEY") == [api_key]
        assert _entity_texts(entities, "SECRET_KEY") == ["SyntheticSecretKey42"]
        assert _entity_texts(entities, "AUTH_TOKEN") == [
            "abcdefghijklmnopqrstuvwxyz123456",
            "SyntheticAuthToken42",
        ]
        assert _entity_texts(entities, "LOGIN") == ["svc-bot"]
        assert _entity_texts(entities, "PASSWORD") == ["S3cure-Value42"]

    @pytest.mark.parametrize(
        "text, entity_type",
        [
            ("8.8.8.8", "INTERNAL_IP"),
            ("docs.github.com", "INTERNAL_DOMAIN"),
            ("app-prod-01", "HOSTNAME"),
            ("https://api.example.com/docs", "DB_URL"),
            ("Объясни, что такое bearer token", "BEARER_TOKEN"),
            ("Объясни, чем API key отличается от password", "API_KEY"),
            ("Объясни, чем API key отличается от password", "PASSWORD"),
        ],
    )
    def test_default_policy_avoids_low_signal_false_positives(
        self,
        monkeypatch,
        text,
        entity_type,
    ):
        analyzer = _build_analyzer(
            InternalIpRecognizer(),
            InternalDomainRecognizer(),
            HostnameRecognizer(),
            CredentialUrlRecognizer(),
            BearerTokenRecognizer(),
            ApiKeyRecognizer(),
            PasswordRecognizer(),
        )

        entities = _api_entities(monkeypatch, analyzer, text)

        assert _entity_texts(entities, entity_type) == []

    def test_public_ip_detection_can_be_enabled(self, monkeypatch):
        monkeypatch.setenv("PRESIDIO_ANALYZER_DETECT_PUBLIC_IPS", "true")
        analyzer = _build_analyzer(InternalIpRecognizer())

        entities = _api_entities(monkeypatch, analyzer, "Public endpoint 8.8.8.8")

        assert _entity_texts(entities, "INTERNAL_IP") == ["8.8.8.8"]

    def test_internal_domain_suffixes_are_configurable(self, monkeypatch):
        monkeypatch.setenv("PRESIDIO_ANALYZER_INTERNAL_DOMAIN_SUFFIXES", "bank.example")
        analyzer = _build_analyzer(InternalDomainRecognizer())

        entities = _api_entities(
            monkeypatch,
            analyzer,
            "Internal endpoint scoring.bank.example",
        )

        assert _entity_texts(entities, "INTERNAL_DOMAIN") == [
            "scoring.bank.example"
        ]


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
            ("Адрес регистрации: ул Маршала Жукова 5", "ул Маршала Жукова 5"),
            ("Адрес: ул Академика Королева 12", "ул Академика Королева 12"),
            ("Проживает по адресу: ул Ленина д 10 кв 5", "ул Ленина д 10 кв 5"),
            ("Адрес: ул. Ленина, д. 10-12", "ул. Ленина, д. 10-12"),
            ("Адрес: ул. Ленина, д. 10/2, кв. 5", "ул. Ленина, д. 10/2, кв. 5"),
            ("Адрес: ул. Ленина, д. 10м", "ул. Ленина, д. 10м"),
            ("Адрес: г. москва, ул. тверская", "г. москва, ул. тверская"),
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
            "ул Маршала Жукова 5 лет обсуждали",
            "Адрес вопроса: ул Академика Королева 12 рублей стоит билет",
            "ул Академика Королева 12 человек пришли",
            "Улица Ленина 10% выросла",
            "ул Ленина 10м была перекрыта",
            "ул Ленина 10км была перекрыта",
        ],
    )
    def test_address_false_positive_corpus(self, monkeypatch, text):
        analyzer = _build_analyzer(RuAddressRecognizer())

        entities = _api_entities(monkeypatch, analyzer, text)

        assert _entity_texts(entities, "RU_ADDRESS") == []
