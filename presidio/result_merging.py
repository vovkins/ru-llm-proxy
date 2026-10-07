"""Source-aware merging for overlapping Analyzer results."""

from __future__ import annotations

import re
from bisect import bisect_left, bisect_right
from dataclasses import dataclass
from functools import lru_cache
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
_NAME_TYPES = frozenset({"PERSON", "ORGANIZATION"})
_LEGAL_FORM_RE = re.compile(r"(?:ООО|АО|ПАО|ОАО|ЗАО)", re.IGNORECASE)
_NAME_WORDS_RE = re.compile(r"[А-Яа-яЁё]+(?:[- \t\u00a0][А-Яа-яЁё]+){1,2}")
_PERSON_FIELD_RE = re.compile(r"(?i)(?<!\w)(?:фио|ф\.и\.о\.)\s*[:=]\s*$")
_PERSON_LABEL_RE = re.compile(
    r"(?im)(?:^|(?<=[;\r]))[ \t\u00a0]{0,16}"
    r"(?P<label>фио|ф\.и\.о\.)[ \t\u00a0]{0,16}[:=][ \t\u00a0]{0,16}"
)
_NAME_WORD = r"[А-ЯЁ][А-Яа-яЁё]{1,39}(?:-[А-ЯЁ][А-Яа-яЁё]{1,39})?"
_NAME_VALUE = rf"{_NAME_WORD}(?:[ \t\u00a0]{{1,16}}{_NAME_WORD}){{1,2}}"
_EXPLICIT_NAME_RE = re.compile(
    rf"(?<!\w)(?:"
    rf"(?P<person>(?i:фио|ф\.и\.о\.))[ \t\u00a0]{{0,16}}[:=]|"
    rf"(?P<organization>(?i:название организации))[ \t\u00a0]{{0,16}}[:=]|"
    rf"(?P<legal>(?i:ООО|АО|ПАО|ОАО|ЗАО))"
    rf")[ \t\u00a0]{{1,16}}(?P<value>{_NAME_VALUE})(?![\w-])"
)
_COMPANY_NAME_RE = re.compile(
    rf"(?<!\w)(?i:компания)[ \t\u00a0]{{1,16}}"
    rf"(?P<value>{_NAME_VALUE})(?![\w-])"
)
_NAME_FRAGMENT_TYPES = _NAME_TYPES | {"LOGIN"}
_SERVICE_NUMBER_RE = re.compile(
    r"(?i)(?<!\w)(?:номер|идентификатор)\s+"
    r"(?:заявки|заказа|строки)\s*[:=#№]?\s*$"
)


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


@dataclass(frozen=True)
class NameContext:
    """Bounded grammar evidence; never sufficient for a new detection alone."""

    entity_type: str
    start: int
    end: int
    legal_form_end: int | None = None


def name_context_evidence(text: str) -> tuple[NameContext, ...]:
    contexts = []
    for pattern in (_EXPLICIT_NAME_RE, _COMPANY_NAME_RE):
        for match in pattern.finditer(text):
            start, end = match.span("value")
            # Never interpret a truncated three-word prefix of a longer name.
            tail = text[end : end + 18]
            if re.match(r"[ \t\u00a0]{1,16}[А-ЯЁ]", tail):
                continue
            if pattern is _COMPANY_NAME_RE:
                prefix = text[max(0, match.start() - 64) : match.start()].rstrip(" \t\u00a0")
                if not prefix and match.start() > 64:
                    continue
                if prefix and prefix[-1] not in ".!?;\r\n":
                    continue
                entity_type = "ORGANIZATION"
                legal_end = None
            else:
                entity_type = "PERSON" if match.group("person") else "ORGANIZATION"
                legal_end = match.end("legal") if match.group("legal") else None
                if entity_type == "PERSON" and any(
                    _LEGAL_FORM_RE.fullmatch(word) for word in text[start:end].split()
                ):
                    continue
                if legal_end is not None:
                    start = match.start("legal")
            if end - start <= 128:
                contexts.append(NameContext(entity_type, start, end, legal_end))
    return tuple(dict.fromkeys(contexts))


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

        context_start = max(0, start - 96)
        for index in range(start - 1, context_start - 1, -1):
            if _is_sentence_boundary(text, index):
                context_start = index + 1
                break
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
    sentence_starts: Iterable[int] = (),
) -> MergeOutcome:
    """Merge overlapping results using source and entity specificity."""
    if requested_entities is not None:
        requested_entities = tuple(requested_entities)
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

    valid_results, name_decisions = _refine_names(
        text, valid_results, frozenset(sentence_starts), score_threshold
    )
    decisions.extend(name_decisions)
    if requested_entities:
        requested = frozenset(requested_entities)
        valid_results = [r for r in valid_results if r.entity_type in requested]
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


def _copy_name(
    result: RecognizerResult,
    *,
    entity_type: str | None = None,
    start: int | None = None,
    end: int | None = None,
    score: float | None = None,
) -> RecognizerResult:
    return RecognizerResult(
        entity_type=entity_type or result.entity_type,
        start=result.start if start is None else start,
        end=result.end if end is None else end,
        score=result.score if score is None else score,
        analysis_explanation=result.analysis_explanation,
        recognition_metadata=dict(result.recognition_metadata or {}),
    )


@lru_cache(maxsize=1)
def _russian_morphology():
    # Already installed by the Russian spaCy model; no second NER inference.
    import pymorphy3

    return pymorphy3.MorphAnalyzer()


def _is_unambiguous_imperative(word: str) -> bool:
    if not re.fullmatch(r"[А-Яа-яЁё]{2,32}", word):
        return False
    parses = _russian_morphology().parse(word)
    return bool(parses) and all("impr" in parse.tag for parse in parses)


def _refine_names(
    text: str,
    results: list[RecognizerResult],
    sentence_starts: frozenset[int],
    score_threshold: float,
) -> tuple[list[RecognizerResult], list[MergeDecision]]:
    decisions = []
    boundaries = sorted(sentence_starts)
    unique_names = {}
    retained = []
    for result in results:
        if (
            detection_source(result) != SOURCE_NER
            or result.entity_type not in _NAME_TYPES
        ):
            retained.append(result)
            continue
        key = (result.entity_type, result.start, result.end)
        previous = unique_names.get(key)
        if previous is not None:
            decisions.append(MergeDecision("exact_duplicate", SOURCE_NER, SOURCE_NER))
        if previous is None or _priority_key(result) < _priority_key(previous):
            unique_names[key] = result
    results = [*retained, *unique_names.values()]
    results, context_decisions = _refine_explicit_name_values(
        text, results, score_threshold
    )
    decisions.extend(context_decisions)
    refined = []
    for result in results:
        if (
            detection_source(result) != SOURCE_NER
            or result.entity_type not in _NAME_TYPES
        ):
            refined.append(result)
            continue
        value = text[result.start : result.end]
        if value.isdecimal():
            start = result.start
            lower_bound = max(0, start - 64)
            while start > lower_bound and text[start - 1].isdecimal():
                start -= 1
            complete_prefix = not start or not text[start - 1].isdecimal()
            if complete_prefix and _SERVICE_NUMBER_RE.search(
                text[max(0, start - 64) : start]
            ):
                decisions.append(
                    MergeDecision(
                        "name_service_number_suppressed", SOURCE_NONE, SOURCE_NER
                    )
                )
                continue
        if result.entity_type == "ORGANIZATION":
            if (
                _PERSON_FIELD_RE.search(text[max(0, result.start - 32) : result.start])
                and _NAME_WORDS_RE.fullmatch(value)
                and not any(_LEGAL_FORM_RE.fullmatch(word) for word in value.split())
            ):
                result = _copy_name(result, entity_type="PERSON")
                decisions.append(
                    MergeDecision("name_person_field", SOURCE_NER, SOURCE_NER)
                )
            elif _LEGAL_FORM_RE.fullmatch(value.split()[0] if value.split() else ""):
                # A period alone is not a boundary: require spaCy's sentence
                # start and an unambiguous dictionary imperative in the tail.
                for boundary in boundaries[
                    bisect_right(boundaries, result.start) : bisect_right(
                        boundaries, result.end - 1
                    )
                ]:
                    before = text[result.start : boundary].rstrip()
                    if not before.endswith("."):
                        continue
                    tail = text[boundary : result.end].strip()
                    if not _is_unambiguous_imperative(tail):
                        continue
                    end = result.start + len(before) - 1
                    while end > result.start and text[end - 1].isspace():
                        end -= 1
                    if len(text[result.start : end].split()) < 2 or any(
                        other is not result
                        and other.entity_type != "ORGANIZATION"
                        and _spans_overlap(boundary, result.end, other.start, other.end)
                        for other in results
                    ):
                        continue
                    result = _copy_name(result, end=end)
                    decisions.append(
                        MergeDecision("name_instruction_tail", SOURCE_NER, SOURCE_NER)
                    )
                    break
        refined.append(result)

    names = sorted(
        (
            r
            for r in refined
            if detection_source(r) == SOURCE_NER and r.entity_type in _NAME_TYPES
        ),
        key=lambda r: (r.start, r.end, -r.score),
    )
    removed = set()
    replacements = {}
    for left, right in zip(names, names[1:]):
        if id(left) in removed or id(right) in removed:
            continue
        if not (
            left.entity_type == "ORGANIZATION"
            and _LEGAL_FORM_RE.fullmatch(text[left.start : left.end])
            and right.entity_type == "PERSON"
            and left.end < right.start
            and right.start - left.end <= 16
            and text[left.end : right.start].isspace()
            and not any(c in "\r\n" for c in text[left.end : right.start])
            and right.end - right.start <= 128
            and _NAME_WORDS_RE.fullmatch(text[right.start : right.end])
        ):
            continue
        # Do not bridge another sensitive category or steal its protected span.
        if any(
            other is not left
            and other is not right
            and _spans_overlap(left.start, right.end, other.start, other.end)
            for other in refined
        ):
            continue
        replacements[id(left)] = _copy_name(
            left, end=right.end, score=min(left.score, right.score)
        )
        removed.add(id(right))
        decisions.append(MergeDecision("name_legal_form_join", SOURCE_NER, SOURCE_NER))
    return [
        replacements.get(id(r), r) for r in refined if id(r) not in removed
    ], decisions


def _refine_explicit_name_values(
    text: str, results: list[RecognizerResult], score_threshold: float
) -> tuple[list[RecognizerResult], list[MergeDecision]]:
    decisions = []
    if not any(
        detection_source(r) == SOURCE_NER and r.entity_type in _NAME_TYPES
        for r in results
    ):
        return results, decisions
    # Only a separate field header may be discarded, never its value or a
    # similarly named organization in prose. BIO may include the separator.
    label_spans = {
        match.start("label"): (match.end("label"), match.end())
        for match in _PERSON_LABEL_RE.finditer(text)
    }
    retained = []
    for result in results:
        if (
            detection_source(result) == SOURCE_NER
            and result.entity_type in _NAME_TYPES
            and (bounds := label_spans.get(result.start)) is not None
            and bounds[0] <= result.end <= bounds[1]
        ):
            decisions.append(MergeDecision("name_field_label", SOURCE_NONE, SOURCE_NER))
        else:
            retained.append(result)
    ordered = sorted(retained, key=lambda r: (r.start, r.end))
    starts = [r.start for r in ordered]
    max_ends = []
    for r in ordered:
        max_ends.append(max(r.end, max_ends[-1] if max_ends else 0))
    removed = set()
    replacements = []
    previous_end = -1
    for context in sorted(name_context_evidence(text), key=lambda c: (c.start, -c.end)):
        if context.start < previous_end:
            continue
        previous_end = context.end
        overlapping = [
            r for r in ordered[
                bisect_right(max_ends, context.start) : bisect_left(starts, context.end)
            ]
            if _spans_overlap(context.start, context.end, r.start, r.end)
        ]
        fragments = [
            r for r in overlapping
            if detection_source(r) == SOURCE_NER
            and r.entity_type in _NAME_FRAGMENT_TYPES
            and r.score >= score_threshold
            and context.start <= r.start < r.end <= context.end
        ]
        if not fragments or not any(r.entity_type in _NAME_TYPES for r in fragments):
            continue
        legal_prefix = [
            r for r in fragments
            if r.entity_type == "ORGANIZATION"
            and r.start == context.start and r.end == context.legal_form_end
        ]
        removable_location = [
            r for r in overlapping
            if legal_prefix and r.entity_type == "LOCATION"
            and detection_source(r) == SOURCE_STRUCTURAL
            and r.start == context.start and r.end == context.legal_form_end
        ]
        consumed = {id(r) for r in [*fragments, *removable_location]}
        if any(id(r) not in consumed for r in overlapping):
            continue
        # All letters must already be accepted NER evidence. Only whitespace
        # may be bridged, and a competing native credential always vetoes this.
        covered = bytearray(context.end - context.start)
        for fragment in fragments:
            covered[fragment.start - context.start : fragment.end - context.start] = (
                b"\x01" * (fragment.end - fragment.start)
            )
        if any(
            not covered[index] and not character.isspace()
            for index, character in enumerate(text[context.start : context.end])
        ):
            continue
        anchor = min(legal_prefix or fragments, key=_priority_key)
        merged = _copy_name(
            anchor, entity_type=context.entity_type,
            start=context.start, end=context.end,
            score=min(r.score for r in fragments),
        )
        removed.update(consumed)
        replacements.append(merged)
        if removable_location:
            decisions.append(
                MergeDecision("name_legal_prefix", SOURCE_NER, SOURCE_STRUCTURAL)
            )
        decisions.append(MergeDecision("name_explicit_value", SOURCE_NER, SOURCE_NER))
    return [r for r in retained if id(r) not in removed] + replacements, decisions


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
