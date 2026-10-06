"""Keep the spaCy experiment's expectations independent of its results."""

import pytest

from presidio.entity_types import SUPPORTED_ENTITY_TYPES
from presidio.evaluation.spacy_ablation import (
    CONTEXT_CASES,
    ContextCase,
    check_context_case,
)


def test_ablation_covers_every_public_entity_type():
    assert {
        case.entity_type for case in CONTEXT_CASES if case.positive
    } == SUPPORTED_ENTITY_TYPES
    assert len({case.case_id for case in CONTEXT_CASES}) == len(CONTEXT_CASES)
    assert all(case.value in case.text for case in CONTEXT_CASES)


@pytest.mark.parametrize("case", CONTEXT_CASES, ids=lambda case: case.case_id)
def test_missing_entity_never_passes_a_positive_case(case):
    assert check_context_case(case, [])["passed"] is (not case.positive)


def test_other_type_can_mask_but_cannot_satisfy_typed_expectation():
    case = ContextCase("example", "КПП: 770801001", "RU_KPP", "770801001")
    result = {"entity_type": "ORGANIZATION", "start": 5, "end": 14}
    row = check_context_case(case, [result])
    assert row["masking_covered"]
    assert not row["typed_covered"]
    assert not row["passed"]


def test_partial_value_does_not_count_as_covered():
    case = ContextCase("example", "Адрес: ул Ленина 10", "RU_ADDRESS", "ул Ленина 10")
    result = {"entity_type": "RU_ADDRESS", "start": 7, "end": 16}
    assert not check_context_case(case, [result])["passed"]


def test_partial_false_positive_still_fails_a_negative_case():
    case = ContextCase("example", "Номер 770801001", "RU_KPP", "770801001", False)
    result = {"entity_type": "RU_KPP", "start": 6, "end": 10}
    assert not check_context_case(case, [result])["passed"]
