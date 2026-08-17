"""Source-aware merging for overlapping Analyzer results."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable

from presidio_analyzer import RecognizerResult


DETECTION_SOURCE_METADATA_KEY = "ru_llm_detection_source"
NER_MODEL_METADATA_KEY = "ru_llm_ner_model"

SOURCE_NER = "ner"
SOURCE_NATIVE_CREDENTIAL = "native_credential"
SOURCE_STRUCTURAL = "structural"
SOURCE_CONTEXTUAL_CONTRACT = "contextual_contract"
SOURCE_NONE = "none"

CONTRACT_CONTEXT_SCORE = 0.85
CONTRACT_CONTEXT_RECOGNIZER_NAME = "ContractContextFallback"

_KNOWN_SOURCES = frozenset(
    {
        SOURCE_NER,
        SOURCE_NATIVE_CREDENTIAL,
        SOURCE_STRUCTURAL,
        SOURCE_CONTEXTUAL_CONTRACT,
    }
)
_STRUCTURED_CONTAINER_TYPES = frozenset(
    {"PRIVATE_KEY", "DB_URL", "JWT", "API_KEY"}
)
_CREDENTIAL_ENTITY_TYPES = frozenset(
    {"LOGIN", "PASSWORD", "AUTH_TOKEN", "SECRET_KEY"}
)
_INERT_NER_CREDENTIAL_VALUES = frozenset(
    {
        "changeme",
        "example",
        "false",
        "none",
        "null",
        "placeholder",
        "sample",
        "test",
        "true",
        "your-key",
        "your-token",
        "your_api_key",
    }
)
_ENTITY_SPECIFICITY = {
    "PRIVATE_KEY": 0,
    "DB_URL": 1,
    "JWT": 2,
    "API_KEY": 3,
    "BEARER_TOKEN": 4,
    "SECRET_KEY": 5,
    "AUTH_TOKEN": 6,
}

_CONTRACT_CONTEXT_RE = re.compile(
    r"(?i)(?<![\w])(?:"
    r"договор(?:а|у|ом|е|ы|ов|ам|ами|ах)?|"
    r"контракт(?:а|у|ом|е|ы|ов|ам|ами|ах)?|"
    r"госконтракт(?:а|у|ом|е|ы|ов|ам|ами|ах)?|"
    r"гос\.?\s+контракт(?:а|у|ом|е|ы|ов|ам|ами|ах)?|"
    r"государственн(?:ый|ого|ому|ым|ом|ые|ых|ыми)\s+"
    r"контракт(?:а|у|ом|е|ы|ов|ам|ами|ах)?|"
    r"соглашени(?:е|я|ю|ем|и|й|ям|ями|ях)|"
    r"contracts?|agreements?"
    r")(?![\w])"
)
_CONTRACT_CANDIDATE_RE = re.compile(
    r"(?<![\w])"
    r"(?P<value>[A-Za-zА-Яа-яЁё0-9]"
    r"[A-Za-zА-Яа-яЁё0-9./\-\u00ad]{2,63})"
    r"(?![\w])"
)
_CONTRACT_DISQUALIFIER_RE = re.compile(
    r"(?i)(?<![\w])(?:верси(?:я|и|ю|ей)|редакци(?:я|и|ю|ей)|"
    r"шаблон(?:а|у|ом|е|ы|ов)?|template|version|revision)(?![\w])"
)
_EXPLICIT_CONTRACT_NUMBER_RE = re.compile(
    r"(?i)(?:номер|№|#|\bno\.?\b|\bnumber\b)"
)
_DATE_LIKE_RE = re.compile(r"\d{1,2}[./-]\d{1,2}[./-]\d{2,4}")
_DOTTED_VERSION_RE = re.compile(r"\d{1,3}(?:\.\d{1,3}){1,3}")
_SENTENCE_BOUNDARIES = frozenset(".!?;。！？")


@dataclass(frozen=True)
class MergeDecision:
    """One bounded, value-free decision made while merging results."""

    reason: str
    winner_source: str
    loser_source: str


@dataclass(frozen=True)
class MergeOutcome:
    """Merged results and safe diagnostics for observability."""

    results: tuple[RecognizerResult, ...]
    decisions: tuple[MergeDecision, ...]


def detection_source(result: RecognizerResult) -> str:
    """Return a bounded source identifier for a result."""
    metadata = result.recognition_metadata or {}
    source = metadata.get(DETECTION_SOURCE_METADATA_KEY)
    if source in _KNOWN_SOURCES:
        return str(source)
    return SOURCE_STRUCTURAL


def contract_context_evidence(text: str) -> tuple[tuple[int, int], ...]:
    """Return contract-number candidates backed by nearby contract context."""
    evidence = []
    for match in _CONTRACT_CANDIDATE_RE.finditer(text):
        value = match.group("value")
        if not any(character.isdigit() for character in value):
            continue
        normalized_value = value.replace("\u00ad", "")
        if (
            _DATE_LIKE_RE.fullmatch(normalized_value)
            or _DOTTED_VERSION_RE.fullmatch(normalized_value)
            or (normalized_value.isdigit() and len(normalized_value) < 6)
        ):
            continue

        start, end = match.span("value")
        while end > start and text[end - 1] in ".-/\u00ad":
            end -= 1
        if end - start < 3:
            continue

        sentence_start = start
        while sentence_start > 0 and not _is_sentence_boundary(
            text,
            sentence_start - 1,
        ):
            sentence_start -= 1
        context_start = max(sentence_start, start - 96)
        context = text[context_start:start]
        has_contract_context = _CONTRACT_CONTEXT_RE.search(context) is not None
        has_disqualifier = _CONTRACT_DISQUALIFIER_RE.search(context) is not None
        has_explicit_number = _EXPLICIT_CONTRACT_NUMBER_RE.search(context) is not None
        if has_contract_context and (not has_disqualifier or has_explicit_number):
            evidence.append((start, end))

    return tuple(dict.fromkeys(evidence))


def _is_sentence_boundary(text: str, index: int) -> bool:
    character = text[index]
    if character not in _SENTENCE_BOUNDARIES:
        return False
    if character == "." and text[max(0, index - 2) : index].casefold() == "no":
        return False
    return True


def merge_results(
    text: str,
    results: Iterable[RecognizerResult],
    *,
    requested_entities: Iterable[str] | None = None,
    score_threshold: float = 0.0,
) -> MergeOutcome:
    """Merge overlapping results using source and entity specificity."""
    decisions = []
    valid_results = []
    contract_evidence = contract_context_evidence(text)

    for result in results:
        source = detection_source(result)
        if not _valid_span(result, len(text)):
            decisions.append(
                MergeDecision("invalid_span", SOURCE_NONE, source)
            )
            continue
        if (
            source == SOURCE_NER
            and result.entity_type in _CREDENTIAL_ENTITY_TYPES
            and text[result.start : result.end].strip().casefold()
            in _INERT_NER_CREDENTIAL_VALUES
        ):
            decisions.append(
                MergeDecision(
                    "credential_placeholder_suppressed",
                    SOURCE_NONE,
                    SOURCE_NER,
                )
            )
            continue
        if source == SOURCE_NER and result.entity_type == "CONTRACT_NUMBER":
            confirmed = any(
                _spans_overlap(result.start, result.end, start, end)
                for start, end in contract_evidence
            )
            if not confirmed:
                decisions.append(
                    MergeDecision("contract_unconfirmed", SOURCE_NONE, source)
                )
                continue
            result = _with_detection_source(
                result,
                SOURCE_CONTEXTUAL_CONTRACT,
            )
            decisions.append(
                MergeDecision(
                    "contract_context_confirmed",
                    SOURCE_CONTEXTUAL_CONTRACT,
                    SOURCE_NER,
                )
            )
        valid_results.append(result)

    contract_requested = (
        requested_entities is None
        or "CONTRACT_NUMBER" in requested_entities
    )
    if contract_requested and CONTRACT_CONTEXT_SCORE >= score_threshold:
        for start, end in contract_evidence:
            if any(
                result.entity_type == "CONTRACT_NUMBER"
                and result.start == start
                and result.end == end
                for result in valid_results
            ):
                continue
            valid_results.append(
                RecognizerResult(
                    entity_type="CONTRACT_NUMBER",
                    start=start,
                    end=end,
                    score=CONTRACT_CONTEXT_SCORE,
                    recognition_metadata={
                        DETECTION_SOURCE_METADATA_KEY: SOURCE_CONTEXTUAL_CONTRACT,
                        RecognizerResult.RECOGNIZER_NAME_KEY: (
                            CONTRACT_CONTEXT_RECOGNIZER_NAME
                        ),
                    },
                )
            )
            decisions.append(
                MergeDecision(
                    "contract_context_fallback",
                    SOURCE_CONTEXTUAL_CONTRACT,
                    SOURCE_NONE,
                )
            )

    ordered = sorted(valid_results, key=_priority_key)
    kept: list[RecognizerResult] = []
    for candidate in ordered:
        conflict = next(
            (
                existing
                for existing in kept
                if _spans_overlap(
                    candidate.start,
                    candidate.end,
                    existing.start,
                    existing.end,
                )
            ),
            None,
        )
        if conflict is None:
            kept.append(candidate)
            continue

        decisions.append(
            MergeDecision(
                _conflict_reason(conflict, candidate),
                detection_source(conflict),
                detection_source(candidate),
            )
        )

    kept.sort(key=lambda result: (result.start, result.end, result.entity_type))
    return MergeOutcome(tuple(kept), tuple(decisions))


def _valid_span(result: RecognizerResult, text_length: int) -> bool:
    return (
        isinstance(result.start, int)
        and isinstance(result.end, int)
        and 0 <= result.start < result.end <= text_length
    )


def _with_detection_source(
    result: RecognizerResult,
    source: str,
) -> RecognizerResult:
    metadata = dict(result.recognition_metadata or {})
    metadata[DETECTION_SOURCE_METADATA_KEY] = source
    return RecognizerResult(
        entity_type=result.entity_type,
        start=result.start,
        end=result.end,
        score=result.score,
        analysis_explanation=result.analysis_explanation,
        recognition_metadata=metadata,
    )


def _source_rank(result: RecognizerResult) -> int:
    source = detection_source(result)
    if source == SOURCE_STRUCTURAL and result.entity_type in _STRUCTURED_CONTAINER_TYPES:
        return 0
    if source == SOURCE_NATIVE_CREDENTIAL:
        return 1
    if source in {SOURCE_STRUCTURAL, SOURCE_CONTEXTUAL_CONTRACT}:
        return 2
    return 3


def _priority_key(result: RecognizerResult) -> tuple:
    source = detection_source(result)
    source_rank = _source_rank(result)
    specificity = _ENTITY_SPECIFICITY.get(result.entity_type, 50)
    metadata = result.recognition_metadata or {}
    recognizer_name = str(
        metadata.get(RecognizerResult.RECOGNIZER_NAME_KEY, "")
    )
    if source == SOURCE_CONTEXTUAL_CONTRACT:
        within_source_priority = (
            0 if recognizer_name == CONTRACT_CONTEXT_RECOGNIZER_NAME else 1,
            -float(result.score),
        )
    elif source in {SOURCE_NER, SOURCE_NATIVE_CREDENTIAL}:
        within_source_priority = (-float(result.score), specificity)
    else:
        within_source_priority = (specificity, -float(result.score))
    span_length = result.end - result.start
    span_priority = span_length if source == SOURCE_NATIVE_CREDENTIAL else -span_length
    return (
        source_rank,
        *within_source_priority,
        span_priority,
        result.start,
        result.end,
        result.entity_type,
        recognizer_name,
    )


def _conflict_reason(
    winner: RecognizerResult,
    loser: RecognizerResult,
) -> str:
    if (
        winner.start == loser.start
        and winner.end == loser.end
        and winner.entity_type == loser.entity_type
    ):
        return "exact_duplicate"
    if _source_rank(winner) != _source_rank(loser):
        return "overlap_preferred_source"
    if winner.entity_type != loser.entity_type:
        return "overlap_preferred_entity"
    if winner.score != loser.score:
        return "overlap_preferred_score"
    return "overlap_stable_order"


def _spans_overlap(
    left_start: int,
    left_end: int,
    right_start: int,
    right_end: int,
) -> bool:
    return left_start < right_end and right_start < left_end
