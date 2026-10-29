"""Tests for NER span boundary repair."""

from __future__ import annotations

import pytest

from presidio.ner.span_postprocessing import expand_value_spans, postprocess_spans


def _spans(text, pieces, score=0.9):
    spans = []
    for fragment, label in pieces:
        start = text.index(fragment)
        spans.append((start, start + len(fragment), label, score))
    return spans


def _values(text, spans):
    return [(text[start:end], label) for start, end, label, _ in spans]


def test_none_mode_returns_model_spans_unchanged():
    text = "документ 2020/АГ/6581."
    spans = _spans(text, [("2020", "CONTRACT_NUMBER"), ("АГ/6581", "CONTRACT_NUMBER")])

    assert postprocess_spans(text, spans, "none") == spans


def test_contract_fragments_expand_without_guessing_an_unconfirmed_final_dot():
    text = "Статус обработки: успешно завершено для документа 2020/АГ/6581."
    spans = _spans(text, [("2020", "CONTRACT_NUMBER"), ("АГ/6581", "CONTRACT_NUMBER")])

    result = postprocess_spans(text, spans, "secrets-contracts")

    assert _values(text, result) == [("2020/АГ/6581.", "CONTRACT_NUMBER")]


def test_secrets_mode_leaves_contract_numbers_untouched():
    text = "документ 2020/АГ/6581."
    spans = _spans(text, [("2020", "CONTRACT_NUMBER"), ("АГ/6581", "CONTRACT_NUMBER")])

    assert postprocess_spans(text, spans, "secrets") == spans


def test_credential_fragments_preserve_types_until_common_merge():
    text = "MINIO_SECRET_KEY=whsec_NJ09mthHfqlQs3gps+biyjd"
    spans = _spans(
        text,
        [
            ("whsec_NJ09mthHf", "SECRET_KEY"),
            ("qlQs3gps", "AUTH_TOKEN"),
            ("biyjd", "SECRET_KEY"),
        ],
    )

    result = postprocess_spans(text, spans, "secrets")

    assert _values(text, result) == [
        ("whsec_NJ09mthHfqlQs3gps+biyjd", "AUTH_TOKEN"),
        ("whsec_NJ09mthHfqlQs3gps+biyjd", "SECRET_KEY"),
    ]


def test_partial_login_expands_but_stops_at_dsn_separators():
    text = "DATABASE_URL=postgresql://svc-media-eu1-a9b9:1592@db-master:5432/app"
    spans = _spans(text, [("svc-media", "LOGIN"), ("1592", "PASSWORD")])

    result = postprocess_spans(text, spans, "secrets-contracts")

    assert _values(text, result) == [
        ("svc-media-eu1-a9b9", "LOGIN"),
        ("1592", "PASSWORD"),
    ]


def test_middle_of_key_expands_including_ambiguous_sentence_dot():
    text = "Не забудьте, проверьте ключ 6sm7eRYoUhbhwBDHkQezdo9YdN0wOxe3x20tZycn."
    spans = _spans(text, [("hwBDHkQezdo9YdN0wOx", "SECRET_KEY")])

    result = postprocess_spans(text, spans, "secrets")

    assert _values(text, result) == [
        ("6sm7eRYoUhbhwBDHkQezdo9YdN0wOxe3x20tZycn.", "SECRET_KEY")
    ]


def test_fragments_separated_by_a_delimiter_stay_separate():
    text = "login=admin password=hunter2"
    spans = _spans(text, [("admin", "LOGIN"), ("hunter2", "PASSWORD")])

    result = postprocess_spans(text, spans, "secrets")

    assert _values(text, result) == [("admin", "LOGIN"), ("hunter2", "PASSWORD")]


def test_expanded_value_keeps_each_original_score():
    text = "token=abc+def"
    spans = [(6, 9, "AUTH_TOKEN", 0.9), (10, 13, "AUTH_TOKEN", 0.6)]

    assert postprocess_spans(text, spans, "secrets") == [
        (6, 13, "AUTH_TOKEN", 0.6), (6, 13, "AUTH_TOKEN", 0.9),
    ]


def test_person_and_location_are_never_changed():
    text = "Иван Петров из Москвы."
    spans = _spans(text, [("Иван Петров", "PERSON"), ("Москвы", "LOCATION")])

    assert postprocess_spans(text, spans, "secrets-contracts") == spans


def test_expansion_overlap_is_deferred_to_the_common_merger():
    text = "key=abcdef"
    spans = [(4, 6, "SECRET_KEY", 0.9), (7, 9, "PERSON", 0.8)]

    result = postprocess_spans(text, spans, "secrets")

    assert _values(text, result) == [("abcdef", "SECRET_KEY"), ("de", "PERSON")]


def test_unknown_mode_is_rejected():
    with pytest.raises(ValueError, match="unknown span post-processing mode"):
        postprocess_spans("x", [], "aggressive")


def test_value_expansion_preserves_associations_before_final_deduplication():
    text = 'key="abcdef"'
    spans = [(8, 10, "AUTH_TOKEN", .8), (5, 6, "AUTH_TOKEN", .8), (8, 10, "AUTH_TOKEN", .8)]
    assert expand_value_spans(text, spans, "secrets-contracts") == [(5, 11, "AUTH_TOKEN", .8)] * 3
    assert postprocess_spans(text, spans, "secrets-contracts") == [(5, 11, "AUTH_TOKEN", .8)]


@pytest.mark.parametrize("mode,kind", [("none", "AUTH_TOKEN"), ("secrets-contracts", "PERSON")])
def test_disabled_or_unhandled_expansion_keeps_order_and_duplicates(mode, kind):
    spans = [(3, 4, kind, .9), (0, 1, kind, .8), (3, 4, kind, .9)]
    assert expand_value_spans("abcdef", spans, mode) == spans
    assert postprocess_spans("abcdef", spans, mode) == spans
