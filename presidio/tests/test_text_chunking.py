"""Tests for bounded outer text chunk planning."""

import pytest

from presidio.text_chunking import TextChunk, plan_text_chunks


def test_short_text_uses_one_exact_chunk():
    assert plan_text_chunks(
        "короткий текст",
        max_chunk_characters=100,
        overlap_characters=10,
        boundary_search_characters=10,
    ) == (
        TextChunk(index=0, start=0, end=14),
    )


def test_empty_text_needs_no_chunks():
    assert plan_text_chunks("") == ()


def test_chunks_are_bounded_overlap_and_cover_the_source():
    text = "0123456789" * 10
    chunks = plan_text_chunks(
        text,
        max_chunk_characters=30,
        overlap_characters=5,
        boundary_search_characters=0,
    )

    assert chunks == (
        TextChunk(index=0, start=0, end=30),
        TextChunk(index=1, start=25, end=55),
        TextChunk(index=2, start=50, end=80),
        TextChunk(index=3, start=75, end=100),
    )
    assert all(chunk.length <= 30 for chunk in chunks)
    assert all(
        left.end - right.start == 5
        for left, right in zip(chunks, chunks[1:])
    )
    assert chunks[0].start == 0
    assert chunks[-1].end == len(text)


@pytest.mark.parametrize(
    ("text", "expected_end"),
    [
        ("а" * 21 + "\n\n" + "б" * 20, 23),
        ("а" * 22 + "\n" + "б" * 20, 23),
        ("а" * 21 + ". " + "б" * 20, 22),
        ("а" * 21 + " " + "б" * 20, 22),
    ],
)
def test_first_chunk_prefers_natural_boundary(text, expected_end):
    chunks = plan_text_chunks(
        text,
        max_chunk_characters=30,
        overlap_characters=5,
        boundary_search_characters=10,
    )

    assert chunks[0].end == expected_end


@pytest.mark.parametrize(
    "kwargs",
    [
        {"max_chunk_characters": 0},
        {"max_chunk_characters": 10, "overlap_characters": 10},
        {
            "max_chunk_characters": 10,
            "overlap_characters": 5,
            "boundary_search_characters": 5,
        },
    ],
)
def test_invalid_planner_bounds_are_rejected(kwargs):
    with pytest.raises(ValueError):
        plan_text_chunks("текст", **kwargs)


def test_large_boundary_free_input_has_linear_number_of_chunks():
    text = "x" * 1_000_000
    chunks = plan_text_chunks(
        text,
        max_chunk_characters=128_000,
        overlap_characters=8_192,
        boundary_search_characters=8_192,
    )

    assert len(chunks) == 9
    assert chunks[-1].end == len(text)
    assert all(chunk.length <= 128_000 for chunk in chunks)
