"""Static checks for reversible dictionary substitution configuration."""

import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _read(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def test_dictionary_substitution_env_and_compose_defaults_are_enabled():
    env_example = _read(".env.example")
    compose = _read("docker-compose.yml")
    setup_script = _read("scripts/setup_env.sh")

    assert "DICTIONARY_SUBSTITUTIONS_ENABLED=true" in env_example
    assert (
        "DICTIONARY_SUBSTITUTIONS_FILE=/app/litellm_guardrails/"
        "dictionary-substitutions.default.json"
    ) in env_example
    assert "DICTIONARY_SUBSTITUTIONS_FAILURE_MODE=fail_closed" in env_example

    assert (
        "DICTIONARY_SUBSTITUTIONS_ENABLED=${DICTIONARY_SUBSTITUTIONS_ENABLED:-true}"
    ) in compose
    assert (
        "DICTIONARY_SUBSTITUTIONS_FILE=${DICTIONARY_SUBSTITUTIONS_FILE:-"
        "/app/litellm_guardrails/dictionary-substitutions.default.json}"
    ) in compose
    assert (
        "DICTIONARY_SUBSTITUTIONS_FAILURE_MODE="
        "${DICTIONARY_SUBSTITUTIONS_FAILURE_MODE:-fail_closed}"
    ) in compose

    assert 'ensure_key_exists "DICTIONARY_SUBSTITUTIONS_ENABLED" "true"' in setup_script
    assert (
        'ensure_key_exists "DICTIONARY_SUBSTITUTIONS_FILE" '
        '"/app/litellm_guardrails/dictionary-substitutions.default.json"'
    ) in setup_script
    assert (
        'ensure_key_exists "DICTIONARY_SUBSTITUTIONS_FAILURE_MODE" "fail_closed"'
        in setup_script
    )


def test_default_dictionary_seed_contains_top_ten_bank_rules():
    config = json.loads(
        _read("litellm_guardrails/dictionary-substitutions.default.json")
    )
    rules = config["substitutions"]

    assert len(rules) == 10
    assert [rule["source"] for rule in rules] == [
        "Сбербанк",
        "ВТБ",
        "Газпромбанк",
        "Альфа-Банк",
        "ПСБ",
        "Россельхозбанк",
        "Т-Банк",
        "Московский кредитный банк",
        "Банк Дом.РФ",
        "Совкомбанк",
    ]
    assert all(rule["enabled"] is True for rule in rules)
    assert all(rule["restore"] is True for rule in rules)
    assert len({rule["id"] for rule in rules}) == 10
    assert len({rule["replacement"] for rule in rules}) == 10


def test_dictionary_policy_is_documented_in_primary_docs():
    for path in (
        "README.md",
        "docs/architecture.md",
        "docs/examples.md",
        "docs/monitoring.md",
        "docs/compliance.md",
    ):
        text = _read(path)
        assert "DICTIONARY_SUBSTITUTIONS_ENABLED" in text, path
        assert "dictionary-substitutions.default.json" in text, path
