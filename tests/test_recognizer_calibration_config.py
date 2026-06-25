"""Static checks for recognizer calibration documentation and config."""

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
