"""Static checks for recognizer calibration documentation and config."""

import importlib.util
import re
import sys
import types
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_bare_inn_checksum_setting_is_exposed_to_analyzer_runtime():
    env_name = "PRESIDIO_ANALYZER_DETECT_BARE_INN_BY_CHECKSUM"
    configuration = (ROOT / "docs" / "configuration.md").read_text()
    compose = (ROOT / "docker-compose.yml").read_text()
    setup_script = (ROOT / "scripts" / "setup_env.sh").read_text()

    assert f"`{env_name}` | `true`" in configuration
    assert f"{env_name}=${{{env_name}:-true}}" in compose
    assert f'ensure_key_exists "{env_name}"' not in setup_script
    assert "docs/configuration.md" in setup_script


def test_infrastructure_secret_settings_are_exposed_to_analyzer_runtime():
    domain_env = "PRESIDIO_ANALYZER_INTERNAL_DOMAIN_SUFFIXES"
    public_ip_env = "PRESIDIO_ANALYZER_DETECT_PUBLIC_IPS"
    configuration = (ROOT / "docs" / "configuration.md").read_text()
    compose = (ROOT / "docker-compose.yml").read_text()
    setup_script = (ROOT / "scripts" / "setup_env.sh").read_text()

    domain_default_prefix = "internal,local,lan,corp,corp.local"
    assert f"`{domain_env}` | `{domain_default_prefix}" in configuration
    assert (
        f"{domain_env}=${{" + domain_env + f":-{domain_default_prefix}"
        in compose
    )
    assert f'ensure_key_exists "{domain_env}"' not in setup_script

    assert f"`{public_ip_env}` | `false`" in configuration
    assert f"{public_ip_env}=${{{public_ip_env}:-false}}" in compose
    assert f'ensure_key_exists "{public_ip_env}"' not in setup_script


def test_static_suite_runs_recognizer_calibration_regression():
    makefile = (ROOT / "Makefile").read_text()
    workflow = (ROOT / ".github" / "workflows" / "baseline.yml").read_text()
    compose = (ROOT / "docker-compose.yml").read_text()
    dockerfile = (ROOT / "presidio" / "Dockerfile").read_text()

    assert "tests/test_recognizer_calibration_config.py" in makefile
    assert "test-recognizer-api:" in makefile
    assert "make test     — быстрый локальный suite: test-unit + test-static" in makefile
    assert "make test     — запустить весь локальный test suite" not in makefile
    assert "$(PYTHON_LOCAL) tests/test_makefile_routing_smoke.py" in makefile
    assert "$(PYTHON_LOCAL) tests/test_makefile_guardrails_smoke.py" in makefile
    assert "presidio-analyzer-tests" in makefile
    assert "presidio/tests/test_analyzer_api_thresholds.py" in makefile
    assert "make -n test-recognizer-api" in workflow
    assert "recognizer-api:" in workflow
    assert "run: make test-recognizer-api" in workflow
    assert "presidio-analyzer-tests:" in compose
    assert "target: analyzer-test" in compose
    assert "FROM analyzer-runtime AS analyzer-test" in dockerfile


def test_analyzer_api_threshold_tests_use_russian_nlp_engine():
    test_source = (ROOT / "presidio" / "tests" / "test_analyzer_api_thresholds.py").read_text()

    assert "importorskip" not in test_source
    assert "RecognizerRegistry(supported_languages=[\"ru\"])" in test_source
    assert "nlp_engine=analyzer_server.nlp_engine" in test_source
    assert "supported_languages=[\"ru\"]" in test_source
    assert "test_production_analyzer_wiring_detects_registered_russian_recognizers" in test_source
    production_smoke = test_source.split(
        "def test_production_analyzer_wiring_detects_registered_russian_recognizers",
        1,
    )[1].split("\n\n", 1)[0]
    assert "monkeypatch.setattr(analyzer_server, \"analyzer\"" not in production_smoke


def _load_ru_inn_with_fake_presidio(monkeypatch):
    fake_presidio = types.ModuleType("presidio_analyzer")

    class Pattern:
        def __init__(self, name, regex, score):
            self.name = name
            self.regex = regex
            self.score = score

    class PatternRecognizer:
        def __init__(
            self,
            supported_entity,
            patterns,
            context,
            name,
            supported_language,
        ):
            self.supported_entity = supported_entity
            self.patterns = patterns
            self.context = context
            self.name = name
            self.supported_language = supported_language

        def enhance_score_with_context(self, text, patterns):
            return patterns

    fake_presidio.Pattern = Pattern
    fake_presidio.PatternRecognizer = PatternRecognizer
    monkeypatch.setitem(sys.modules, "presidio_analyzer", fake_presidio)

    module_name = "_ru_inn_config_test"
    sys.modules.pop(module_name, None)
    spec = importlib.util.spec_from_file_location(
        module_name,
        ROOT / "presidio" / "recognizers" / "ru_inn.py",
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def _load_ru_address_with_fake_presidio(monkeypatch):
    fake_presidio = types.ModuleType("presidio_analyzer")

    class Pattern:
        def __init__(self, name, regex, score):
            self.name = name
            self.regex = regex
            self.score = score

    class PatternRecognizer:
        def __init__(
            self,
            supported_entity,
            patterns,
            context,
            name,
            supported_language,
        ):
            self.supported_entity = supported_entity
            self.patterns = patterns
            self.context = context
            self.name = name
            self.supported_language = supported_language

    fake_presidio.Pattern = Pattern
    fake_presidio.PatternRecognizer = PatternRecognizer
    monkeypatch.setitem(sys.modules, "presidio_analyzer", fake_presidio)

    module_name = "_ru_address_config_test"
    sys.modules.pop(module_name, None)
    spec = importlib.util.spec_from_file_location(
        module_name,
        ROOT / "presidio" / "recognizers" / "ru_address.py",
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def test_strict_inn_checksum_validation_does_not_boost_valid_results(monkeypatch):
    monkeypatch.setenv("PRESIDIO_ANALYZER_DETECT_BARE_INN_BY_CHECKSUM", "false")
    ru_inn = _load_ru_inn_with_fake_presidio(monkeypatch)

    recognizer = ru_inn.RuInnRecognizer()

    assert [pattern.score for pattern in recognizer.patterns] == [0.3, 0.2]
    assert recognizer.validate_result("7707083893") is None
    assert recognizer.invalidate_result("7707083894") is True


def test_default_inn_checksum_mode_keeps_bare_10_digit_below_threshold(monkeypatch):
    monkeypatch.setenv("PRESIDIO_ANALYZER_DETECT_BARE_INN_BY_CHECKSUM", "true")
    ru_inn = _load_ru_inn_with_fake_presidio(monkeypatch)

    recognizer = ru_inn.RuInnRecognizer()

    assert [pattern.score for pattern in recognizer.patterns] == [0.4, 0.3]
    assert recognizer.validate_result("1234567894") is None
    assert recognizer.invalidate_result("1234567894") is False


def test_address_patterns_are_safe_under_presidio_case_insensitive_matching(monkeypatch):
    ru_address = _load_ru_address_with_fake_presidio(monkeypatch)
    patterns_by_name = {
        pattern.name: pattern
        for pattern in ru_address.RuAddressRecognizer.PATTERNS
    }

    def matches(text):
        result = []
        for pattern in ru_address.RuAddressRecognizer.PATTERNS:
            for match in re.finditer(
                pattern.regex,
                text,
                flags=re.IGNORECASE | re.MULTILINE | re.DOTALL,
            ):
                result.append(match.group(0))
        return result

    assert patterns_by_name["ru_address_street_house_bare"].score < 0.35
    assert matches("В отчете улица продаж выросла на 10 процентов") == []
    assert matches("Тверская улица 10 лет была пешеходной") == []
    assert matches("стул Иванова 10 раз ломался") == []
    assert matches("ул Ленина работает 10 лет") == []
    assert matches("ул. Иванова Петрова 10 человек посетили встречу") == []
    assert matches("Улица Ленина 10 лет была главной") == []
    assert matches("Адрес в строке выше\nул Ленина работает 10 лет") == []
    assert matches("Улица Ленина 10 метров была в ремонте") == []
    assert matches("ул Ленина 10 рублей стоит билет") == []
    assert matches("проспект Ленина 10 домов осталось") == []
    assert matches("ул Ленина 10 квартир продали") == []
    assert matches("ул Ленина 10 этажей построили") == []
    assert matches("ул Ленина 10 месяцев обсуждали") == []
    assert matches("Адрес вопроса: ул Ленина 10 рублей стоит билет") == []
    assert matches("ул Маршала Жукова 5 лет обсуждали") == []
    assert matches("Адрес вопроса: ул Академика Королева 12 рублей стоит билет") == []
    assert matches("ул Академика Королева 12 человек пришли") == []
    assert matches("Улица Ленина 10% выросла") == []
    assert matches("ул Ленина 10м была перекрыта") == []
    assert matches("ул Ленина 10км была перекрыта") == []
    assert "ул.Ленина, д.10" in matches("Адрес: ул.Ленина, д.10")
    assert "ул. ленина, д. 10" in matches("Адрес: ул. ленина, д. 10")
    assert "ул Маршала Жукова 5" in matches(
        "Адрес регистрации: ул Маршала Жукова 5",
    )
    assert "ул Академика Королева 12" in matches(
        "Адрес: ул Академика Королева 12",
    )
    assert "ул Ленина д 10 кв 5" in matches(
        "Проживает по адресу: ул Ленина д 10 кв 5",
    )
    assert "ул. Ленина, д. 10-12" in matches("Адрес: ул. Ленина, д. 10-12")
    assert "ул. Ленина, д. 10/2, кв. 5" in matches(
        "Адрес: ул. Ленина, д. 10/2, кв. 5",
    )
    assert "ул. Ленина, д. 10м" in matches("Адрес: ул. Ленина, д. 10м")
    assert "г. москва, ул. тверская" in matches(
        "Адрес: г. москва, ул. тверская",
    )
    assert "Тверская улица, дом 7" in matches(
        "Фактический адрес: Тверская улица, дом 7",
    )
    assert "г. Москва, ул. Тверская, д. 1" in matches(
        "г. Москва, ул. Тверская, д. 1",
    )
    assert "г.Москва, ул.Тверская, д.1" in matches(
        "г.Москва, ул.Тверская, д.1",
    )
    assert "улица" not in ru_address.RuAddressRecognizer.CONTEXT
    assert "дом" not in ru_address.RuAddressRecognizer.CONTEXT
    assert "квартира" not in ru_address.RuAddressRecognizer.CONTEXT
    assert "адрес" in ru_address.RuAddressRecognizer.CONTEXT


def test_docs_explain_inn_threshold_policy_and_address_limits():
    docs = {
        "README.md": (ROOT / "README.md").read_text(),
        "docs/architecture.md": (ROOT / "docs" / "architecture.md").read_text(),
        "docs/examples.md": (ROOT / "docs" / "examples.md").read_text(),
    }

    for path, text in docs.items():
        assert "PRESIDIO_ANALYZER_DETECT_BARE_INN_BY_CHECKSUM" in text, path
        assert "score_threshold=0.35" in text or '"score_threshold": 0.35' in text, path
        assert "checksum" in text or "контрольн" in text, path
        assert "без контекст" in text, path

    for path in ("docs/architecture.md", "docs/examples.md"):
        text = docs[path]
        assert "RU_ADDRESS" in text, path
        assert "огранич" in text or "unsupported" in text, path


def test_counterparty_requisite_recognizers_are_wired_and_documented():
    recognizers_init = (ROOT / "presidio" / "recognizers" / "__init__.py").read_text()
    requisites = (
        ROOT / "presidio" / "recognizers" / "ru_bank_requisites.py"
    ).read_text()
    readme = (ROOT / "README.md").read_text()
    architecture = (ROOT / "docs" / "architecture.md").read_text()
    examples = (ROOT / "docs" / "examples.md").read_text()
    compliance = (ROOT / "docs" / "compliance.md").read_text()

    for class_name in (
        "RuKppRecognizer",
        "RuOgrnRecognizer",
        "RuOgrnipRecognizer",
        "RuBikRecognizer",
        "RuSettlementAccountRecognizer",
        "RuCorrespondentAccountRecognizer",
    ):
        assert class_name in recognizers_init

    for entity_type in (
        "RU_KPP",
        "RU_OGRN",
        "RU_OGRNIP",
        "RU_BIK",
        "RU_SETTLEMENT_ACCOUNT",
        "RU_CORRESPONDENT_ACCOUNT",
    ):
        assert entity_type in requisites
        assert entity_type in architecture
        assert entity_type in examples
        assert entity_type in compliance

    assert "_account_checksum_valid" in requisites
    assert "_find_contextual_biks" in requisites
    assert "_required_context_re" in requisites
    assert "score_threshold=0.35" in readme
    assert "score_threshold=0.35" in architecture
    assert '"score_threshold": 0.35' in examples
    assert "онлайн-провер" in architecture


def test_infrastructure_secret_recognizers_are_wired_and_documented():
    recognizers_init = (ROOT / "presidio" / "recognizers" / "__init__.py").read_text()
    infra_secrets = (
        ROOT / "presidio" / "recognizers" / "infra_secrets.py"
    ).read_text()
    readme = (ROOT / "README.md").read_text()
    architecture = (ROOT / "docs" / "architecture.md").read_text()
    examples = (ROOT / "docs" / "examples.md").read_text()
    compliance = (ROOT / "docs" / "compliance.md").read_text()

    for class_name in (
        "InternalIpRecognizer",
        "InternalDomainRecognizer",
        "HostnameRecognizer",
        "CredentialUrlRecognizer",
        "JwtRecognizer",
        "BearerTokenRecognizer",
        "PrivateKeyRecognizer",
        "ApiKeyRecognizer",
        "LoginRecognizer",
        "PasswordRecognizer",
    ):
        assert class_name in recognizers_init

    for entity_type in (
        "INTERNAL_IP",
        "INTERNAL_DOMAIN",
        "HOSTNAME",
        "DB_URL",
        "JWT",
        "BEARER_TOKEN",
        "PRIVATE_KEY",
        "API_KEY",
        "LOGIN",
        "PASSWORD",
    ):
        assert entity_type in infra_secrets
        assert entity_type in architecture
        assert entity_type in examples
        assert entity_type in compliance

    assert "ipaddress.ip_address" in infra_secrets
    assert "PRESIDIO_ANALYZER_INTERNAL_DOMAIN_SUFFIXES" in infra_secrets
    assert "PRESIDIO_ANALYZER_DETECT_PUBLIC_IPS" in infra_secrets
    assert "score_threshold=0.35" in readme
    assert "score_threshold=0.35" in architecture
    assert '"score_threshold": 0.35' in examples
    assert "не как классификатор всей полезной нагрузки" in architecture
    assert "не является полноценным сканированием секретов в исходном коде" in compliance


def test_phone_recognizer_requires_digit_boundaries_for_country_code_pattern():
    source = (ROOT / "presidio" / "recognizers" / "ru_phone.py").read_text()

    assert r"(?<!\d)(?:\+?7|8)" in source
    assert r"(?!\d)" in source


def test_readme_documents_recognizer_api_target():
    readme = (ROOT / "README.md").read_text()

    assert "| `make test` | Быстрый локальный набор: `test-unit` и `test-static`. |" in readme
    assert "| `make test-static` | Лёгкие статические и asyncio-регрессионные тесты на хосте через `PYTHON_LOCAL`. |" in readme
    assert "| `make test-recognizer-api` |" in readme
    assert "make test-recognizer-api" in readme
    assert "PYTHON_LOCAL" in readme
    assert "Локальные тесты запускаются через Docker" not in readme
