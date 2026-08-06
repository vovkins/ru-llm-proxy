"""Unicode-safe text normalization and token window planning for NER."""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass

import regex


GRAPHEME_PATTERN = regex.compile(r"\X")
SENTENCE_ENDINGS = frozenset(".!?…")


class TextMappingError(RuntimeError):
    """Raised when normalized offsets cannot be mapped safely."""


@dataclass(frozen=True)
class NormalizedText:
    """NFC text with a source range for every normalized character."""

    original: str
    text: str
    source_spans: tuple[tuple[int, int], ...]

    def to_original_span(self, start: int, end: int) -> tuple[int, int]:
        if not (0 <= start < end <= len(self.text)):
            raise TextMappingError("normalized entity span is outside the text")
        original_start = self.source_spans[start][0]
        original_end = self.source_spans[end - 1][1]
        if not (0 <= original_start < original_end <= len(self.original)):
            raise TextMappingError("mapped entity span is outside the original text")
        return original_start, original_end


@dataclass(frozen=True)
class TokenWindow:
    """Half-open range of content tokens sent through one model pass."""

    index: int
    start_token: int
    end_token: int


def normalize_for_ner(text: str) -> NormalizedText:
    """Normalize grapheme clusters to NFC and retain original source ranges."""
    normalized_parts: list[str] = []
    source_spans: list[tuple[int, int]] = []

    for match in GRAPHEME_PATTERN.finditer(text):
        normalized_cluster = unicodedata.normalize("NFC", match.group())
        normalized_parts.append(normalized_cluster)
        source_spans.extend(
            (match.start(), match.end()) for _ in normalized_cluster
        )

    normalized = "".join(normalized_parts)
    if normalized != unicodedata.normalize("NFC", text):
        raise TextMappingError("grapheme normalization does not match whole-text NFC")
    if len(normalized) != len(source_spans):
        raise TextMappingError("normalized character mapping is incomplete")
    return NormalizedText(
        original=text,
        text=normalized,
        source_spans=tuple(source_spans),
    )


def _validate_token_offsets(
    text: str,
    token_offsets: list[tuple[int, int]],
) -> None:
    previous_start = 0
    for start, end in token_offsets:
        if not (0 <= start < end <= len(text)):
            raise TextMappingError("content token offset is outside normalized text")
        if start < previous_start:
            raise TextMappingError("content token offsets are not ordered")
        previous_start = start


def _preferred_window_end(
    text: str,
    token_offsets: list[tuple[int, int]],
    *,
    minimum_end: int,
    hard_end: int,
) -> int:
    paragraph_ends: list[int] = []
    sentence_ends: list[int] = []

    for end_token in range(minimum_end, hard_end + 1):
        previous_end = token_offsets[end_token - 1][1]
        next_start = token_offsets[end_token][0]
        separator = text[previous_end:next_start]
        if "\n" in separator:
            paragraph_ends.append(end_token)
            continue
        if separator and separator.isspace():
            preceding = text[:previous_end].rstrip()
            if preceding and preceding[-1] in SENTENCE_ENDINGS:
                sentence_ends.append(end_token)

    if paragraph_ends:
        return paragraph_ends[-1]
    if sentence_ends:
        return sentence_ends[-1]
    return hard_end


def plan_token_windows(
    text: str,
    token_offsets: list[tuple[int, int]],
    *,
    max_content_tokens: int,
    overlap_tokens: int,
    boundary_search_tokens: int,
) -> tuple[TokenWindow, ...]:
    """Plan overlapping windows with deterministic semantic boundary preference."""
    if max_content_tokens <= 0:
        raise ValueError("max_content_tokens must be positive")
    if not 0 < overlap_tokens < max_content_tokens:
        raise ValueError("overlap_tokens must be between zero and window size")
    if not 0 <= boundary_search_tokens < max_content_tokens - overlap_tokens:
        raise ValueError("boundary_search_tokens leaves no forward progress")

    _validate_token_offsets(text, token_offsets)
    token_count = len(token_offsets)
    if token_count == 0:
        return ()
    if token_count <= max_content_tokens:
        return (TokenWindow(index=0, start_token=0, end_token=token_count),)

    windows: list[TokenWindow] = []
    start_token = 0
    while start_token < token_count:
        hard_end = min(start_token + max_content_tokens, token_count)
        if hard_end == token_count:
            end_token = hard_end
        else:
            minimum_end = hard_end - boundary_search_tokens
            end_token = _preferred_window_end(
                text,
                token_offsets,
                minimum_end=minimum_end,
                hard_end=hard_end,
            )

        windows.append(
            TokenWindow(
                index=len(windows),
                start_token=start_token,
                end_token=end_token,
            )
        )
        if end_token == token_count:
            break
        next_start = end_token - overlap_tokens
        if next_start <= start_token:
            raise TextMappingError("token window planner made no forward progress")
        start_token = next_start

    return tuple(windows)
