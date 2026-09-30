"""Repair span boundaries of a small NER model before masking.

A small model often finds a secret but emits it as fragments (split on ``/``,
``+`` or ``-``, with the type flipping mid-value) or covers only part of it, so
the uncovered characters would reach the external LLM. These deterministic rules
run on the final, window-merged entities in normalized-text coordinates:

1. merge adjacent fragments of one value (credentials together, contract
   numbers separately) separated by at most two non-delimiter characters;
2. expand every credential (and, in ``secrets-contracts``, contract number) to
   the nearest delimiter, then drop trailing sentence punctuation.

The rules and delimiter sets were selected on validation data for the
``tiny2`` profile; see docs/research/ner-tiny2.md.
"""

from __future__ import annotations

try:
    from model_profiles import (
        SPAN_POSTPROCESSING_NONE,
        SPAN_POSTPROCESSING_SECRETS_CONTRACTS,
        SPAN_POSTPROCESSING_MODES,
    )
except ImportError:
    from presidio.model_profiles import (
        SPAN_POSTPROCESSING_NONE,
        SPAN_POSTPROCESSING_SECRETS_CONTRACTS,
        SPAN_POSTPROCESSING_MODES,
    )

Span = tuple[int, int, str, float]

CREDENTIAL_TYPES = frozenset({"LOGIN", "PASSWORD", "SECRET_KEY", "AUTH_TOKEN"})
CONTRACT_TYPES = frozenset({"CONTRACT_NUMBER"})
MAX_MERGE_GAP = 2
WHITESPACE = frozenset(" \t\r\n")
# Delimiters that end any value: whitespace, quotes, assignment and brackets.
BASE_DELIMITERS = WHITESPACE | frozenset("'\"`=,;()[]{}<>")
# Credentials additionally stop at URL/DSN separators so that
# ``scheme://login:password@host`` yields two values, not one.
CREDENTIAL_DELIMITERS = BASE_DELIMITERS | frozenset(":@/&?")
TRAILING_PUNCTUATION = frozenset(".,;:!?")


def _merge_group(text: str, spans: list[Span], group: frozenset[str]) -> list[Span]:
    merged: list[Span] = []
    for span in sorted(spans):
        start, end, label, score = span
        if merged:
            prev_start, prev_end, prev_label, prev_score = merged[-1]
            gap = text[prev_end:start]
            if (
                label in group
                and prev_label in group
                and 0 <= start - prev_end <= MAX_MERGE_GAP
                and not any(char in BASE_DELIMITERS for char in gap)
            ):
                merged_label = (
                    prev_label if prev_end - prev_start >= end - start else label
                )
                merged[-1] = (
                    prev_start,
                    max(prev_end, end),
                    merged_label,
                    min(prev_score, score),
                )
                continue
        merged.append(span)
    return merged


def _expand(
    text: str,
    spans: list[Span],
    labels: frozenset[str],
    delimiters: frozenset[str],
) -> list[Span]:
    expanded: list[Span] = []
    for start, end, label, score in spans:
        if label in labels:
            while start > 0 and text[start - 1] not in delimiters:
                start -= 1
            while end < len(text) and text[end] not in delimiters:
                end += 1
            while (
                end - start > 1
                and text[end - 1] in TRAILING_PUNCTUATION
                and (end == len(text) or text[end] in WHITESPACE)
            ):
                end -= 1
        expanded.append((start, end, label, score))
    return _drop_overlaps(expanded)


def _drop_overlaps(spans: list[Span]) -> list[Span]:
    """Keep the longer span when expansion made two spans overlap."""
    result: list[Span] = []
    for span in sorted(set(spans)):
        if result and span[0] < result[-1][1]:
            if span[1] - span[0] > result[-1][1] - result[-1][0]:
                result[-1] = span
            continue
        result.append(span)
    return result


def postprocess_spans(text: str, spans: list[Span], mode: str) -> list[Span]:
    """Return repaired spans; ``none`` returns the input unchanged."""
    if mode not in SPAN_POSTPROCESSING_MODES:
        raise ValueError(f"unknown span post-processing mode: {mode}")
    if mode == SPAN_POSTPROCESSING_NONE or not spans:
        return list(spans)
    result = _merge_group(text, list(spans), CREDENTIAL_TYPES)
    if mode == SPAN_POSTPROCESSING_SECRETS_CONTRACTS:
        result = _merge_group(text, result, CONTRACT_TYPES)
    result = _expand(text, result, CREDENTIAL_TYPES, CREDENTIAL_DELIMITERS)
    if mode == SPAN_POSTPROCESSING_SECRETS_CONTRACTS:
        result = _expand(text, result, CONTRACT_TYPES, BASE_DELIMITERS)
    return result
