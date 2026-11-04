"""Tests for NER span boundary repair."""

from __future__ import annotations

import pytest

from presidio.ner.span_postprocessing import (
    expand_value_spans, expand_value_spans_with_evidence, postprocess_spans,
)


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


@pytest.mark.parametrize("source,value,label", [
    ("prefix 12345678 suffix", "12345678", "CONTRACT_NUMBER"),
    ('{"token":"abc+def"}', "abc+def", "AUTH_TOKEN"),
    ('token="abc def"', "abc def", "SECRET_KEY"),
    ("postgresql://svc:pass@host/db", "pass", "PASSWORD"),
    ("https://host/api?token=abc+def&other=ok", "abc+def", "AUTH_TOKEN"),
    ("Договор № АБ-123/25 подписан", "АБ-123/25", "CONTRACT_NUMBER"),
    (" prefix token=abc+def suffix", "abc+def", "AUTH_TOKEN"),
])
def test_bounded_value_evidence_reuses_existing_span_expansion(source, value, label):
    start = source.index(value)
    span = (start, start + 1, label, .8)
    result, = expand_value_spans_with_evidence(source, [span], "secrets-contracts")
    assert result.span == (start, start + len(value), label, .8)
    assert result.left_confirmed and result.right_confirmed
    assert expand_value_spans(source, [span], "secrets-contracts") == [result.span]


@pytest.mark.parametrize("source,start,end,label", [
    ("abcdef", 0, 6, "AUTH_TOKEN"),
    ("abc def", 0, 7, "AUTH_TOKEN"),
    ('{"a":"abc","b":"def"}', 7, 19, "AUTH_TOKEN"),
    ("https://svc:pass@host/db", 0, 5, "AUTH_TOKEN"),
    ('{"person":"Ivan"}', 11, 15, "PERSON"),
])
def test_unknown_ranges_do_not_masquerade_as_confirmed_internal_edges(source, start, end, label):
    result, = expand_value_spans_with_evidence(source, [(start, end, label, .8)], "secrets-contracts")
    assert not result.left_confirmed and not result.right_confirmed


def test_credential_label_coverage_is_not_relabelled_as_value_boundary():
    source = " prefix PASSWORD=abcdef suffix"
    start = source.index("WORD")
    result, = expand_value_spans_with_evidence(source, [(start, start + 4, "PASSWORD", .8)], "secrets-contracts")
    assert result.span == (start, source.index(" suffix"), "PASSWORD", .8)
    assert not result.left_confirmed and result.right_confirmed


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
