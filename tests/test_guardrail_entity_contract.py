"""Static checks for the public Analyzer entity contract."""

import re
from pathlib import Path

import yaml

from presidio.entity_types import (
    DETERMINISTIC_ENTITY_TYPES,
    NER_ENTITY_TYPES,
    SUPPORTED_ENTITY_TYPES,
)


ROOT = Path(__file__).resolve().parents[1]


def _pre_call_guardrail_info():
    config = yaml.safe_load(
        (ROOT / "litellm-config.yaml").read_text(encoding="utf-8")
    )
    guardrail = next(
        item
        for item in config["guardrails"]
        if item["guardrail_name"] == "ru-pii-mask-pre"
    )
    return guardrail["guardrail_info"]


def test_public_entity_contract_combines_deterministic_and_ner_types():
    assert SUPPORTED_ENTITY_TYPES == (
        DETERMINISTIC_ENTITY_TYPES | NER_ENTITY_TYPES
    )
    assert len(SUPPORTED_ENTITY_TYPES) == 29


def test_guardrail_metadata_lists_complete_entity_contract_once():
    info = _pre_call_guardrail_info()
    entities_param = next(
        item for item in info["params"] if item["name"] == "entities"
    )
    listed_types = re.findall(
        r"\b[A-Z][A-Z0-9_]+\b",
        entities_param["description"],
    )

    assert set(listed_types) == SUPPORTED_ENTITY_TYPES
    assert len(listed_types) == len(SUPPORTED_ENTITY_TYPES)


def test_guardrail_metadata_describes_all_sensitive_data_families():
    description = _pre_call_guardrail_info()["description"]

    for term in ("персональные данные", "реквизиты", "инфраструктуру", "секреты"):
        assert term in description
    assert "GET /guardrails/list" in description
