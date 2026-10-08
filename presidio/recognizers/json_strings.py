"""Deterministic detections in JSON strings, projected to original escape spans."""

import json
import re

from presidio_analyzer import EntityRecognizer, RecognizerResult

from entity_types import DETERMINISTIC_ENTITY_TYPES
from result_merging import DETECTION_SOURCE_METADATA_KEY, SOURCE_STRUCTURAL
from recognizers.credential_rules import (
    AuthTokenRecognizer, LoginRecognizer, PasswordRecognizer, SecretKeyRecognizer,
    _is_inert_credential_value,
)


MAX_JSON_CHARS = 65536
_STRING = re.compile(r'"(?:[^"\\]|\\.)*"', re.S)


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
        from recognizers import PLAIN_RECOGNIZERS
        from recognizers.base64_data import Base64DataRecognizer
        self._recognizers = [cls() for cls in PLAIN_RECOGNIZERS] + [Base64DataRecognizer()]
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
        results = []
        key = None
        value_start = None
        for match in _STRING.finditer(text):
            if text[match.end():].lstrip().startswith(":"):
                key = json.loads(match.group())
                delimiter = re.match(r"\s*:\s*", text[match.end():])
                value_start = match.end() + delimiter.end()
                continue
            decoded, offsets = decoded_string_offsets(text, match.start(), match.end())
            if match.start() == value_start and not _is_inert_credential_value(decoded):
                for recognizer in self._fields:
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
                requested = set(entities or self.supported_entities) & set(recognizer.supported_entities)
                if not requested:
                    continue
                for finding in recognizer.analyze(decoded, list(requested), None):
                    if finding.score < 0.35:
                        continue
                    metadata = dict(finding.recognition_metadata or {})
                    metadata["json_string_projection"] = True
                    # Presidio associates results with registered recognizer instances.
                    metadata[RecognizerResult.RECOGNIZER_NAME_KEY] = self.name
                    metadata[RecognizerResult.RECOGNIZER_IDENTIFIER_KEY] = self.id
                    results.append(RecognizerResult(
                        entity_type=finding.entity_type,
                        start=offsets[finding.start], end=offsets[finding.end],
                        score=finding.score, recognition_metadata=metadata,
                    ))
        return EntityRecognizer.remove_duplicates(results)
