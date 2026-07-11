"""Unit tests for reversible dictionary substitution policy."""

import json
from pathlib import Path

import pytest

from litellm_guardrails.dictionary_policy import (
    DictionaryPolicyAmbiguousRequestError,
    DictionaryPolicyConfigError,
    DictionarySubstitutionPolicy,
)


def _policy(*rules):
    return DictionarySubstitutionPolicy.from_config({"substitutions": list(rules)})


def test_case_insensitive_russian_phrase_matching_and_restore_mapping():
    policy = _policy(
        {
            "id": "tbank_to_zetta",
            "source": "Т-Банк",
            "replacement": "Зетта Групп",
            "match": {"case_sensitive": False, "whole_phrase": True},
            "restore": True,
        }
    )

    result = policy.apply("Проверь т-банк по договору")

    assert result.text == "Проверь Зетта Групп по договору"
    assert result.mapping == {"Зетта Групп": "т-банк"}
    assert result.rule_counts == {"tbank_to_zetta": 1}


def test_longest_match_first_keeps_overlapping_rules_deterministic():
    policy = _policy(
        {
            "id": "short",
            "source": "Т-Банк",
            "replacement": "Зетта Групп",
        },
        {
            "id": "long",
            "source": "Т-Банк Инвестиции",
            "replacement": "Зетта Инвест",
        },
    )

    result = policy.apply("Открой Т-Банк Инвестиции")

    assert result.text == "Открой Зетта Инвест"
    assert result.mapping == {"Зетта Инвест": "Т-Банк Инвестиции"}
    assert result.rule_counts == {"long": 1}


def test_duplicate_ids_are_rejected():
    with pytest.raises(DictionaryPolicyConfigError, match="duplicate"):
        _policy(
            {"id": "same", "source": "Сбербанк", "replacement": "Северный Траст"},
            {"id": "same", "source": "ВТБ", "replacement": "Восточный Контур"},
        )


def test_duplicate_restore_replacements_are_rejected():
    with pytest.raises(DictionaryPolicyConfigError, match="duplicate restore"):
        _policy(
            {"id": "one", "source": "Сбербанк", "replacement": "Северный Траст"},
            {"id": "two", "source": "ВТБ", "replacement": "Северный Траст"},
        )


def test_original_replacement_text_in_request_is_rejected_as_ambiguous():
    policy = _policy(
        {
            "id": "tbank_to_zetta",
            "source": "Т-Банк",
            "replacement": "Зетта Групп",
        }
    )

    with pytest.raises(DictionaryPolicyAmbiguousRequestError):
        policy.apply("Сравни Т-Банк и Зетта Групп")


def test_default_dictionary_contains_ten_unique_enabled_bank_rules():
    config_path = (
        Path(__file__).resolve().parents[1] / "dictionary-substitutions.default.json"
    )
    config = json.loads(config_path.read_text(encoding="utf-8"))
    rules = config["substitutions"]

    assert len(rules) == 10
    assert all(rule["enabled"] is True for rule in rules)
    assert all(rule["restore"] is True for rule in rules)
    assert {rule["source"] for rule in rules} == {
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
    }
    assert len({rule["replacement"] for rule in rules}) == 10

    policy = DictionarySubstitutionPolicy.from_config(config)
    assert len(policy.rules) == 10
