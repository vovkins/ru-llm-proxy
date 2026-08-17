"""Tests for source-aware Analyzer result merging."""

import itertools
import sys
from pathlib import Path

import pytest


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from presidio_analyzer import RecognizerResult  # noqa: E402

from result_merging import (  # noqa: E402
    CONTRACT_CONTEXT_SCORE,
    DETECTION_SOURCE_METADATA_KEY,
    SOURCE_NATIVE_CREDENTIAL,
    SOURCE_NER,
    SOURCE_CONTEXTUAL_CONTRACT,
    SOURCE_STRUCTURAL,
    contract_context_evidence,
    detection_source,
    merge_results,
)


def _result(entity_type, start, end, score, source=None, recognizer_name="test"):
    metadata = {RecognizerResult.RECOGNIZER_NAME_KEY: recognizer_name}
    if source is not None:
        metadata[DETECTION_SOURCE_METADATA_KEY] = source
    return RecognizerResult(
        entity_type=entity_type,
        start=start,
        end=end,
        score=score,
        recognition_metadata=metadata,
    )


def _summary(outcome):
    return [
        (result.entity_type, result.start, result.end, result.score)
        for result in outcome.results
    ]


def test_missing_source_metadata_is_classified_as_structural():
    result = _result("RU_INN", 0, 10, 0.4)

    assert detection_source(result) == SOURCE_STRUCTURAL


def test_exact_duplicate_keeps_higher_score_once():
    text = "abcdefghij"
    low = _result("LOGIN", 1, 5, 0.4, SOURCE_NATIVE_CREDENTIAL, "low")
    high = _result("LOGIN", 1, 5, 0.9, SOURCE_NATIVE_CREDENTIAL, "high")

    outcome = merge_results(text, [low, high])

    assert _summary(outcome) == [("LOGIN", 1, 5, 0.9)]
    assert [decision.reason for decision in outcome.decisions] == ["exact_duplicate"]


def test_native_value_beats_wider_bearer_result_regardless_of_score():
    text = "curl -H 'Authorization: Bearer SyntheticToken42'"
    value = "SyntheticToken42"
    start = text.index(value)
    native = _result(
        "AUTH_TOKEN",
        start,
        start + len(value),
        0.7,
        SOURCE_NATIVE_CREDENTIAL,
    )
    structural = _result(
        "BEARER_TOKEN",
        text.index("Authorization"),
        start + len(value),
        0.99,
        SOURCE_STRUCTURAL,
    )

    outcome = merge_results(text, [structural, native])

    assert _summary(outcome) == [
        ("AUTH_TOKEN", start, start + len(value), 0.7)
    ]
    assert outcome.decisions[0].reason == "overlap_preferred_source"


@pytest.mark.parametrize(
    "specific_type",
    ["PRIVATE_KEY", "DB_URL", "JWT", "API_KEY"],
)
def test_specialized_structural_type_beats_generic_native_type(specific_type):
    text = "0123456789abcdef"
    generic_type = "AUTH_TOKEN" if specific_type == "JWT" else "SECRET_KEY"
    structural = _result(
        specific_type,
        2,
        14,
        0.6,
        SOURCE_STRUCTURAL,
    )
    native = _result(
        generic_type,
        2,
        14,
        0.99,
        SOURCE_NATIVE_CREDENTIAL,
    )

    outcome = merge_results(text, [native, structural])

    assert [result.entity_type for result in outcome.results] == [specific_type]


def test_validated_structural_result_beats_overlapping_ner_result():
    text = "ИНН 7707083893"
    start = text.index("7707083893")
    inn = _result("RU_INN", start, len(text), 0.4, SOURCE_STRUCTURAL)
    ner = _result("ORGANIZATION", start, len(text), 0.99, SOURCE_NER)

    outcome = merge_results(text, [ner, inn])

    assert [result.entity_type for result in outcome.results] == ["RU_INN"]


def test_contextual_hostname_beats_overlapping_ner_login():
    text = "Хост k8s-controller-us1 в статусе NotReady."
    value = "k8s-controller-us1"
    start = text.index(value)
    hostname = _result(
        "HOSTNAME",
        start,
        start + len(value),
        0.75,
        SOURCE_STRUCTURAL,
    )
    ner_login = _result(
        "LOGIN",
        start,
        start + len(value),
        0.99,
        SOURCE_NER,
    )

    outcome = merge_results(text, [ner_login, hostname])

    assert [result.entity_type for result in outcome.results] == ["HOSTNAME"]
    assert outcome.decisions[0].reason == "overlap_preferred_source"


def test_ner_conflict_uses_score_then_stable_boundaries():
    text = "Иван Петров"
    low = _result("ORGANIZATION", 0, len(text), 0.6, SOURCE_NER)
    high = _result("PERSON", 0, 4, 0.9, SOURCE_NER)

    assert [result.entity_type for result in merge_results(text, [low, high]).results] == [
        "PERSON"
    ]

    left = _result("PERSON", 0, 4, 0.9, SOURCE_NER)
    right = _result("PERSON", 0, len(text), 0.9, SOURCE_NER)
    assert _summary(merge_results(text, [left, right])) == [
        ("PERSON", 0, len(text), 0.9)
    ]


def test_ner_score_precedes_entity_specificity():
    text = "Иван Петров"
    low_token = _result("AUTH_TOKEN", 0, len(text), 0.4, SOURCE_NER)
    high_person = _result("PERSON", 0, 4, 0.95, SOURCE_NER)

    assert [
        result.entity_type
        for result in merge_results(text, [low_token, high_person]).results
    ] == ["PERSON"]


def test_merge_is_stable_for_every_input_permutation():
    text = "Authorization: Bearer SyntheticToken42; ИНН 7707083893"
    token = "SyntheticToken42"
    token_start = text.index(token)
    inn_start = text.index("7707083893")
    results = [
        _result(
            "BEARER_TOKEN",
            0,
            token_start + len(token),
            0.99,
            SOURCE_STRUCTURAL,
        ),
        _result(
            "AUTH_TOKEN",
            token_start,
            token_start + len(token),
            0.8,
            SOURCE_NATIVE_CREDENTIAL,
        ),
        _result("RU_INN", inn_start, len(text), 0.4, SOURCE_STRUCTURAL),
    ]
    expected = _summary(merge_results(text, results))

    for permutation in itertools.permutations(results):
        assert _summary(merge_results(text, permutation)) == expected


def test_non_overlapping_results_are_all_preserved_in_source_order():
    text = "Иван, Москва, login=demo"
    results = [
        _result("LOGIN", 20, 24, 0.8, SOURCE_NATIVE_CREDENTIAL),
        _result("PERSON", 0, 4, 0.9, SOURCE_NER),
        _result("LOCATION", 6, 12, 0.9, SOURCE_NER),
    ]

    assert [result.entity_type for result in merge_results(text, results).results] == [
        "PERSON",
        "LOCATION",
        "LOGIN",
    ]


@pytest.mark.parametrize(
    "start, end",
    [(-1, 2), (2, 2), (3, 2), (0, 20)],
)
def test_invalid_spans_are_removed_without_inspecting_text(start, end):
    outcome = merge_results(
        "short",
        [_result("PERSON", start, end, 0.9, SOURCE_NER)],
    )

    assert outcome.results == ()
    assert outcome.decisions[0].reason == "invalid_span"


@pytest.mark.parametrize(
    "entity_type, text, value",
    [
        ("LOGIN", "login=example", "example"),
        ("PASSWORD", "password=changeme", "changeme"),
        ("AUTH_TOKEN", "AUTH_TOKEN=your-token", "your-token"),
        ("SECRET_KEY", "SECRET_KEY=placeholder", "placeholder"),
    ],
)
def test_inert_ner_credential_values_are_suppressed(entity_type, text, value):
    start = text.index(value)
    ner = _result(
        entity_type,
        start,
        start + len(value),
        0.99,
        SOURCE_NER,
    )

    outcome = merge_results(text, [ner])

    assert outcome.results == ()
    assert outcome.decisions[0].reason == "credential_placeholder_suppressed"


@pytest.mark.parametrize(
    "text, value",
    [
        ("Договор № AB-2026/0042 вступает в силу.", "AB-2026/0042"),
        ("Номер договора: 12345678.", "12345678"),
        ("Контракт 77-И/2025 зарегистрирован.", "77-И/2025"),
        ("Contract number CN-2026-0199 was approved.", "CN-2026-0199"),
        ("Agreement no. AG-2026-0044 was signed.", "AG-2026-0044"),
        ("Номер\u00a0договора из PDF: ДГ-2026\u00ad/117.", "ДГ-2026\u00ad/117"),
        ("Номер договора:\nML-2026/0091 указан в форме.", "ML-2026/0091"),
    ],
)
def test_contract_ner_result_requires_and_accepts_context_evidence(text, value):
    start = text.index(value)
    ner = _result(
        "CONTRACT_NUMBER",
        start,
        start + len(value),
        0.9,
        SOURCE_NER,
    )

    outcome = merge_results(text, [ner])

    assert _summary(outcome) == [
        ("CONTRACT_NUMBER", start, start + len(value), 0.9)
    ]
    assert detection_source(outcome.results[0]) == SOURCE_CONTEXTUAL_CONTRACT
    assert outcome.decisions[0].reason == "contract_context_confirmed"


def test_contract_context_confirms_repeated_numbers_in_same_sentence():
    text = "Договоры AA-101/26 и BB-202/26 связаны."
    results = []
    for value in ("AA-101/26", "BB-202/26"):
        start = text.index(value)
        results.append(
            _result(
                "CONTRACT_NUMBER",
                start,
                start + len(value),
                0.9,
                SOURCE_NER,
            )
        )

    assert len(merge_results(text, results).results) == 2


@pytest.mark.parametrize(
    "text, value",
    [
        ("Версия приложения AB-2026/0042 используется в тестах.", "AB-2026/0042"),
        ("Номер заявки 12345678 принят в обработку.", "12345678"),
        ("Случайный код 77-И/2025 указан в таблице.", "77-И/2025"),
        ("SECRET_KEY=synthetic-secret-2026", "synthetic-secret-2026"),
        ("Версия договора AB-2026/0042 используется в тестах.", "AB-2026/0042"),
        ("Шаблон договора DEMO-2026/01 опубликован.", "DEMO-2026/01"),
        ("Договор подписан 12.07.2026 сторонами.", "12.07.2026"),
        ("У договора версия 2.4.1 используется в тестах.", "2.4.1"),
    ],
)
def test_unconfirmed_contract_ner_result_is_suppressed(text, value):
    start = text.index(value)
    ner = _result(
        "CONTRACT_NUMBER",
        start,
        start + len(value),
        0.99,
        SOURCE_NER,
    )

    outcome = merge_results(text, [ner])

    assert outcome.results == ()
    assert outcome.decisions[0].reason == "contract_unconfirmed"


def test_contract_context_fallback_creates_entity_without_ner():
    text = "Договор № AB-2026/0042 вступает в силу."
    value = "AB-2026/0042"
    start = text.index(value)

    assert contract_context_evidence(text)
    outcome = merge_results(text, [])

    assert _summary(outcome) == [
        (
            "CONTRACT_NUMBER",
            start,
            start + len(value),
            CONTRACT_CONTEXT_SCORE,
        )
    ]
    assert outcome.decisions[0].reason == "contract_context_fallback"


def test_contract_context_fallback_relabels_overlapping_ner_entity():
    text = "Соглашение СГ-88/2026 подписано сторонами."
    value = "СГ-88/2026"
    start = text.index(value)
    wrong_ner_type = _result(
        "PERSON",
        start,
        start + len(value),
        0.99,
        SOURCE_NER,
    )

    outcome = merge_results(text, [wrong_ner_type])

    assert [result.entity_type for result in outcome.results] == [
        "CONTRACT_NUMBER"
    ]
    assert [decision.reason for decision in outcome.decisions] == [
        "contract_context_fallback",
        "overlap_preferred_source",
    ]


def test_contract_context_fallback_corrects_wider_ner_boundaries():
    text = "Agreement no. AG-2026-0044 was signed."
    value = "AG-2026-0044"
    start = text.index(value)
    wider_ner = _result(
        "CONTRACT_NUMBER",
        text.index("no."),
        start + len(value),
        0.99,
        SOURCE_NER,
    )

    outcome = merge_results(text, [wider_ner])

    assert _summary(outcome) == [
        (
            "CONTRACT_NUMBER",
            start,
            start + len(value),
            CONTRACT_CONTEXT_SCORE,
        )
    ]


def test_contract_context_fallback_respects_requested_entities():
    text = "Договор № AB-2026/0042 вступает в силу."

    outcome = merge_results(text, [], requested_entities=["PERSON"])

    assert outcome.results == ()
    assert outcome.decisions == ()


def test_contract_context_fallback_respects_score_threshold():
    text = "Договор № AB-2026/0042 вступает в силу."

    outcome = merge_results(
        text,
        [],
        score_threshold=CONTRACT_CONTEXT_SCORE + 0.01,
    )

    assert outcome.results == ()
    assert outcome.decisions == ()
