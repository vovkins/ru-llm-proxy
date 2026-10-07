"""Narrow context refinements of accepted NER spans, not new detections."""

import itertools

import pytest
from presidio_analyzer import RecognizerResult

from presidio import analyzer_server
from result_merging import (
    DETECTION_SOURCE_METADATA_KEY, SOURCE_NER, merge_results, name_context_evidence,
)


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


@pytest.mark.parametrize("entities", [None, [], ["PERSON"], ["ORGANIZATION"]])
def test_fragmented_fio_joins_accepted_name_and_login_fragments(entities):
    text = "ФИО: ИВАН ПЕТРОВ."
    fragments = [
        result(text, "ИВ", "PERSON", 0.78),
        result(text, "АН П", "LOGIN", 0.51),
        result(text, "ЕТ", "PERSON", 0.53),
        result(text, "РО", "LOGIN", 0.54),
        result(text, "В", "PERSON", 0.63),
    ]
    # The last single letter is inside the surname, not the first name.
    fragments[-1].start = text.rindex("В")
    fragments[-1].end = fragments[-1].start + 1
    for order in (fragments, list(reversed(fragments))):
        outcome = merge_results(text, order, requested_entities=entities, score_threshold=0.35)
        assert summary(text, outcome) == (
            [] if entities == ["ORGANIZATION"] else [("PERSON", "ИВАН ПЕТРОВ", 0.51)]
        )
        if outcome.results:
            assert outcome.results[0].recognition_metadata == fragments[0].recognition_metadata


@pytest.mark.parametrize("label", ["ФИО", "Ф.И.О."])
@pytest.mark.parametrize("separator", [":", " ="])
@pytest.mark.parametrize("value", ["АННА СОКОЛОВА.", "не заполнено.", ""])
def test_exact_person_field_label_is_not_a_name(label, separator, value):
    text = label + separator + " " + value
    outcome = merge_results(text, [result(text, label, "PERSON")])
    assert not outcome.results
    assert [d.reason for d in outcome.decisions] == ["name_field_label"]


@pytest.mark.parametrize("text,value", [
    ("Подписал Петров Ф. И. О.", "Петров Ф. И. О."),
    ("Ф.И.О. подписал документ", "Ф.И.О."),
    ("Организация Ф.И.О.: письмо", "Организация Ф.И.О."),
])
def test_person_label_suppression_does_not_remove_initials_or_larger_names(text, value):
    assert summary(text, merge_results(text, [result(text, value, "PERSON")])) == [
        ("PERSON", value, 0.8)
    ]


def test_structural_person_label_is_not_suppressed():
    text = "Ф.И.О.: не заполнено."
    assert merge_results(text, [result(text, "Ф.И.О.", "PERSON", source="structural")]).results


@pytest.mark.parametrize("entities", [None, ["PERSON"], ["ORGANIZATION"]])
def test_legal_context_joins_fragmented_ner_name_over_exact_location_prefix(entities):
    text = "Получатель: АО Анна Соколова."
    fragments = [
        result(text, "АО", score=0.7),
        result(text, "АО", "LOCATION", source="structural"),
        result(text, "Анна", "LOGIN", 0.71),
        result(text, "Соколова", "PERSON", 0.99),
    ]
    outcome = merge_results(text, fragments, requested_entities=entities)
    assert summary(text, outcome) == (
        [] if entities == ["PERSON"] else [("ORGANIZATION", "АО Анна Соколова", 0.7)]
    )
    assert "name_legal_prefix" in [d.reason for d in outcome.decisions]


@pytest.mark.parametrize("source,entity_type,value", [
    ("native_credential", "LOGIN", "Анна"),
    ("structural", "LOCATION", "Анна"),
    ("ner", "LOCATION", "Соколова"),
    ("structural", "PRIVATE_KEY", "АО Анна Соколова"),
])
def test_explicit_legal_context_does_not_override_competing_sensitive_results(source, entity_type, value):
    text = "АО Анна Соколова"
    outcome = merge_results(text, [
        result(text, "АО"), result(text, "Анна", "LOGIN"),
        result(text, "Соколова", "PERSON"),
        result(text, value, entity_type, source=source),
    ])
    assert "name_explicit_value" not in [d.reason for d in outcome.decisions]


@pytest.mark.parametrize("prefix", ["Компания ", "Название организации: "])
def test_explicit_org_context_refines_person_type(prefix):
    text = prefix + "АННА СОКОЛОВА получила письмо."
    for entity_type in ("PERSON", "ORGANIZATION"):
        outcome = merge_results(text, [result(text, "АННА СОКОЛОВА", "PERSON")], requested_entities=[entity_type])
        assert summary(text, outcome) == (
            [("ORGANIZATION", "АННА СОКОЛОВА", 0.8)] if entity_type == "ORGANIZATION" else []
        )


@pytest.mark.parametrize("prefix", [
    "Сотрудник компании ", "Для компании работает ", "Клиент ",
    "Контакт компании ", "В компании ",
])
def test_human_roles_or_ambiguous_client_do_not_reclassify_person(prefix):
    text = prefix + "АННА СОКОЛОВА получила письмо."
    assert summary(text, merge_results(text, [result(text, "АННА СОКОЛОВА", "PERSON")])) == [
        ("PERSON", "АННА СОКОЛОВА", 0.8)
    ]


@pytest.mark.parametrize("gap", ["\n", " " * 17, "\t" * 17])
def test_explicit_name_value_does_not_bridge_lines_or_excessive_gap(gap):
    text = "ФИО: ИВАН" + gap + "ПЕТРОВ"
    assert not name_context_evidence(text)


@pytest.mark.parametrize("text", [
    "ФИО: ИВАН ПЕТРОВ СЕРГЕЕВ НИКОЛАЕВ",
    "ФИО: Иван ПетровLOGIN", "ФИО: Иван login=Петров", "ФИО: Иван Петров_123",
])
def test_explicit_name_grammar_is_bounded_and_does_not_take_partial_words(text):
    assert not name_context_evidence(text)


def test_context_does_not_invent_missing_letters_or_raise_confidence():
    text = "ФИО: ИВАН ПЕТРОВ"
    fragments = [result(text, "ИВАН", "PERSON", 0.8), result(text, "ЕТРОВ", "LOGIN", 0.5)]
    assert "name_explicit_value" not in [d.reason for d in merge_results(text, fragments).decisions]
    fragments[1] = result(text, "ПЕТРОВ", "LOGIN", 0.2)
    assert "name_explicit_value" not in [d.reason for d in merge_results(text, fragments, score_threshold=0.35).decisions]


def test_many_adjacent_fields_are_independent():
    text = "ФИО: Иван Петров.\nФ.И.О. = Анна Соколова."
    outcome = merge_results(text, [
        result(text, "Иван Петров"), result(text, "Анна Соколова"),
        result(text, "Ф.И.О.", "PERSON"),
    ])
    assert summary(text, outcome) == [
        ("PERSON", "Иван Петров", 0.8), ("PERSON", "Анна Соколова", 0.8),
    ]


def test_truncated_whitespace_lookbehind_is_not_a_sentence_start():
    text = "Сотрудник" + " " * 65 + "компания АННА СОКОЛОВА"
    outcome = merge_results(text, [result(text, "АННА СОКОЛОВА", "PERSON")])
    assert summary(text, outcome) == [("PERSON", "АННА СОКОЛОВА", 0.8)]
