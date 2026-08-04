"""Tests for Russian PII recognizers."""

import pytest

from presidio_analyzer import AnalyzerEngine
from presidio_analyzer.nlp_engine import NlpEngineProvider

from presidio.recognizers import ALL_RECOGNIZERS


def _entity_texts(text, results, entity_type):
    return [text[result.start:result.end] for result in results if result.entity_type == entity_type]


@pytest.fixture
def analyzer():
    """Create analyzer with all Russian recognizers registered."""
    nlp_engine = NlpEngineProvider(
        nlp_configuration={
            "nlp_engine_name": "spacy",
            "models": [{"lang_code": "ru", "model_name": "ru_core_news_sm"}],
        }
    ).create_engine()
    engine = AnalyzerEngine(nlp_engine=nlp_engine)
    for recognizer_cls in ALL_RECOGNIZERS:
        engine.registry.add_recognizer(recognizer_cls())
    return engine


# === Phone ===

class TestRuPhone:
    def test_mobile_with_plus7(self, analyzer):
        results = analyzer.analyze("Мой телефон +7 903 123 45 67", language="ru")
        phone_results = [r for r in results if r.entity_type == "PHONE_NUMBER"]
        assert len(phone_results) >= 1

    def test_mobile_with_8(self, analyzer):
        results = analyzer.analyze("Звоните: 89031234567", language="ru")
        phone_results = [r for r in results if r.entity_type == "PHONE_NUMBER"]
        assert len(phone_results) >= 1

    def test_formatted_phone(self, analyzer):
        results = analyzer.analyze("Тел.: +7 (495) 123-45-67", language="ru")
        phone_results = [r for r in results if r.entity_type == "PHONE_NUMBER"]
        assert len(phone_results) >= 1

    def test_no_false_positive_short(self, analyzer):
        results = analyzer.analyze("Цена: 12345 рублей", language="ru")
        phone_results = [r for r in results if r.entity_type == "PHONE_NUMBER"]
        assert len(phone_results) == 0

    def test_no_false_positive_inside_long_digit_run(self, analyzer):
        results = analyzer.analyze(
            "Идентификатор операции 40702810900000000000",
            language="ru",
            score_threshold=0.35,
        )
        phone_results = [r for r in results if r.entity_type == "PHONE_NUMBER"]
        assert len(phone_results) == 0


# === Email ===

class TestRuEmail:
    def test_basic_email(self, analyzer):
        results = analyzer.analyze("Напишите на email: test@example.com", language="ru")
        email_results = [r for r in results if r.entity_type == "EMAIL_ADDRESS"]
        assert len(email_results) >= 1

    def test_russian_domain(self, analyzer):
        results = analyzer.analyze("Почта: ivan@yandex.ru", language="ru")
        email_results = [r for r in results if r.entity_type == "EMAIL_ADDRESS"]
        assert len(email_results) >= 1


# === INN ===

class TestRuInn:
    def test_inn_legal_entity_valid(self, analyzer):
        # Valid 10-digit INN (Sberbank)
        results = analyzer.analyze("ИНН: 7707083893", language="ru")
        inn_results = [r for r in results if r.entity_type == "RU_INN"]
        assert len(inn_results) >= 1

    def test_inn_individual_valid(self, analyzer):
        # Valid 12-digit INN (synthetic but passes checksum)
        # We use a context word to boost score
        results = analyzer.analyze("Мой ИНН налогоплательщика 500100732259", language="ru")
        inn_results = [r for r in results if r.entity_type == "RU_INN"]
        assert len(inn_results) >= 1

    def test_inn_invalid_checksum(self, analyzer):
        # Invalid checksum - should not be detected
        results = analyzer.analyze("ИНН: 7707083894", language="ru")
        inn_results = [r for r in results if r.entity_type == "RU_INN"]
        assert len(inn_results) == 0


# === Counterparty and bank requisites ===

class TestRuCounterpartyRequisites:
    def test_kpp_requires_context(self, analyzer):
        positive_text = "КПП получателя: 770801001"
        results = analyzer.analyze(
            positive_text,
            language="ru",
            score_threshold=0.35,
        )
        assert _entity_texts(positive_text, results, "RU_KPP") == ["770801001"]

        negative_text = "Код строки 770801001 указан в таблице"
        results = analyzer.analyze(
            negative_text,
            language="ru",
            score_threshold=0.35,
        )
        assert _entity_texts(negative_text, results, "RU_KPP") == []

    def test_ogrn_valid_checksum(self, analyzer):
        text = "ОГРН контрагента 1027700132195"
        results = analyzer.analyze(text, language="ru", score_threshold=0.35)
        assert _entity_texts(text, results, "RU_OGRN") == ["1027700132195"]

    def test_ogrn_invalid_checksum(self, analyzer):
        text = "ОГРН контрагента 1027700132196"
        results = analyzer.analyze(text, language="ru", score_threshold=0.35)
        assert _entity_texts(text, results, "RU_OGRN") == []

    def test_ogrnip_valid_checksum(self, analyzer):
        text = "ОГРНИП предпринимателя 304500116000157"
        results = analyzer.analyze(text, language="ru", score_threshold=0.35)
        assert _entity_texts(text, results, "RU_OGRNIP") == ["304500116000157"]

    def test_ogrnip_invalid_checksum(self, analyzer):
        text = "ОГРНИП предпринимателя 304500116000158"
        results = analyzer.analyze(text, language="ru", score_threshold=0.35)
        assert _entity_texts(text, results, "RU_OGRNIP") == []

    def test_bik_requires_context(self, analyzer):
        positive_text = "БИК банка получателя 044525225"
        results = analyzer.analyze(
            positive_text,
            language="ru",
            score_threshold=0.35,
        )
        assert _entity_texts(positive_text, results, "RU_BIK") == ["044525225"]

        negative_text = "В таблице есть номер 044525225 без банковского контекста"
        results = analyzer.analyze(
            negative_text,
            language="ru",
            score_threshold=0.35,
        )
        assert _entity_texts(negative_text, results, "RU_BIK") == []

    def test_settlement_account_with_bik_checksum(self, analyzer):
        text = "Расчетный счет 40702810900000000000, БИК 044525225"
        results = analyzer.analyze(text, language="ru", score_threshold=0.35)
        assert _entity_texts(text, results, "RU_SETTLEMENT_ACCOUNT") == [
            "40702810900000000000"
        ]
        assert _entity_texts(text, results, "RU_BIK") == ["044525225"]

    def test_settlement_account_rejects_invalid_bik_checksum(self, analyzer):
        text = "Расчетный счет 40702810900000000001, БИК 044525225"
        results = analyzer.analyze(text, language="ru", score_threshold=0.35)
        assert _entity_texts(text, results, "RU_SETTLEMENT_ACCOUNT") == []

    def test_settlement_account_rejects_generic_twenty_digit_number(self, analyzer):
        text = "Идентификатор операции 40702810900000000000 сохранен"
        results = analyzer.analyze(text, language="ru", score_threshold=0.35)
        assert _entity_texts(text, results, "RU_SETTLEMENT_ACCOUNT") == []

    def test_correspondent_account_with_bik_checksum(self, analyzer):
        text = "БИК 044525225, к/с 30101810400000000225"
        results = analyzer.analyze(text, language="ru", score_threshold=0.35)
        assert _entity_texts(text, results, "RU_CORRESPONDENT_ACCOUNT") == [
            "30101810400000000225"
        ]

    def test_correspondent_account_rejects_invalid_bik_checksum(self, analyzer):
        text = "БИК 044525225, к/с 30101810400000000226"
        results = analyzer.analyze(text, language="ru", score_threshold=0.35)
        assert _entity_texts(text, results, "RU_CORRESPONDENT_ACCOUNT") == []


# === Infrastructure identifiers and secrets ===

class TestInfrastructureSecrets:
    JWT = (
        "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9."
        "eyJzdWIiOiJzdmMtdXNlciIsImlzcyI6ImlkcCJ9."
        "c2lnbmF0dXJl"
    )

    def test_private_ipv4_is_detected(self, analyzer):
        text = "Внутренний endpoint 10.24.3.7 доступен только из VPN"
        results = analyzer.analyze(text, language="ru", score_threshold=0.35)
        assert _entity_texts(text, results, "INTERNAL_IP") == ["10.24.3.7"]

    def test_private_ipv4_before_sentence_punctuation_is_detected(self, analyzer):
        text = "Внутренний endpoint 10.24.3.7."
        results = analyzer.analyze(text, language="ru", score_threshold=0.35)
        assert _entity_texts(text, results, "INTERNAL_IP") == ["10.24.3.7"]

    def test_public_ipv4_is_not_detected_by_default(self, analyzer):
        text = "Публичный DNS 8.8.8.8 указан как пример"
        results = analyzer.analyze(text, language="ru", score_threshold=0.35)
        assert _entity_texts(text, results, "INTERNAL_IP") == []

    def test_public_ipv4_detection_is_policy_controlled(self, analyzer, monkeypatch):
        monkeypatch.setenv("PRESIDIO_ANALYZER_DETECT_PUBLIC_IPS", "true")
        text = "Публичный endpoint 8.8.8.8 тоже считаем чувствительным"
        results = analyzer.analyze(text, language="ru", score_threshold=0.35)
        assert _entity_texts(text, results, "INTERNAL_IP") == ["8.8.8.8"]

    def test_internal_ipv6_is_detected(self, analyzer):
        text = "Service IP fd00::10 используется в overlay network"
        results = analyzer.analyze(text, language="ru", score_threshold=0.35)
        assert _entity_texts(text, results, "INTERNAL_IP") == ["fd00::10"]

    def test_invalid_ip_is_rejected(self, analyzer):
        text = "Версия 999.999.999.999 не является IP"
        results = analyzer.analyze(text, language="ru", score_threshold=0.35)
        assert _entity_texts(text, results, "INTERNAL_IP") == []

    def test_internal_domain_suffix_is_detected(self, analyzer):
        text = "Endpoint api.payments.corp.local вызывает backend"
        results = analyzer.analyze(text, language="ru", score_threshold=0.35)
        assert _entity_texts(text, results, "INTERNAL_DOMAIN") == [
            "api.payments.corp.local"
        ]

    def test_internal_domain_before_sentence_punctuation_is_detected(self, analyzer):
        text = "Endpoint api.payments.corp.local."
        results = analyzer.analyze(text, language="ru", score_threshold=0.35)
        assert _entity_texts(text, results, "INTERNAL_DOMAIN") == [
            "api.payments.corp.local"
        ]

    def test_external_domain_is_not_internal_domain(self, analyzer):
        text = "Документация лежит на docs.github.com"
        results = analyzer.analyze(text, language="ru", score_threshold=0.35)
        assert _entity_texts(text, results, "INTERNAL_DOMAIN") == []

    def test_context_bound_hostname_is_detected(self, analyzer):
        text = "hostname=app-prod-01 обрабатывает платежи"
        results = analyzer.analyze(text, language="ru", score_threshold=0.35)
        assert _entity_texts(text, results, "HOSTNAME") == ["hostname=app-prod-01"]

    def test_bare_hostname_like_word_is_not_detected(self, analyzer):
        text = "app-prod-01 выглядит как имя, но контекста хоста нет"
        results = analyzer.analyze(text, language="ru", score_threshold=0.35)
        assert _entity_texts(text, results, "HOSTNAME") == []

    def test_credential_url_is_detected(self, analyzer):
        text = "DATABASE_URL=postgresql://svc_user:S3curePass42@db.internal:5432/app"
        results = analyzer.analyze(text, language="ru", score_threshold=0.35)
        assert _entity_texts(text, results, "DB_URL") == [
            "postgresql://svc_user:S3curePass42@db.internal:5432/app"
        ]

    def test_url_without_credentials_is_not_db_url(self, analyzer):
        text = "Документация: https://api.example.com/docs"
        results = analyzer.analyze(text, language="ru", score_threshold=0.35)
        assert _entity_texts(text, results, "DB_URL") == []

    def test_jwt_is_detected_and_invalid_jwt_is_rejected(self, analyzer):
        positive_text = f"OIDC token {self.JWT}"
        results = analyzer.analyze(positive_text, language="ru", score_threshold=0.35)
        assert _entity_texts(positive_text, results, "JWT") == [self.JWT]

        negative_text = "token eyJnotreally.abcdefghi.signature"
        results = analyzer.analyze(negative_text, language="ru", score_threshold=0.35)
        assert _entity_texts(negative_text, results, "JWT") == []

    def test_bearer_token_is_detected(self, analyzer):
        token = "Bearer abcdefghijklmnopqrstuvwxyz123456"
        text = f"Authorization: {token}"
        results = analyzer.analyze(text, language="ru", score_threshold=0.35)
        assert _entity_texts(text, results, "BEARER_TOKEN") == [
            f"Authorization: {token}"
        ]

    def test_private_key_block_is_detected(self, analyzer):
        key = (
            "-----BEGIN PRIVATE KEY-----\n"
            "MIIEvQIBADANBgkqhkiG9w0BAQEFAASC\n"
            "-----END PRIVATE KEY-----"
        )
        text = f"Ключ:\n{key}"
        results = analyzer.analyze(text, language="ru", score_threshold=0.35)
        assert _entity_texts(text, results, "PRIVATE_KEY") == [key]

    def test_api_key_is_detected_but_placeholders_are_rejected(self, analyzer):
        key = "sk-abcdefghijklmnopqrstuvwxyz123456"
        text = f"Provider key {key}"
        results = analyzer.analyze(text, language="ru", score_threshold=0.35)
        assert _entity_texts(text, results, "API_KEY") == [key]

        placeholder_text = "OPENAI_API_KEY=your-api-key-placeholder"
        results = analyzer.analyze(placeholder_text, language="ru", score_threshold=0.35)
        assert _entity_texts(placeholder_text, results, "API_KEY") == []

    def test_login_and_password_assignments_are_detected(self, analyzer):
        text = "login=svc-bot password=S3cure-Value42"
        results = analyzer.analyze(text, language="ru", score_threshold=0.35)
        assert _entity_texts(text, results, "LOGIN") == ["svc-bot"]
        assert _entity_texts(text, results, "PASSWORD") == ["S3cure-Value42"]

    def test_incidental_secret_words_are_not_detected(self, analyzer):
        text = "Объясни, чем API key отличается от bearer token и password."
        results = analyzer.analyze(text, language="ru", score_threshold=0.35)
        for entity_type in ("API_KEY", "BEARER_TOKEN", "PASSWORD", "LOGIN"):
            assert _entity_texts(text, results, entity_type) == []


# === SNILS ===

class TestRuSnils:
    def test_snils_valid(self, analyzer):
        # Valid SNILS: 112 233 445 95
        results = analyzer.analyze("СНИЛС: 112 233 445 95", language="ru")
        snils_results = [r for r in results if r.entity_type == "RU_SNILS"]
        assert len(snils_results) >= 1

    def test_snils_invalid(self, analyzer):
        # Invalid checksum
        results = analyzer.analyze("СНИЛС: 112 233 445 00", language="ru")
        snils_results = [r for r in results if r.entity_type == "RU_SNILS"]
        assert len(snils_results) == 0


# === Passport ===

class TestRuPassport:
    def test_passport_spaced(self, analyzer):
        results = analyzer.analyze("Паспорт: 45 10 123456", language="ru")
        passport_results = [r for r in results if r.entity_type == "RU_PASSPORT"]
        assert len(passport_results) >= 1

    def test_passport_compact(self, analyzer):
        results = analyzer.analyze("Серия и номер паспорта 4510 123456", language="ru")
        passport_results = [r for r in results if r.entity_type == "RU_PASSPORT"]
        assert len(passport_results) >= 1

    def test_passport_invalid_region(self, analyzer):
        results = analyzer.analyze("Паспорт: 00 00 123456", language="ru")
        passport_results = [r for r in results if r.entity_type == "RU_PASSPORT"]
        assert len(passport_results) == 0


# === Bank Card ===

class TestRuBankCard:
    def test_valid_card(self, analyzer):
        # Valid test card number (passes Luhn)
        results = analyzer.analyze("Номер карты: 4111 1111 1111 1111", language="ru")
        card_results = [r for r in results if r.entity_type == "CREDIT_CARD"]
        assert len(card_results) >= 1

    def test_invalid_card(self, analyzer):
        # Fails Luhn
        results = analyzer.analyze("Номер карты: 4111 1111 1111 1112", language="ru")
        card_results = [r for r in results if r.entity_type == "CREDIT_CARD"]
        assert len(card_results) == 0


# === Address ===

class TestRuAddress:
    def test_full_address(self, analyzer):
        results = analyzer.analyze(
            "Проживает по адресу: ул. Ленина, д. 10, кв. 5", language="ru"
        )
        addr_results = [r for r in results if r.entity_type == "RU_ADDRESS"]
        assert len(addr_results) >= 1

    def test_prospect_address(self, analyzer):
        results = analyzer.analyze(
            "Адрес: проспект Мира, дом 25", language="ru"
        )
        addr_results = [r for r in results if r.entity_type == "RU_ADDRESS"]
        assert len(addr_results) >= 1

    def test_city_street(self, analyzer):
        results = analyzer.analyze(
            "г. Москва, ул. Тверская, д. 1", language="ru"
        )
        addr_results = [r for r in results if r.entity_type == "RU_ADDRESS"]
        assert len(addr_results) >= 1
