"""Tests for Unicode mapping and semantic NER window planning."""

from __future__ import annotations

import unicodedata

import pytest

from presidio.ner.text_processing import (
    TextMappingError,
    normalize_for_ner,
    plan_token_windows,
)


def _word_offsets(text: str) -> list[tuple[int, int]]:
    offsets = []
    cursor = 0
    for word in text.split():
        start = text.index(word, cursor)
        end = start + len(word)
        offsets.append((start, end))
        cursor = end
    return offsets


def _word_text(count: int, *, sentence_at: int | None = None) -> str:
    words = [f"w{index}" for index in range(count)]
    if sentence_at is not None:
        words[sentence_at] += "."
    return " ".join(words)


@pytest.mark.parametrize(
    "text",
    [
        "Сергеи\u0306 Петров",
        "Jose\u0301 Alvarez",
        "👩‍💻 Сергеи\u0306 Петров",
        "строка из PDF\u00a0без замены пробела",
    ],
)
def test_nfc_mapping_preserves_original_ranges(text):
    mapped = normalize_for_ner(text)

    assert mapped.text == unicodedata.normalize("NFC", text)
    assert len(mapped.source_spans) == len(mapped.text)
    assert mapped.to_original_span(0, len(mapped.text)) == (0, len(text))


def test_nfc_mapping_expands_composed_character_to_original_cluster():
    text = "Сергеи\u0306 Петров"
    mapped = normalize_for_ner(text)
    normalized_value = "Сергей"

    start = mapped.text.index(normalized_value)
    original_start, original_end = mapped.to_original_span(
        start,
        start + len(normalized_value),
    )

    assert text[original_start:original_end] == "Сергеи\u0306"


def test_nfc_mapping_keeps_repeated_values_position_specific():
    text = "Сергеи\u0306 встретил Сергеи\u0306"
    mapped = normalize_for_ner(text)
    first = mapped.text.index("Сергей")
    second = mapped.text.index("Сергей", first + 1)

    first_span = mapped.to_original_span(first, first + len("Сергей"))
    second_span = mapped.to_original_span(second, second + len("Сергей"))

    assert first_span == (0, len("Сергеи\u0306"))
    assert second_span[0] > first_span[1]
    assert text[slice(*second_span)] == "Сергеи\u0306"


@pytest.mark.parametrize("span", [(-1, 1), (0, 0), (0, 99)])
def test_nfc_mapping_rejects_invalid_spans(span):
    mapped = normalize_for_ner("Иван")

    with pytest.raises(TextMappingError):
        mapped.to_original_span(*span)


def test_window_planner_does_not_split_exact_limit():
    text = _word_text(384)

    windows = plan_token_windows(
        text,
        _word_offsets(text),
        max_content_tokens=384,
        overlap_tokens=64,
        boundary_search_tokens=64,
    )

    assert [(window.start_token, window.end_token) for window in windows] == [
        (0, 384)
    ]


def test_window_planner_uses_overlap_after_hard_split():
    text = _word_text(385)

    windows = plan_token_windows(
        text,
        _word_offsets(text),
        max_content_tokens=384,
        overlap_tokens=64,
        boundary_search_tokens=64,
    )

    assert [(window.start_token, window.end_token) for window in windows] == [
        (0, 384),
        (320, 385),
    ]


def test_window_planner_prefers_sentence_boundary_in_search_area():
    text = _word_text(420, sentence_at=349)

    windows = plan_token_windows(
        text,
        _word_offsets(text),
        max_content_tokens=384,
        overlap_tokens=64,
        boundary_search_tokens=64,
    )

    assert windows[0].end_token == 350
    assert windows[1].start_token == 286


def test_window_planner_prefers_paragraph_over_later_sentence():
    words = [f"w{index}" for index in range(420)]
    words[359] += "."
    text = " ".join(words[:340]) + "\n\n" + " ".join(words[340:])

    windows = plan_token_windows(
        text,
        _word_offsets(text),
        max_content_tokens=384,
        overlap_tokens=64,
        boundary_search_tokens=64,
    )

    assert windows[0].end_token == 340
    assert windows[1].start_token == 276


def test_window_planner_rejects_invalid_offsets():
    with pytest.raises(TextMappingError):
        plan_token_windows(
            "text",
            [(0, 5)],
            max_content_tokens=384,
            overlap_tokens=64,
            boundary_search_tokens=64,
        )
