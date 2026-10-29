"""Bounded, opt-in negative evidence for truncated shell prompt detections."""
from __future__ import annotations

import re
from typing import Any, Callable
from .text_processing import NormalizedText

RECHECK_OFF = "off"
RECHECK_SHELL_PROMPT = "shell-prompt"
PROMPT = re.compile(
    r"(?i)^(?:bash|zsh|sh|fish)(?P<edge>(?:[- ])?[$#>])(?:[ \t]+[^\r\n]*)?$"
)
CREDENTIAL_CONTEXT = re.compile(
    r"(?i)\b(?:password|passwd|pwd|credentials|secret_key|api_secret|"
    r"login|username|token|authorization|парол\w*|логин\w*)\b"
    r"[^\r\n]{0,48}[:=]\s*[\"']?\s*$"
)
CREDENTIAL_TYPES = frozenset({"PASSWORD", "LOGIN", "AUTH_TOKEN", "SECRET_KEY"})


def resolve_recheck(profile_name: str) -> str:
    if profile_name != "tiny2":
        raise ValueError("Unknown processing bundle")
    # E5 did not establish a benefit: preserve E6's runtime default. The narrow
    # algorithm remains independently testable, not an administrator env mode.
    return RECHECK_OFF


def prediction_key(prediction: Any) -> tuple[str, int, int, int]:
    entity = prediction.entity
    return entity.entity_type, entity.start, entity.end, prediction.window_index


def eligible_prompt(text: str, start: int, end: int) -> bool:
    if not 0 <= start < end <= len(text):
        return False
    value = text[start:end]
    if len(value) > 4 or any(c.isalnum() or c.isspace() for c in value):
        return False
    left = text.rfind("\n", 0, start) + 1
    right = text.find("\n", end)
    if right < 0:
        right = len(text)
    raw_line = text[left:right]
    line = raw_line.strip()
    match = PROMPT.fullmatch(line) if len(line) <= 64 else None
    if match is None:
        return False
    line_start = left + len(raw_line) - len(raw_line.lstrip())
    edge_start, edge_end = match.span("edge")
    # A prompt-shaped line does not make punctuation in its arguments safe.
    if not line_start + edge_start <= start < end <= line_start + edge_end:
        return False
    return not (
        CREDENTIAL_CONTEXT.search(text[max(0, left - 96):left])
        or CREDENTIAL_CONTEXT.search(text[max(0, left - 96):start])
    )


def confirmed_prompt_edges(
    normalized: NormalizedText, input_ids: list[int], offsets: list[tuple[int, int]],
    clusters: tuple[Any, ...], positive_recovery: list[Any],
    protected_spans: tuple[tuple[int, int], ...] | None,
    infer: Callable[..., Any], check_cancelled: Callable[[], None],
) -> tuple[set[tuple[str, int, int, int]], int]:
    """Confirm O in two centers; native spans use original input coordinates.

    None means authoritative evidence was not supplied: withdraw nothing.
    Raw O scores are heuristics, not calibrated safety probabilities.
    """
    confirmed: set[tuple[str, int, int, int]] = set()
    processed = checked = 0
    if protected_spans is None:
        return confirmed, processed
    for cluster in clusters:
        check_cancelled()
        if cluster.entity_type not in CREDENTIAL_TYPES:
            continue
        start = min(p.entity.start for p in cluster.predictions)
        end = max(p.entity.end for p in cluster.predictions)
        if checked >= 4 or not eligible_prompt(normalized.text, start, end):
            continue
        if any(p.entity.start < end and start < p.entity.end for p in positive_recovery):
            continue
        try:
            original_start, original_end = normalized.to_original_span(start, end)
            if any(a < original_end and original_start < b for a, b in protected_spans):
                continue
            checked += 1
            indices = [i for i, (a, b) in enumerate(offsets) if a < end and start < b]
            if not indices or len(input_ids) <= 384:
                continue
            center = (indices[0] + indices[-1]) // 2
            starts = sorted({min(max(0, center - 192 + shift), len(input_ids) - 384)
                             for shift in (-32, 32)})
            if len(starts) != 2 or any(
                indices[0] - a < 64 or a + 384 - indices[-1] - 1 < 64 for a in starts
            ):
                continue
        except Exception:
            continue
        check_cancelled()
        try:
            predictions = infer([input_ids[a:a + 384] for a in starts],
                                [offsets[a:a + 384] for a in starts])
        except Exception:
            check_cancelled()
            continue
        processed += len(starts)
        check_cancelled()
        try:
            confirmations = []
            if len(predictions) != 2:
                continue
            for window in predictions:
                relevant = [p for p in window if p.start < end and start < p.end]
                covered = all(any(p.start <= i < p.end for p in relevant)
                              for i in range(start, end))
                confirmations.append(covered and bool(relevant) and all(
                    p.label == "O" and 0.90 <= p.score <= 1.0 for p in relevant
                ))
            if all(confirmations):
                confirmed.update(prediction_key(p) for p in cluster.predictions)
        except Exception:
            continue
    return confirmed, processed
