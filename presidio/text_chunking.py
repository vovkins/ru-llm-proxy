"""Bounded, overlap-aware planning for large Analyzer inputs."""

from __future__ import annotations

from dataclasses import dataclass


DEFAULT_MAX_CHUNK_CHARACTERS = 128_000
DEFAULT_CHUNK_OVERLAP_CHARACTERS = 8_192
DEFAULT_BOUNDARY_SEARCH_CHARACTERS = 8_192
SENTENCE_ENDINGS = frozenset(".!?…。！？")


@dataclass(frozen=True)
class TextChunk:
    """Half-open source range processed as one bounded Analyzer unit."""

    index: int
    start: int
    end: int

    @property
    def length(self) -> int:
        return self.end - self.start


def _preferred_chunk_end(
    text: str,
    *,
    minimum_end: int,
    hard_end: int,
) -> int:
    """Choose the latest natural boundary without slicing a growing prefix."""
    paragraph = text.rfind("\n\n", minimum_end, hard_end)
    if paragraph >= minimum_end:
        return paragraph + 2

    newline = text.rfind("\n", minimum_end, hard_end)
    if newline >= minimum_end:
        return newline + 1

    for index in range(hard_end - 1, minimum_end - 1, -1):
        if (
            text[index] in SENTENCE_ENDINGS
            and index + 1 < len(text)
            and text[index + 1].isspace()
        ):
            return index + 1

    for index in range(hard_end - 1, minimum_end - 1, -1):
        if text[index].isspace():
            return index + 1
    return hard_end


def plan_text_chunks(
    text: str,
    *,
    max_chunk_characters: int = DEFAULT_MAX_CHUNK_CHARACTERS,
    overlap_characters: int = DEFAULT_CHUNK_OVERLAP_CHARACTERS,
    boundary_search_characters: int = DEFAULT_BOUNDARY_SEARCH_CHARACTERS,
) -> tuple[TextChunk, ...]:
    """Cover text with bounded overlapping chunks and guaranteed progress."""
    if max_chunk_characters < 1:
        raise ValueError("max_chunk_characters must be positive")
    if not 0 <= overlap_characters < max_chunk_characters:
        raise ValueError("overlap_characters must be below the chunk size")
    if not 0 <= boundary_search_characters < (
        max_chunk_characters - overlap_characters
    ):
        raise ValueError("boundary search leaves no guaranteed forward progress")
    if not text:
        return ()
    if len(text) <= max_chunk_characters:
        return (TextChunk(index=0, start=0, end=len(text)),)

    chunks: list[TextChunk] = []
    start = 0
    while start < len(text):
        hard_end = min(start + max_chunk_characters, len(text))
        if hard_end == len(text):
            end = hard_end
        else:
            minimum_end = hard_end - boundary_search_characters
            end = _preferred_chunk_end(
                text,
                minimum_end=minimum_end,
                hard_end=hard_end,
            )

        if end <= start:
            raise RuntimeError("text chunk planner made no progress")
        chunks.append(TextChunk(index=len(chunks), start=start, end=end))
        if end == len(text):
            break

        next_start = end - overlap_characters
        if next_start <= start:
            raise RuntimeError("text chunk overlap prevents forward progress")
        start = next_start

    return tuple(chunks)
