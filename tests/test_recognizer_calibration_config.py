"""Static checks for recognizer calibration documentation and config."""

import importlib.util
import re
import sys
import types
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_bare_inn_checksum_setting_is_exposed_to_analyzer_runtime():
    env_name = "PRESIDIO_ANALYZER_DETECT_BARE_INN_BY_CHECKSUM"
    env_example = (ROOT / ".env.example").read_text()
    compose = (ROOT / "docker-compose.yml").read_text()
    setup_script = (ROOT / "scripts" / "setup_env.sh").read_text()

    assert f"{env_name}=true" in env_example
    assert f"{env_name}=${{{env_name}:-true}}" in compose
    assert f'ensure_key_exists "{env_name}" "true"' in setup_script


def test_static_suite_runs_recognizer_calibration_regression():
    makefile = (ROOT / "Makefile").read_text()
    baseline = (ROOT / ".github" / "workflows" / "baseline.yml").read_text()
    compose = (ROOT / "docker-compose.yml").read_text()
    dockerfile = (ROOT / "presidio" / "Dockerfile").read_text()
    runtime_requirements = (
        ROOT / "presidio" / "requirements-analyzer.txt"
    ).read_text()
    api_test_requirements = (
        ROOT / "presidio" / "requirements-analyzer-api-tests.txt"
    ).read_text()
    shared_requirements = (
        ROOT / "presidio" / "requirements-analyzer-base.txt"
    ).read_text()

    assert "tests/test_recognizer_calibration_config.py" in makefile
    assert "test-analyzer-api:" in makefile
    assert "presidio-analyzer-tests" in compose
    assert "presidio-analyzer-tests" in makefile
    assert "make test-analyzer-api" in baseline
    assert "-r requirements-analyzer-base.txt" in runtime_requirements
    assert "-r requirements-analyzer-base.txt" in api_test_requirements
    assert "numpy>=1.26,<2.0" in shared_requirements
    assert "presidio-analyzer>=2.2.362,<3.0" in shared_requirements

    test_stage = dockerfile.split("FROM base AS analyzer-api-tests", 1)[1].split(
        "FROM base AS analyzer",
        1,
    )[0]
    assert "deeppavlov" not in test_stage.lower()
    assert "download_model.py" not in test_stage


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


def _load_module_with_fake_presidio(monkeypatch, module_path, module_name):
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
            global_regex_flags=re.DOTALL | re.MULTILINE | re.IGNORECASE,
        ):
            self.supported_entity = supported_entity
            self.patterns = patterns
            self.context = context
            self.name = name
            self.supported_language = supported_language
            self.global_regex_flags = global_regex_flags

        def enhance_score_with_context(self, text, patterns):
            return patterns

    fake_presidio.Pattern = Pattern
    fake_presidio.PatternRecognizer = PatternRecognizer
    monkeypatch.setitem(sys.modules, "presidio_analyzer", fake_presidio)

    sys.modules.pop(module_name, None)
    spec = importlib.util.spec_from_file_location(module_name, module_path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def _load_ru_inn_with_fake_presidio(monkeypatch):
    return _load_module_with_fake_presidio(
        monkeypatch,
        ROOT / "presidio" / "recognizers" / "ru_inn.py",
        "_ru_inn_config_test",
    )


def _load_ru_address_with_fake_presidio(monkeypatch):
    return _load_module_with_fake_presidio(
        monkeypatch,
        ROOT / "presidio" / "recognizers" / "ru_address.py",
        "_ru_address_config_test",
    )


def test_strict_inn_checksum_validation_does_not_boost_valid_results(monkeypatch):
    monkeypatch.setenv("PRESIDIO_ANALYZER_DETECT_BARE_INN_BY_CHECKSUM", "false")
    ru_inn = _load_ru_inn_with_fake_presidio(monkeypatch)

    recognizer = ru_inn.RuInnRecognizer()

    assert [pattern.score for pattern in recognizer.patterns] == [0.3, 0.2]
    assert recognizer.validate_result("7707083893") is None
    assert recognizer.invalidate_result("7707083894") is True


def test_default_inn_policy_keeps_bare_10_digit_below_api_threshold(monkeypatch):
    monkeypatch.setenv("PRESIDIO_ANALYZER_DETECT_BARE_INN_BY_CHECKSUM", "true")
    ru_inn = _load_ru_inn_with_fake_presidio(monkeypatch)

    recognizer = ru_inn.RuInnRecognizer()

    assert [pattern.score for pattern in recognizer.patterns] == [0.4, 0.2]
    assert recognizer.validate_result("1234567894") is None
    assert recognizer.invalidate_result("1234567894") is False


def test_address_recognizer_keeps_global_regex_case_sensitive(monkeypatch):
    ru_address = _load_ru_address_with_fake_presidio(monkeypatch)

    recognizer = ru_address.RuAddressRecognizer()

    assert recognizer.global_regex_flags & re.IGNORECASE == 0


def test_address_regex_rejects_prose_under_runtime_flags(monkeypatch):
    ru_address = _load_ru_address_with_fake_presidio(monkeypatch)
    recognizer = ru_address.RuAddressRecognizer()

    false_positive_texts = [
        "В отчете улица продаж выросла на 10 процентов",
        "ул Ленина работает 10 лет",
        "ул Ленина\nРаботает 10 лет",
        "ул. Иванова Петрова 10 человек посетили встречу",
        "Улица Ленина 10 лет была главной",
    ]

    for text in false_positive_texts:
        matches = [
            pattern.name
            for pattern in recognizer.patterns
            if re.search(pattern.regex, text, recognizer.global_regex_flags)
        ]
        assert matches == [], text


def test_address_city_pattern_preserves_city_prefixed_house(monkeypatch):
    ru_address = _load_ru_address_with_fake_presidio(monkeypatch)
    recognizer = ru_address.RuAddressRecognizer()

    patterns_by_name = {pattern.name: pattern for pattern in recognizer.patterns}
    city_pattern = patterns_by_name["ru_address_city_street"]
    full_pattern = patterns_by_name["ru_address_full"]

    match = re.search(
        city_pattern.regex,
        "г. Москва, ул. Тверская, д. 1",
        recognizer.global_regex_flags,
    )

    assert match is not None
    assert match.group(0) == "г. Москва, ул. Тверская, д. 1"
    assert city_pattern.score > full_pattern.score


def test_address_regex_accepts_common_marker_casing(monkeypatch):
    ru_address = _load_ru_address_with_fake_presidio(monkeypatch)
    recognizer = ru_address.RuAddressRecognizer()

    positive_texts = [
        "Адрес: ул. ленина, д. 10",
        "Адрес: Ул. Ленина, д. 10",
        "Адрес: ул. Ленина, Дом 10",
        "Адрес: ул.Ленина, д.10",
        "г.Москва, ул.Тверская, д.1",
    ]

    for text in positive_texts:
        matches = [
            pattern.name
            for pattern in recognizer.patterns
            if re.search(pattern.regex, text, recognizer.global_regex_flags)
        ]
        assert matches, text


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
        assert "bare INN" in text or "гол" in text, path
        assert "RU_ADDRESS" in text, path
        assert "огранич" in text or "unsupported" in text, path
