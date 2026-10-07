"""Narrow context refinements of accepted NER spans, not new detections."""

import itertools

import pytest
from presidio_analyzer import RecognizerResult

from presidio import analyzer_server
from result_merging import DETECTION_SOURCE_METADATA_KEY, SOURCE_NER, merge_results


def result(text, value, entity_type="ORGANIZATION", score=0.8, source=SOURCE_NER):
    start = text.index(value)
    return RecognizerResult(
        entity_type,
        start,
        start + len(value),
        score,
        recognition_metadata={DETECTION_SOURCE_METADATA_KEY: source},
    )


def summary(text, outcome):
    return [(r.entity_type, text[r.start : r.end], r.score) for r in outcome.results]


@pytest.mark.parametrize("form", ["ООО", "АО", "ПАО", "ОАО", "ЗАО"])
def test_legal_form_joins_only_already_recognized_name(form):
    text = f"Клиент {form} Иван Петров подтвердил заявку."
    prefix = result(text, form, score=0.9)
    name = result(text, "Иван Петров", "PERSON", 0.7)
    for order in itertools.permutations([prefix, name]):
        outcome = merge_results(text, order, requested_entities=["ORGANIZATION"])
        assert summary(text, outcome) == [("ORGANIZATION", form + " Иван Петров", 0.7)]
        assert outcome.results[0].recognition_metadata == prefix.recognition_metadata
    assert not merge_results(
        text, [prefix, name], requested_entities=["PERSON"]
    ).results


@pytest.mark.parametrize("gap", [", ", "; ", " и ", ". ", "\nПолучатель: ", "\n", " " * 17])
def test_legal_form_does_not_bridge_lists_sentences_or_unrecognized_letters(gap):
    text = "ООО" + gap + "Иван Петров"
    outcome = merge_results(
        text, [result(text, "ООО"), result(text, "Иван Петров", "PERSON")]
    )
    assert len(outcome.results) == 2


def test_legal_join_preserves_conflicting_credential():
    text = "ООО Иван Петров"
    results = [
        result(text, "ООО"),
        result(text, "Иван Петров", "PERSON"),
        result(text, "Петров", "LOGIN", source="native_credential"),
    ]
    outcome = merge_results(text, results)
    assert ("ORGANIZATION", "ООО", 0.8) in summary(text, outcome)
    assert ("LOGIN", "Петров", 0.8) in summary(text, outcome)
    assert "name_legal_form_join" not in [d.reason for d in outcome.decisions]


def test_outer_chunk_duplicates_do_not_prevent_join():
    text = "ООО Иван Петров"
    outcome = merge_results(
        text,
        [
            result(text, "ООО", score=0.7),
            result(text, "ООО"),
            result(text, "Иван Петров", "PERSON", 0.9),
        ],
    )
    assert summary(text, outcome) == [("ORGANIZATION", text, 0.8)]


@pytest.mark.parametrize("field", ["ФИО:", "Ф.И.О. ="])
def test_explicit_person_field_refines_type_before_requested_filter(field):
    text = field + " ИВАН ПЕТРОВ."
    for entity_type in ["PERSON", "ORGANIZATION"]:
        outcome = merge_results(
            text, [result(text, "ИВАН ПЕТРОВ")], requested_entities=[entity_type]
        )
        assert summary(text, outcome) == (
            [("PERSON", "ИВАН ПЕТРОВ", 0.8)] if entity_type == "PERSON" else []
        )


@pytest.mark.parametrize(
    "text,value",
    [
        ("Клиент ИВАН ПЕТРОВ", "ИВАН ПЕТРОВ"),
        ("Название организации: ИВАН ПЕТРОВ", "ИВАН ПЕТРОВ"),
        ("ФИО: ООО Вектор", "ООО Вектор"),
        ("Поля ФИО и ИВАН ПЕТРОВ", "ИВАН ПЕТРОВ"),
    ],
)
def test_ambiguous_context_does_not_reclassify(text, value):
    assert summary(text, merge_results(text, [result(text, value)])) == [
        ("ORGANIZATION", value, 0.8)
    ]


@pytest.mark.parametrize("command", ["Верни", "Покажи", "Сохрани"])
def test_instruction_tail_requires_sentence_boundary_and_dictionary_evidence(command):
    text = f"Компания ООО Вектор. {command} ровно MODEL_OK."
    entity = result(text, f"ООО Вектор. {command}")
    artifacts = analyzer_server.nlp_engine.process_text(text, "ru")
    starts = [t.idx for t in artifacts.tokens if t.is_sent_start]
    assert summary(text, merge_results(text, [entity], sentence_starts=starts)) == [
        ("ORGANIZATION", "ООО Вектор", 0.8)
    ]
    assert summary(text, merge_results(text, [entity])) == [
        ("ORGANIZATION", f"ООО Вектор. {command}", 0.8)
    ]


@pytest.mark.parametrize(
    "tail", ["Иван", "Север", "Петров", "Верни письмо", "И.", "Ltd"]
)
def test_non_imperative_or_multiword_tail_is_not_removed(tail):
    text = "ООО Вектор. " + tail
    entity = result(text, text)
    outcome = merge_results(text, [entity], sentence_starts=[len("ООО Вектор. ")])
    assert summary(text, outcome) == [("ORGANIZATION", text, 0.8)]


def test_independent_sensitive_result_in_instruction_tail_vetoes_trim():
    text = "ООО Вектор. Верни"
    outcome = merge_results(
        text,
        [
            result(text, text),
            result(text, "Верни", "LOGIN", source="native_credential"),
        ],
        sentence_starts=[len("ООО Вектор. ")],
    )
    assert "name_instruction_tail" not in [d.reason for d in outcome.decisions]


@pytest.mark.parametrize(
    "field", ["Номер заявки", "Номер заказа:", "Идентификатор строки ="]
)
def test_service_number_suppresses_only_ner_name_fragment(field):
    text = field + " 12345678 принят в обработку."
    assert not merge_results(text, [result(text, "1234")]).results
    assert summary(
        text, merge_results(text, [result(text, "1234", source="structural")])
    ) == [("ORGANIZATION", "1234", 0.8)]


@pytest.mark.parametrize(
    "text,value",
    [
        ("Компания ООО 1234", "1234"),
        ("Верни 12345678", "1234"),
        ("Номер заявки: ООО 1234", "1234"),
    ],
)
def test_numeric_fragment_without_exact_service_context_is_preserved(text, value):
    assert summary(text, merge_results(text, [result(text, value)])) == [
        ("ORGANIZATION", value, 0.8)
    ]


def test_contract_and_credential_are_not_name_refinement_targets():
    text = "Номер заявки 12345678. Договор № 87654321."
    outcome = merge_results(
        text,
        [
            result(text, "12345678", "AUTH_TOKEN", source="native_credential"),
            result(text, "87654321", "CONTRACT_NUMBER"),
        ],
    )
    assert {r.entity_type for r in outcome.results} == {"AUTH_TOKEN", "CONTRACT_NUMBER"}
    assert "name_service_number_suppressed" not in [d.reason for d in outcome.decisions]


def test_non_ner_person_or_organization_is_unchanged():
    text = "ФИО: ИВАН ПЕТРОВ"
    assert summary(
        text, merge_results(text, [result(text, "ИВАН ПЕТРОВ", source="structural")])
    ) == [("ORGANIZATION", "ИВАН ПЕТРОВ", 0.8)]


def test_generator_filter_is_not_consumed_by_contract_check():
    text = "ФИО: ИВАН ПЕТРОВ"
    outcome = merge_results(
        text, [result(text, "ИВАН ПЕТРОВ")],
        requested_entities=(t for t in ["PERSON"]),
    )
    assert summary(text, outcome) == [("PERSON", "ИВАН ПЕТРОВ", 0.8)]


def test_numeric_lookbehind_is_bounded_and_conservative():
    text = "Номер заявки " + "1" * 10000
    entity = RecognizerResult(
        "ORGANIZATION", len(text) - 4, len(text), 0.8,
        recognition_metadata={DETECTION_SOURCE_METADATA_KEY: SOURCE_NER},
    )
    outcome = merge_results(text, [entity])
    assert summary(text, outcome) == [("ORGANIZATION", "1111", 0.8)]
    assert "name_service_number_suppressed" not in [d.reason for d in outcome.decisions]
