"""Deterministic detections in JSON strings, projected to original escape spans."""

import json
import re
from array import array
from bisect import bisect_left, bisect_right
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass

from presidio_analyzer import EntityRecognizer, RecognizerResult

from entity_types import DETERMINISTIC_ENTITY_TYPES
from result_merging import DETECTION_SOURCE_METADATA_KEY, SOURCE_STRUCTURAL
from recognizers.credential_rules import (
    AuthTokenRecognizer, LoginRecognizer, PasswordRecognizer, SecretKeyRecognizer,
    _is_inert_credential_value,
)
from recognizers.ru_passport_ext import RuPassportExtRecognizer


MAX_JSON_CHARS = 65536
_STRING = re.compile(r'"(?:[^"\\]|\\.)*"', re.S)
_KEY_SEPARATOR = re.compile(r"\s*:")
_MEMBER_SEPARATOR = re.compile(r"\s*:\s*")
_UNSIGNED_INTEGER = re.compile(r'[0-9]+(?=[ \t\r\n]*[,}\]])')
_JSON_SCALAR = re.compile(
    r'"(?:[^"\\]+|\\.)*"|-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?'
    r'|true|false|null'
)
_JSON_ESCAPE = re.compile(r'\\(?:u[0-9A-Fa-f]{4}|.)')
_JSON_SURROGATE = re.compile(r'[\ud800-\udfff]')


def _reject_json_constant(value):
    raise ValueError(f"Invalid JSON constant: {value}")


def _character_offsets(token: str, start: int, check: Callable[[], None]) -> Sequence[int]:
    """Map decoded characters to whole source escapes, including surrogate pairs."""
    if "\\" not in token:
        return range(start + 1, start + len(token))
    offsets = array("Q", [start + 1])
    cursor = 1
    for index, escape in enumerate(_JSON_ESCAPE.finditer(token)):
        if index % 256 == 0:
            check()
        left, right = escape.span()
        if left < cursor:
            continue
        offsets.extend(range(start + cursor + 1, start + left + 1))
        if token[left + 1] == "u" and token[right:right + 2] == "\\u":
            if (0xD800 <= int(token[left + 2:right], 16) <= 0xDBFF
                    and 0xDC00 <= int(token[right + 2:right + 6], 16) <= 0xDFFF):
                right += 6
        offsets.append(start + right)
        cursor = right
    offsets.extend(range(start + cursor + 1, start + len(token)))
    return offsets


@dataclass(frozen=True)
class JsonArgumentScalar:
    decoded: str | None
    compact_offsets: Sequence[int]
    source_offsets: Sequence[int]
    key: str | None = None
    is_key: bool = False
    is_number: bool = False


@dataclass(frozen=True)
class JsonArgumentView:
    """Analysis-only JSON spelling; outgoing JSON is always edited at source offsets."""

    text: str
    scalars: tuple[JsonArgumentScalar, ...]
    starts: tuple[int, ...]

    @classmethod
    def parse(cls, source: str, check: Callable[[], None]) -> "JsonArgumentView | None":
        if not source.lstrip(" \t\r\n").startswith(("{", "[")):
            return None
        check()
        try:
            json.loads(source, parse_int=str, parse_float=str, parse_constant=_reject_json_constant)
        except (ValueError, RecursionError):
            return None
        check()
        parts = []
        scalars = []
        cursor = compact_start = member_start = 0
        key = None
        for match in _JSON_SCALAR.finditer(source):
            check()
            start, end = match.span()
            token = match.group()
            parts.append(source[cursor:start])
            compact_start += start - cursor
            if token.startswith('"'):
                decoded = json.loads(token)
                if _JSON_SURROGATE.search(decoded):
                    return None  # Unpaired surrogates stay on the legacy path.
                compact = json.dumps(decoded, ensure_ascii=False)
                member = _MEMBER_SEPARATOR.match(source, end)
                scalar = JsonArgumentScalar(
                    decoded, _character_offsets(compact, compact_start, check),
                    _character_offsets(token, start, check),
                    key=key if start == member_start else None, is_key=member is not None,
                )
                if member is not None:
                    key, member_start = decoded, member.end()
            else:
                compact = token
                scalar = JsonArgumentScalar(
                    None, range(compact_start, compact_start + len(token) + 1),
                    range(start, end + 1), is_number=token[0] in "-0123456789",
                )
            scalars.append(scalar)
            parts.append(compact)
            compact_start += len(compact)
            cursor = end
        parts.append(source[cursor:])
        return cls("".join(parts), tuple(scalars), tuple(s.compact_offsets[0] for s in scalars))

    def project(self, start: int, end: int) -> Iterator[tuple[int, int]]:
        """Keep every touched scalar, never JSON delimiters or half an escape."""
        index = max(0, bisect_right(self.starts, start) - 1)
        while index < len(self.scalars) and self.starts[index] < end:
            scalar = self.scalars[index]
            offsets = scalar.compact_offsets
            left, right = max(start, offsets[0]), min(end, offsets[-1])
            if left < right:
                if scalar.decoded is None and not scalar.is_number:
                    raise ValueError("Unsupported JSON literal matched")
                if scalar.is_number:
                    yield scalar.source_offsets[0], scalar.source_offsets[-1]
                else:
                    yield (scalar.source_offsets[bisect_right(offsets, left) - 1],
                           scalar.source_offsets[bisect_left(offsets, right)])
            index += 1


def iter_json_string_values(text):
    """Yield bounded valid JSON values with their original character offsets."""
    if len(text) > MAX_JSON_CHARS or not text.lstrip().startswith(("{", "[", '"')):
        return
    try:
        json.loads(text, parse_constant=_reject_json_constant)
    except (ValueError, RecursionError):
        return
    for match in _STRING.finditer(text):
        if _KEY_SEPARATOR.match(text, match.end()):
            continue
        yield decoded_string_offsets(text, match.start(), match.end())


def iter_json_keyed_strings(text, *, integers=False):
    """Yield string members, optionally also unsigned integer source literals."""
    if len(text) > MAX_JSON_CHARS or not text.lstrip().startswith(("{", "[", '"')):
        return
    try:
        json.loads(text, parse_constant=_reject_json_constant)
    except (ValueError, RecursionError):
        return
    key = None
    value_start = None
    for match in _STRING.finditer(text):
        separator = _MEMBER_SEPARATOR.match(text, match.end())
        if separator:
            key = json.loads(match.group())
            value_start = separator.end()
            if integers:
                number = _UNSIGNED_INTEGER.match(text, value_start)
                if number:
                    yield key, number.group(), range(number.start(), number.end() + 1)
            continue
        if match.start() == value_start:
            yield (key, *decoded_string_offsets(text, match.start(), match.end()))


def decoded_string_offsets(text, start, end):
    """Decode a validated JSON string while retaining each character boundary."""
    cursor = start + 1
    offsets = [cursor]
    parts = []
    while cursor < end - 1:
        length = 1
        if text[cursor] == "\\":
            length = 6 if text[cursor + 1] == "u" else 2
            if length == 6 and 0xD800 <= int(text[cursor + 2:cursor + 6], 16) <= 0xDBFF:
                if text[cursor + 6:cursor + 8] == "\\u" and 0xDC00 <= int(text[cursor + 8:cursor + 12], 16) <= 0xDFFF:
                    length = 12
            part = json.loads('"' + text[cursor:cursor + length] + '"')
        else:
            part = text[cursor]
        parts.append(part)
        cursor += length
        offsets.append(cursor)
    return "".join(parts), offsets


class JsonStringRecognizer(EntityRecognizer):
    """Inspect decoded values once, without evaluating code or invoking NER."""

    def __init__(self, supported_language="ru"):
        super().__init__(
            supported_entities=sorted(DETERMINISTIC_ENTITY_TYPES),
            supported_language=supported_language,
            name="JsonStringRecognizer",
            version="1.0.0",
        )
        from recognizers import PLAIN_RECOGNIZERS, ADDITIVE_RECOGNIZERS
        from recognizers.base64_data import Base64DataRecognizer
        self._recognizers = [cls() for cls in [*PLAIN_RECOGNIZERS, *ADDITIVE_RECOGNIZERS]] + [Base64DataRecognizer()]
        self._fields = [PasswordRecognizer(), SecretKeyRecognizer(), AuthTokenRecognizer(), LoginRecognizer(), Base64DataRecognizer()]

    def load(self):
        pass

    def analyze(self, text, entities, nlp_artifacts=None):
        if len(text) > MAX_JSON_CHARS or not text.lstrip().startswith(("{", "[", '"')):
            return []
        try:
            json.loads(text)
        except (ValueError, RecursionError):
            return []
        return self._analyze_values(self._iter_legacy_values(text), entities)

    @staticmethod
    def _iter_legacy_values(text):
        key = None
        value_start = None
        for match in _STRING.finditer(text):
            delimiter = _MEMBER_SEPARATOR.match(text, match.end())
            if delimiter:
                key = json.loads(match.group())
                value_start = delimiter.end()
                continue
            decoded, offsets = decoded_string_offsets(text, match.start(), match.end())
            yield decoded, offsets, key if match.start() == value_start else None

    def analyze_arguments(self, view, entities, check):
        def values():
            for scalar in view.scalars:
                check()
                if scalar.decoded is not None and not scalar.is_key:
                    yield scalar.decoded, scalar.compact_offsets, scalar.key

        return self._analyze_values(values(), entities, check)

    def _analyze_values(self, values, entities, check=lambda: None):
        results = []
        for decoded, offsets, key in values:
            if key is not None and decoded:
                for recognizer in self._fields:
                    accepted = not _is_inert_credential_value(decoded)
                    if isinstance(recognizer, PasswordRecognizer):
                        accepted = accepted or recognizer._accept_match(
                            decoded, "credential.kv.password", json.dumps(decoded), ""
                        ) or not decoded.strip()
                    if not accepted:
                        continue
                    if not set(recognizer.supported_entities) & set(entities or self.supported_entities):
                        continue
                    for finding in recognizer.analyze(f'{key}="synthetic-value-123"', entities):
                        results.append(RecognizerResult(
                            entity_type=finding.entity_type, start=offsets[0], end=offsets[-1],
                            score=0.95,
                            recognition_metadata={
                                DETECTION_SOURCE_METADATA_KEY: SOURCE_STRUCTURAL,
                                "json_string_projection": True, "encoded_container": True,
                                RecognizerResult.RECOGNIZER_NAME_KEY: self.name,
                                RecognizerResult.RECOGNIZER_IDENTIFIER_KEY: self.id,
                            },
                        ))
            for recognizer in self._recognizers:
                check()
                requested = set(entities or self.supported_entities) & set(recognizer.supported_entities)
                if not requested:
                    continue
                for finding in recognizer.analyze(decoded, list(requested), None):
                    # An explicit passport key can confirm its own scalar, not
                    # another member/array item encountered later in the source.
                    if (isinstance(recognizer, RuPassportExtRecognizer)
                            and finding.score < 0.35 and key is not None
                            and recognizer._CONTEXT_RE.search(key)):
                        finding.score += 0.35
                    if finding.score < 0.35:
                        continue
                    metadata = dict(finding.recognition_metadata or {})
                    metadata["json_string_projection"] = True
                    metadata["projected_recognizer_name"] = recognizer.name
                    # Presidio associates results with registered recognizer instances.
                    metadata[RecognizerResult.RECOGNIZER_NAME_KEY] = self.name
                    metadata[RecognizerResult.RECOGNIZER_IDENTIFIER_KEY] = self.id
                    results.append(RecognizerResult(
                        entity_type=finding.entity_type,
                        start=offsets[finding.start], end=offsets[finding.end],
                        score=finding.score, recognition_metadata=metadata,
                    ))
        return EntityRecognizer.remove_duplicates(results)
