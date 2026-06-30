"""Static checks for recognizer calibration documentation and config."""

import importlib.util
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

    assert "tests/test_recognizer_calibration_config.py" in makefile


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


def test_strict_inn_checksum_validation_does_not_boost_valid_results(monkeypatch):
    monkeypatch.setenv("PRESIDIO_ANALYZER_DETECT_BARE_INN_BY_CHECKSUM", "false")
    ru_inn = _load_ru_inn_with_fake_presidio(monkeypatch)

    recognizer = ru_inn.RuInnRecognizer()

    assert [pattern.score for pattern in recognizer.patterns] == [0.3, 0.2]
    assert recognizer.validate_result("7707083893") is None
    assert recognizer.invalidate_result("7707083894") is True


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
