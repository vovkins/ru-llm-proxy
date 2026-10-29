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

    # The higher score owns the overlap; the rest of the lower-score model
    # value stays masked instead of reaching the provider.
    assert _summary(merge_results(text, [low, high])) == [
        ("PERSON", 0, 4, 0.9),
        ("ORGANIZATION", 5, len(text), 0.6),
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

    assert _summary(merge_results(text, [low_token, high_person])) == [
        ("PERSON", 0, 4, 0.95),
        ("AUTH_TOKEN", 5, len(text), 0.4),
    ]


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
        ('{"contract_id": "AB-2024/17"}', "AB-2024/17"),
        ("contract_number = 'CN-88-2026'", "CN-88-2026"),
        ('номер_договора = "77-И/2025"', "77-И/2025"),
        ("client_contract_no=DG-2026-0042", "DG-2026-0042"),
        ("contractNumber: DG-2026-0042", "DG-2026-0042"),
        ("agreement_ref: AG/2026/7781", "AG/2026/7781"),
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
        ("contractor_id: AB-2024/17", "AB-2024/17"),
        ("build_id: AB-2024/17", "AB-2024/17"),
        ("contractNotes: AB-2024/17", "AB-2024/17"),
        ("get_txs(contract_address, start_block=27619770)", "27619770"),
        ('hashes(API_KEY, TOKEN_CONTRACT, "9f3Kq77Lm2Xp")', "9f3Kq77Lm2Xp"),
        ("CONTRACT_NUMBER, один проход, LR 1e-6 и epoch-4", "epoch-4"),
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


def test_model_value_keeps_the_part_a_spacy_location_does_not_cover():
    text = "Офис: г. Тверь, ул. Советская"
    spacy_city = _result(
        "LOCATION", text.index("Тверь"), text.index(","), 0.85,
        recognizer_name="SpacyRecognizer",
    )
    ner_location = _result(
        "LOCATION", text.index("г."), len(text), 0.9, SOURCE_NER
    )

    outcome = merge_results(text, [spacy_city, ner_location])

    # "г." and spaCy's "Тверь" are then joined across the space; every
    # character of the model value stays covered.
    assert _summary(outcome) == [
        ("LOCATION", text.index("г."), text.index(","), 0.9),
        ("LOCATION", text.index(","), len(text), 0.9),
    ]
    assert [decision.reason for decision in outcome.decisions] == [
        "overlap_preferred_source",
        "overlap_fragment_kept",
        "overlap_fragment_kept",
        "name_fragments_joined",
    ]


def test_deterministic_winner_keeps_its_boundaries_against_a_model_value():
    text = "token=SyntheticToken42 for deploy"
    value = "SyntheticToken42"
    native = _result(
        "AUTH_TOKEN", text.index(value), text.index(value) + len(value), 0.7,
        SOURCE_NATIVE_CREDENTIAL,
    )
    wider_ner = _result("AUTH_TOKEN", 0, len(text), 0.99, SOURCE_NER)

    outcome = merge_results(text, [wider_ner, native])
    assert _summary(outcome) == [
        ("AUTH_TOKEN", 0, text.index(value), 0.99),
        ("AUTH_TOKEN", text.index(value), text.index(value) + len(value), 0.7),
        ("AUTH_TOKEN", text.index("for deploy"), len(text), 0.99),
    ]
    assert outcome.results[1] is native


def test_additive_address_rule_completes_a_partial_native_address():
    text = "Доставка: ул. Ленина, д. 10, кв. 5"
    native = _result(
        "RU_ADDRESS", text.index("ул."), text.index(", кв."), 0.85,
        recognizer_name="RuAddressRecognizer",
    )
    chain = _result(
        "RU_ADDRESS", text.index("ул."), len(text), 0.75,
        recognizer_name="RuAddressChainRecognizer",
    )

    assert _summary(merge_results(text, [native, chain])) == [
        ("RU_ADDRESS", text.index("ул."), text.index(", кв."), 0.85),
        ("RU_ADDRESS", text.index(", кв."), len(text), 0.75),
    ]


@pytest.mark.parametrize(
    ("text", "value"),
    [
        ('__all__ = ["UniSpeechSatForCTC"]', "Uni"),
        ('__all__ = ["UniSpeechSatForCTC"]', "UniSpeechSatForC"),
        ("service = user_service_factory()", "service_fact"),
    ],
)
def test_model_organization_cutting_a_code_identifier_is_suppressed(text, value):
    start = text.index(value)
    ner = _result("ORGANIZATION", start, start + len(value), 0.9, SOURCE_NER)

    outcome = merge_results(text, [ner])

    assert outcome.results == ()
    assert [d.reason for d in outcome.decisions] == ["ner_identifier_fragment_suppressed"]


@pytest.mark.parametrize(
    ("text", "value", "entity_type"),
    [
        ("# Copyright 2024 The HuggingFace Inc. team.", "HuggingFace Inc", "ORGANIZATION"),
        ("author: BeerKate", "BeerKate", "ORGANIZATION"),
        ("name: Name for the TensorFlow ops.", "TensorF", "ORGANIZATION"),
        ('"Qwen2ForCausalLM",', "alLM", "ORGANIZATION"),
        ("Договор с ООО «РомашкаСервис» подписан", "РомашкаСер", "ORGANIZATION"),
        ("Smiths family", "Smith", "ORGANIZATION"),
        ("class PetrovReport:", "Petrov", "PERSON"),
    ],
)
def test_organizations_without_code_proof_and_other_types_are_preserved(text, value, entity_type):
    start = text.index(value)
    ner = _result(entity_type, start, start + len(value), 0.9, SOURCE_NER)

    assert _summary(merge_results(text, [ner])) == [
        (entity_type, start, start + len(value), 0.9)
    ]


def test_model_password_keeps_the_tail_a_native_value_cut_at_an_ampersand():
    text = "REDIS_PASS=i$N+=S&84dOvw^euNCcwvq9r"
    start = text.index("i$N")
    native = _result("PASSWORD", start, text.index("&"), 0.9, SOURCE_NATIVE_CREDENTIAL)
    ner = _result("PASSWORD", start, len(text), 0.95, SOURCE_NER)

    assert _summary(merge_results(text, [native, ner])) == [
        ("PASSWORD", start, text.index("&"), 0.9),
        ("PASSWORD", text.index("&"), len(text), 0.95),
    ]


def test_name_before_email_keeps_the_frozen_k05_ner_coverage():
    text = "Михаил Соколов mikhail.sokolov@outlook.com не могу войти в личный кабинет"
    ner = _result("PERSON", 0, 22, 0.5756028294563293, SOURCE_NER)
    email = _result("EMAIL_ADDRESS", 15, 42, 0.85, SOURCE_STRUCTURAL)

    for order in itertools.permutations([ner, email]):
        outcome = merge_results(text, order)

        assert _summary(outcome) == [
            ("PERSON", 0, 14, ner.score),
            ("EMAIL_ADDRESS", 15, 42, email.score),
        ]
        assert outcome.results[0].recognition_metadata == ner.recognition_metadata
        assert outcome.results[1] is email


@pytest.mark.parametrize(
    "ner_bounds,winner_bounds",
    [
        ((2, 18), [(6, 10)]),
        ((6, 18), [(2, 10)]),
        ((2, 10), [(6, 18)]),
        ((2, 22), [(4, 8), (12, 16)]),
        ((4, 12), [(2, 16)]),
        ((4, 12), [(4, 12)]),
        ((2, 22), [(4, 8), (8, 12), (16, 20)]),
    ],
)
@pytest.mark.parametrize("source", [SOURCE_NATIVE_CREDENTIAL, SOURCE_STRUCTURAL, SOURCE_NER])
def test_overlapping_ner_retains_all_uncovered_characters(ner_bounds, winner_bounds, source):
    text = "abcdefghijklmnopqrstuvwxyz"
    ner = _result("PASSWORD", *ner_bounds, 0.8, SOURCE_NER)
    winners = [_result("AUTH_TOKEN", start, end, 0.99, source, "winner")
               for start, end in winner_bounds]
    expected_coverage = {i for r in [ner, *winners] for i in range(r.start, r.end)}
    expected = None

    for order in itertools.permutations([ner, *winners]):
        outcome = merge_results(text, order)
        assert {i for r in outcome.results for i in range(r.start, r.end)} == expected_coverage
        assert all(a.end <= b.start for a, b in zip(outcome.results, outcome.results[1:]))
        assert all(any(r is winner for r in outcome.results) for winner in winners)
        assert all(r.recognition_metadata == ner.recognition_metadata and r.score == ner.score
                   for r in outcome.results if not any(r is winner for winner in winners))
        if expected is None:
            expected = _summary(outcome)
        assert _summary(outcome) == expected


@pytest.mark.parametrize("separator", [" ", "\t", "\n", " \t "])
def test_ner_remainder_preserves_whole_words_but_trims_edge_whitespace(separator):
    text = separator + "label=" + separator + "Alpha42" + separator + "ordinary text" + separator
    start = text.index("Alpha42")
    native = _result("PASSWORD", start, start + len("Alpha42"), 0.9, SOURCE_NATIVE_CREDENTIAL)
    ner = _result("PASSWORD", 0, len(text), 0.8, SOURCE_NER)

    outcome = merge_results(text, [ner, native])

    assert [text[r.start:r.end] for r in outcome.results] == ["label=", "Alpha42", "ordinary text"]
    assert all(text[i].isspace() or any(r.start <= i < r.end for r in outcome.results)
               for i in range(len(text)))


@pytest.mark.parametrize("label,entity_type", [("Email:", "PERSON"), ("Bearer", "AUTH_TOKEN")])
def test_overlapped_model_field_labels_remain_masked(label, entity_type):
    text = label + " jane@example.test"
    start = text.index("jane")
    ner = _result(entity_type, 0, start + len("jane"), 0.9, SOURCE_NER)
    email = _result("EMAIL_ADDRESS", start, len(text), 0.85, SOURCE_STRUCTURAL)

    outcome = merge_results(text, [ner, email])

    assert [text[r.start:r.end] for r in outcome.results] == [label, "jane@example.test"]
    assert outcome.results[0].entity_type == entity_type
    assert outcome.results[1] is email


def test_excluded_ner_class_does_not_reappear_as_an_overlap_remainder():
    text = "Email: jane@example.test"
    ner = _result("PERSON", 0, 11, 0.9, SOURCE_NER)
    email = _result("EMAIL_ADDRESS", 7, len(text), 0.85, SOURCE_STRUCTURAL)

    assert merge_results(text, [ner, email], requested_entities=["EMAIL_ADDRESS"]).results == (email,)


def test_wider_ner_keeps_left_label_and_entire_detected_tail():
    text = "token=SyntheticToken42&x2 for deploy"
    value = "SyntheticToken42"
    start = text.index(value)
    native = _result("AUTH_TOKEN", start, start + len(value), 0.7, SOURCE_NATIVE_CREDENTIAL)
    wider_ner = _result("AUTH_TOKEN", 0, len(text), 0.99, SOURCE_NER)

    assert _summary(merge_results(text, [wider_ner, native])) == [
        ("AUTH_TOKEN", 0, start, 0.99),
        ("AUTH_TOKEN", start, start + len(value), 0.7),
        ("AUTH_TOKEN", start + len(value), len(text), 0.99),
    ]


@pytest.mark.parametrize(
    ("text", "pieces", "value"),
    [
        ("Москва - Гавана - Каракас", ("Москва", "Гавана - Каракас"), "Москва - Гавана - Каракас"),
        ("Rambler - Новости", ("Rambler", "Новости"), "Rambler - Новости"),
        ("ООО «Лаборатория Север» сообщило", ("ООО", "«Лаборатория Север"), "ООО «Лаборатория Север»"),
    ],
)
def test_same_type_name_fragments_are_joined(text, pieces, value):
    results = [
        _result("LOCATION" if "Москва" in text else "ORGANIZATION",
                text.index(piece), text.index(piece) + len(piece), 0.9, SOURCE_NER)
        for piece in pieces
    ]

    outcome = merge_results(text, results)

    assert [(r.start, r.end) for r in outcome.results] == [
        (text.index(value), text.index(value) + len(value))
    ]


def test_names_of_different_types_or_far_apart_are_not_joined():
    text = "Иван, Москва и Тверь"
    person = _result("PERSON", 0, 4, 0.9, SOURCE_NER)
    city = _result("LOCATION", 6, 12, 0.9, SOURCE_NER)
    other = _result("LOCATION", text.index("Тверь"), len(text), 0.9, SOURCE_NER)

    assert len(merge_results(text, [person, city, other]).results) == 3


def test_name_start_moves_off_a_backslash_escape_letter():
    text = '{"text": "Привет\\nСервис Плюс"}'
    start = text.index("nСервис")
    ner = _result("ORGANIZATION", start, text.index('"}'), 0.9, SOURCE_NER)
    escape_only = _result("PERSON", start, start + 1, 0.9, SOURCE_NER)

    assert _summary(merge_results(text, [ner, escape_only])) == [
        ("ORGANIZATION", start + 1, text.index('"}'), 0.9)
    ]


def test_organization_piece_of_a_split_secret_stays_masked():
    text = "token: DHYdN9XeCevnykcwVsxZg"
    start = text.index("DH")
    pieces = [
        _result("SECRET_KEY", start, start + 2, 0.9, SOURCE_NER),
        _result("ORGANIZATION", start + 2, start + 6, 0.5, SOURCE_NER),
        _result("SECRET_KEY", start + 6, len(text), 0.9, SOURCE_NER),
    ]

    outcome = merge_results(text, pieces)

    covered = set()
    for result in outcome.results:
        covered.update(range(result.start, result.end))
    assert set(range(start, len(text))) <= covered


def _word_piece_result(entity_type, start, end, score):
    return _result(entity_type, start, end, score, SOURCE_NER)


@pytest.mark.parametrize("text,value,kind", [
    ("Клиент John Smith оставил заявку", "John Smi", "PERSON"),
    ("Компания Northwind Research работает", "Northwind Rese", "ORGANIZATION"),
    ("Город New York указан", "New Yor", "LOCATION"),
])
def test_partial_latin_names_are_not_discarded_without_context_evidence(text, value, kind):
    start = text.index(value)
    outcome = merge_results(text, [_word_piece_result(kind, start, start + len(value), .9)])
    assert any(r.start <= start and r.end >= start + len(value) for r in outcome.results)


@pytest.mark.parametrize(
    ("text", "value"),
    [
        ("Документ подписал Петров И. С. вчера.", "Петров И. С."),
        ("Документ подписал Петров И.С. вчера.", "Петров И.С."),
        ('1. Лутц М., "Изучаем Python"', "Лутц М."),
    ],
)
def test_model_surname_takes_the_initials_right_after_it(text, value):
    start = text.index(value)
    surname = _result("PERSON", start, start + value.index(" "), 0.99, SOURCE_NER)

    assert _summary(merge_results(text, [surname])) == [
        ("PERSON", start, start + len(value), 0.99)
    ]


def test_initials_are_not_taken_from_another_result_or_a_word():
    text = "Петров И. Сидоров"
    surname = _result("PERSON", 0, 6, 0.99, SOURCE_NER)
    assert _summary(merge_results(text, [surname])) == [("PERSON", 0, 9, 0.99)]

    text = "Петров Иван"
    assert _summary(merge_results(text, [_result("PERSON", 0, 6, 0.99, SOURCE_NER)])) == [
        ("PERSON", 0, 6, 0.99)
    ]


@pytest.mark.parametrize(
    ("text", "pieces"),
    [
        ("Ф.И.О.:", [(0, 2)]),
        ("Ф.И.О.: не заполнено.", [(0, 2)]),
        ("ФИО: пока не указано.", [(0, 3)]),
    ],
)
def test_model_person_inside_a_name_field_label_is_suppressed(text, pieces):
    results = [_result("PERSON", start, end, 0.97, SOURCE_NER) for start, end in pieces]

    assert merge_results(text, results).results == ()


def test_name_after_a_field_label_is_kept():
    text = "Ф.И.О.: Петров Иван Сергеевич"
    label_piece = _result("PERSON", 0, 4, 0.77, SOURCE_NER)
    dot = _result("PERSON", 5, 6, 0.98, SOURCE_NER)
    name = _result("PERSON", 8, len(text), 0.99, SOURCE_NER)

    assert _summary(merge_results(text, [label_piece, dot, name])) == [
        ("PERSON", 8, len(text), 0.99)
    ]


@pytest.mark.parametrize(
    ("text", "value", "entity_type"),
    [
        ("user: which file contains the implementation?\nassistant: Done.", "Don", "PERSON"),
        ("Поля ФИО и ORGANIZATION описаны в инструкции.", "ORGANIZA", "ORGANIZATION"),
    ],
)
def test_word_piece_name_is_not_suppressed_without_context(text, value, entity_type):
    start = text.index(value)
    piece = _word_piece_result(entity_type, start, start + len(value), 0.8)

    assert merge_results(text, [piece]).results == (piece,)


def test_word_piece_name_is_kept_on_its_own_or_next_to_a_secret():
    text = "assistant: Done."
    start = text.index("Don")
    bert_piece = _result("PERSON", start, start + 3, 0.8, SOURCE_NER)
    assert len(merge_results(text, [bert_piece]).results) == 1

    text = "token: ci_test_97"
    login = _result("LOGIN", text.index("i_test"), len(text), 0.9, SOURCE_NER)
    piece = _word_piece_result("PERSON", text.index("ci"), text.index("ci") + 1, 0.6)
    covered = set()
    for result in merge_results(text, [login, piece]).results:
        covered.update(range(result.start, result.end))
    assert set(range(text.index("ci"), len(text))) <= covered


@pytest.mark.parametrize(
    ("text", "value", "expected"),
    [
        ("Компания ООО Вектор. Иван Петров получил письмо.", "ООО Вектор.", "ООО Вектор"),
        ("ООО Вектор; Иван Петров подписал документ.", "ООО Вектор;", "ООО Вектор"),
        ("Организация Northwind Research Ltd. получила письмо.", "Организация Northwind Research Ltd",
         "Northwind Research Ltd"),
        ("Договор с ООО Вектор И.", "ООО Вектор И.", "ООО Вектор И."),
    ],
)
def test_model_organization_drops_closing_punctuation_and_generic_noun(text, value, expected):
    start = text.index(value)
    organization = _result("ORGANIZATION", start, start + len(value), 0.9, SOURCE_NER)

    assert [(text[r.start:r.end]) for r in merge_results(text, [organization]).results] == [expected]


def test_instruction_tail_cut_inside_the_word_is_removed_at_a_sentence_start():
    text = "Компания ООО Вектор. Верни ровно MODEL_OK."
    value = "ООО Вектор. Верн"
    start = text.index(value)
    organization = _result("ORGANIZATION", start, start + len(value), 0.9, SOURCE_NER)

    outcome = merge_results(text, [organization], sentence_starts=[0, text.index("Верни")])

    assert [(text[r.start:r.end]) for r in outcome.results] == ["ООО Вектор"]


@pytest.mark.parametrize(("text", "value"), [("Новости Yahoo ! за день", "Yahoo !"),
                                              ("Газета Городские вести . Выпуск", "Городские вести ."),
                                              ("Сервис Yahoo! работает", "Yahoo!")])
def test_model_organization_keeps_spaced_or_brand_punctuation(text, value):
    start = text.index(value)
    organization = _result("ORGANIZATION", start, start + len(value), 0.9, SOURCE_NER)

    assert [(text[r.start:r.end]) for r in merge_results(text, [organization]).results] == [value]


@pytest.mark.parametrize("separator", [" ", "\t", "\n", " \t "])
@pytest.mark.parametrize(
    "entity_type,recognizer_name",
    [("RU_ADDRESS", "RuAddressChainRecognizer"),
     ("RU_PASSPORT", "RuPassportExtRecognizer")],
)
def test_shortened_ner_fragment_does_not_hide_a_later_additive_remainder(
    separator, entity_type, recognizer_name,
):
    # Controlled recognizer outputs: the test does not claim BERT predicts
    # these boundaries. A virtual coverage bit must correspond to a real mask.
    text = "address=Alpha42&tail" + separator + "ул. Ленина, д. 5"
    start = text.index("Alpha42")
    native = _result("PASSWORD", start, start + len("Alpha42"), .9, SOURCE_NATIVE_CREDENTIAL)
    wide = _result("PASSWORD", 0, len(text), .8, SOURCE_NER)
    additive = _result(entity_type, start, len(text), .75,
                       SOURCE_STRUCTURAL, recognizer_name)
    expected = None
    for order in itertools.permutations([native, wide, additive]):
        outcome = merge_results(text, order)
        spans = outcome.results
        covered = {i for r in spans for i in range(r.start, r.end)}
        assert all(i in covered for i in range(text.index("ул."), len(text)))
        assert next(r for r in spans if r.start == start) is native
        assert all(a.end <= b.start for a, b in zip(spans, spans[1:]))
        summary = _summary(outcome)
        if expected is None:
            expected = summary
        assert summary == expected


def test_nested_additive_and_ner_losers_complete_their_combined_ranges():
    text = "password=Alpha42&tail middle final visible"
    start = text.index("Alpha42")
    native = _result("PASSWORD", start, start + len("Alpha42"), .9, SOURCE_NATIVE_CREDENTIAL)
    wide = _result("PASSWORD", 0, len(text), .8, SOURCE_NER)
    address = _result("RU_ADDRESS", start, text.index(" final"), .75,
                      SOURCE_STRUCTURAL, "RuAddressChainRecognizer")
    passport = _result("RU_PASSPORT", start + 1, text.index(" visible"), .7,
                       SOURCE_STRUCTURAL, "RuPassportExtRecognizer")
    expected = None
    for order in itertools.permutations([native, wide, address, passport]):
        outcome = merge_results(text, order)
        spans = outcome.results
        covered = {i for r in spans for i in range(r.start, r.end)}
        assert all(text[i].isspace() or i in covered
                   for i in range(len(text)))
        assert native in spans
        assert all(a.end <= b.start for a, b in zip(spans, spans[1:]))
        if expected is None:
            expected = _summary(outcome)
        assert _summary(outcome) == expected


def test_ner_keeps_its_detected_following_text_without_an_additive_rule():
    text = "password=Alpha42&tail ordinary text"
    start = text.index("Alpha42")
    native = _result("PASSWORD", start, start + len("Alpha42"), .9, SOURCE_NATIVE_CREDENTIAL)
    wide = _result("PASSWORD", 0, len(text), .8, SOURCE_NER)
    for order in itertools.permutations([native, wide]):
        spans = merge_results(text, order).results
        assert [text[r.start:r.end] for r in spans] == [
            "password=", "Alpha42", "&tail ordinary text",
        ]


def test_two_exact_winners_and_additive_rule_keep_all_detected_ner_gaps():
    text = "key=Alpha42&tail middle Beta77&more final visible"
    first = text.index("Alpha42")
    second = text.index("Beta77")
    end = text.index(" visible")
    password = _result("PASSWORD", first, first + len("Alpha42"), .9, SOURCE_NATIVE_CREDENTIAL)
    secret = _result("SECRET_KEY", second, second + len("Beta77"), .9, SOURCE_NATIVE_CREDENTIAL)
    wide = _result("PASSWORD", 0, len(text), .8, SOURCE_NER)
    address = _result("RU_ADDRESS", first, end, .75,
                      SOURCE_STRUCTURAL, "RuAddressChainRecognizer")
    expected = None
    for order in itertools.permutations([password, secret, wide, address]):
        outcome = merge_results(text, order)
        spans = outcome.results
        covered = {i for r in spans for i in range(r.start, r.end)}
        assert all(text[i].isspace() or i in covered for i in range(len(text)))
        assert password in spans and secret in spans
        assert all(a.end <= b.start for a, b in zip(spans, spans[1:]))
        if expected is None:
            expected = _summary(outcome)
        assert _summary(outcome) == expected


def test_excluding_additive_entity_preserves_only_the_allowed_ner_class():
    text = "password=Alpha42&tail ул. Ленина, д. 5"
    start = text.index("Alpha42")
    native = _result("PASSWORD", start, start + len("Alpha42"), .9, SOURCE_NATIVE_CREDENTIAL)
    wide = _result("PASSWORD", 0, len(text), .8, SOURCE_NER)
    address = _result("RU_ADDRESS", start, len(text), .75,
                      SOURCE_STRUCTURAL, "RuAddressChainRecognizer")
    for order in itertools.permutations([native, wide, address]):
        spans = merge_results(text, order, requested_entities=["PASSWORD"]).results
        assert [text[r.start:r.end] for r in spans] == [
            "password=", "Alpha42", "&tail ул. Ленина, д. 5",
        ]
        assert all(r.entity_type == "PASSWORD" for r in spans)
