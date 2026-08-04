"""Load and validate the sanitized NER migration corpus."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable


TARGET_ENTITY_TYPES = frozenset(
    {
        "PERSON",
        "LOCATION",
        "ORGANIZATION",
        "LOGIN",
        "PASSWORD",
        "AUTH_TOKEN",
        "SECRET_KEY",
        "CONTRACT_NUMBER",
    }
)

DEFAULT_CORPUS_PATH = Path(__file__).with_name("data") / "ner_migration.jsonl"
_CASE_ID_RE = re.compile(r"^[a-z][a-z0-9_]*$")
_MARKER_RE = re.compile(
    r"\{\{(?P<entity>[A-Z][A-Z0-9_]*):(?P<value>.*?)\}\}",
    re.DOTALL,
)
_ALLOWED_RECORD_FIELDS = {
    "id",
    "annotated_text",
    "critical",
    "tags",
    "language",
    "score_threshold",
    "prefix_repeat",
    "suffix_repeat",
}


@dataclass(frozen=True, order=True)
class EntitySpan:
    """One entity interval using Python character offsets."""

    entity_type: str
    start: int
    end: int


@dataclass(frozen=True)
class CorpusCase:
    """One sanitized Analyzer evaluation case."""

    case_id: str
    text: str
    expected: tuple[EntitySpan, ...]
    critical: bool
    tags: tuple[str, ...]
    language: str = "ru"
    score_threshold: float = 0.35


def parse_annotated_text(annotated_text: str) -> tuple[str, tuple[EntitySpan, ...]]:
    """Strip ``{{ENTITY:value}}`` markers and derive original-text spans."""
    if not isinstance(annotated_text, str) or not annotated_text:
        raise ValueError("annotated_text must be a non-empty string")

    plain_parts: list[str] = []
    expected: list[EntitySpan] = []
    source_cursor = 0
    plain_length = 0

    for match in _MARKER_RE.finditer(annotated_text):
        prefix = annotated_text[source_cursor : match.start()]
        if "{{" in prefix or "}}" in prefix:
            raise ValueError("malformed annotation marker")
        plain_parts.append(prefix)
        plain_length += len(prefix)

        entity_type = match.group("entity")
        value = match.group("value")
        if entity_type not in TARGET_ENTITY_TYPES:
            raise ValueError(f"unsupported target entity type: {entity_type}")
        if not value:
            raise ValueError(f"empty value for entity type: {entity_type}")
        if "{{" in value or "}}" in value:
            raise ValueError("nested annotation markers are not supported")

        start = plain_length
        plain_parts.append(value)
        plain_length += len(value)
        expected.append(EntitySpan(entity_type=entity_type, start=start, end=plain_length))
        source_cursor = match.end()

    suffix = annotated_text[source_cursor:]
    if "{{" in suffix or "}}" in suffix:
        raise ValueError("malformed annotation marker")
    plain_parts.append(suffix)

    plain_text = "".join(plain_parts)
    return plain_text, tuple(expected)


def _expand_repeat(record: dict[str, Any], field: str) -> str:
    repeat = record.get(field)
    if repeat is None:
        return ""
    if not isinstance(repeat, dict):
        raise ValueError(f"{field} must be an object")
    if set(repeat) != {"text", "count"}:
        raise ValueError(f"{field} must contain exactly text and count")

    text = repeat["text"]
    count = repeat["count"]
    if not isinstance(text, str) or not text:
        raise ValueError(f"{field}.text must be a non-empty string")
    if isinstance(count, bool) or not isinstance(count, int) or not 1 <= count <= 1000:
        raise ValueError(f"{field}.count must be an integer from 1 to 1000")
    return text * count


def _parse_tags(raw_tags: Any) -> tuple[str, ...]:
    if not isinstance(raw_tags, list) or not raw_tags:
        raise ValueError("tags must be a non-empty list")
    if any(not isinstance(tag, str) or not tag for tag in raw_tags):
        raise ValueError("tags must contain non-empty strings")
    if len(set(raw_tags)) != len(raw_tags):
        raise ValueError("tags must not contain duplicates")

    tags = tuple(raw_tags)
    polarity = {"positive", "negative"}.intersection(tags)
    if len(polarity) != 1:
        raise ValueError("tags must contain exactly one of positive or negative")
    return tags


def _parse_record(record: Any, *, line_number: int) -> CorpusCase:
    if not isinstance(record, dict):
        raise ValueError(f"line {line_number}: corpus record must be an object")
    unknown_fields = set(record) - _ALLOWED_RECORD_FIELDS
    if unknown_fields:
        fields = ", ".join(sorted(unknown_fields))
        raise ValueError(f"line {line_number}: unknown fields: {fields}")

    case_id = record.get("id")
    if not isinstance(case_id, str) or not _CASE_ID_RE.fullmatch(case_id):
        raise ValueError(f"line {line_number}: invalid case id")

    tags = _parse_tags(record.get("tags"))
    annotated_text = record.get("annotated_text")
    if not isinstance(annotated_text, str) or not annotated_text:
        raise ValueError(f"line {line_number}: annotated_text must be non-empty")
    prefix = _expand_repeat(record, "prefix_repeat")
    suffix = _expand_repeat(record, "suffix_repeat")
    text, expected = parse_annotated_text(f"{prefix}{annotated_text}{suffix}")

    if "positive" in tags and not expected:
        raise ValueError(f"line {line_number}: positive case has no entities")
    if "negative" in tags and expected:
        raise ValueError(f"line {line_number}: negative case has entities")

    critical = record.get("critical", False)
    if not isinstance(critical, bool):
        raise ValueError(f"line {line_number}: critical must be boolean")
    if critical and not expected:
        raise ValueError(f"line {line_number}: critical case has no entities")

    language = record.get("language", "ru")
    if not isinstance(language, str) or not language:
        raise ValueError(f"line {line_number}: language must be non-empty")

    score_threshold = record.get("score_threshold", 0.35)
    if isinstance(score_threshold, bool) or not isinstance(score_threshold, (int, float)):
        raise ValueError(f"line {line_number}: score_threshold must be numeric")
    score_threshold = float(score_threshold)
    if not 0 <= score_threshold <= 1:
        raise ValueError(f"line {line_number}: score_threshold must be from 0 to 1")

    return CorpusCase(
        case_id=case_id,
        text=text,
        expected=expected,
        critical=critical,
        tags=tags,
        language=language,
        score_threshold=score_threshold,
    )


def validate_corpus(cases: Iterable[CorpusCase]) -> tuple[CorpusCase, ...]:
    """Validate collection-wide corpus invariants."""
    normalized = tuple(cases)
    if not normalized:
        raise ValueError("corpus must not be empty")

    case_ids = [case.case_id for case in normalized]
    if len(set(case_ids)) != len(case_ids):
        raise ValueError("corpus case ids must be unique")

    covered_types = {
        entity.entity_type
        for case in normalized
        for entity in case.expected
    }
    missing_types = TARGET_ENTITY_TYPES - covered_types
    if missing_types:
        missing = ", ".join(sorted(missing_types))
        raise ValueError(f"corpus does not cover target entity types: {missing}")

    if not any("negative" in case.tags for case in normalized):
        raise ValueError("corpus must contain negative cases")
    if not any(case.critical for case in normalized):
        raise ValueError("corpus must contain critical cases")

    for case in normalized:
        for entity in case.expected:
            if not 0 <= entity.start < entity.end <= len(case.text):
                raise ValueError(f"invalid entity span in case {case.case_id}")
    return normalized


def load_corpus(path: Path | str = DEFAULT_CORPUS_PATH) -> tuple[CorpusCase, ...]:
    """Load a JSONL corpus and validate all records."""
    corpus_path = Path(path)
    cases: list[CorpusCase] = []
    with corpus_path.open("r", encoding="utf-8") as corpus_file:
        for line_number, raw_line in enumerate(corpus_file, start=1):
            if not raw_line.strip():
                continue
            try:
                record = json.loads(raw_line)
                cases.append(_parse_record(record, line_number=line_number))
            except (json.JSONDecodeError, ValueError) as exc:
                raise ValueError(f"invalid corpus at line {line_number}: {exc}") from exc
    return validate_corpus(cases)
