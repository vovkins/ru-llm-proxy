"""Context-first Base64 detection with bounded content inspection.

The optional logistic policy adds a document-level cascade on top of it.
"""

import base64
import binascii
import os
import re

from presidio_analyzer import EntityRecognizer, RecognizerResult

from result_merging import DETECTION_SOURCE_METADATA_KEY, SOURCE_STRUCTURAL


MAX_CANDIDATE_CHARS = 16384
MAX_DECODED_BYTES = 65536
MAX_CANDIDATES = 64
POLICY_ENV = "PRESIDIO_ANALYZER_BASE64_POLICY"


def load_policy(environ=None):
    env = os.environ if environ is None else environ
    policy = env.get(POLICY_ENV, "content").strip().lower() or "content"
    if policy not in {"content", "logistic"}:
        raise ValueError(f"{POLICY_ENV} must be content or logistic")
    return policy

_KEY = r"(?:base64|b64|[A-Za-z][A-Za-z0-9_.-]{0,80}[_-](?:base64|b64))"
_CONTEXT = re.compile(
    rf"(?i)(?<![\w.-])[\"']?(?P<key>{_KEY})[\"']?[ \t]*[:=][ \t]*"
    r'(?P<value>"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'|'
    r"(?:\r?\n[ \t]*)?[^\s\"',;}\]]+"
    r"(?:\r?\n[ \t]+[A-Za-z0-9_+/-]+={0,2}(?=[ \t]*(?:\r?\n|$)))*)"
)
_CANDIDATE = re.compile(
    r"(?<![\w+/=-])[A-Za-z0-9_+/-]{8,}={0,2}(?![\w+/=-])"
)


def decode_candidate(value):
    """Require canonical standard/URL-safe spelling; never guess a text encoding."""
    if len(value) > MAX_CANDIDATE_CHARS or len(value) % 4 == 1:
        return None
    if "=" in value and len(value) % 4:
        return None
    if ("+" in value or "/" in value) and ("-" in value or "_" in value):
        return None
    try:
        padded = value + "=" * (-len(value) % 4)
        decoded = base64.b64decode(padded, altchars=b"-_", validate=True)
        canonical = base64.urlsafe_b64encode(decoded) if ("-" in value or "_" in value) else base64.b64encode(decoded)
        if canonical.decode("ascii").rstrip("=") != value.rstrip("="):
            return None
        if "=" in value and canonical.decode("ascii") != value:
            return None
        return decoded
    except (ValueError, binascii.Error):
        return None


class Base64DataRecognizer(EntityRecognizer):
    """Mask declared encodings; infer undeclared ones from sensitive content.

    With the logistic policy, analyze_document() additionally finds Base64 by
    form, regardless of what it contains.
    """

    def __init__(self, supported_language="ru", policy=None):
        super().__init__(
            supported_entities=["BASE64_DATA", "LOGIN", "PASSWORD", "AUTH_TOKEN", "SECRET_KEY"],
            supported_language=supported_language,
            name="Base64DataRecognizer",
            version="1.1.0",
        )
        self.policy = load_policy() if policy is None else load_policy({POLICY_ENV: policy})
        # Lazy import avoids registry cycles and never includes this recognizer or NER.
        from recognizers import PLAIN_RECOGNIZERS
        self._content_recognizers = [
            cls() for cls in PLAIN_RECOGNIZERS
        ]
        from recognizers.credential_rules import (
            AuthTokenRecognizer, LoginRecognizer, PasswordRecognizer, SecretKeyRecognizer,
        )
        self._typed_recognizers = (
            PasswordRecognizer(), SecretKeyRecognizer(), AuthTokenRecognizer(), LoginRecognizer(),
        )

    def load(self):
        pass

    def analyze(self, text, entities, nlp_artifacts=None):
        requested = set(entities or self.supported_entities)
        declared_results = self.analyze_declared(text, self.supported_entities)
        results = [r for r in declared_results if r.entity_type in requested]
        declared = [(r.start, r.end) for r in declared_results]

        attempts = 0
        decoded_bytes = 0
        for match in _CANDIDATE.finditer(text) if "BASE64_DATA" in requested else ():
            if any(start <= match.start() and match.end() <= end for start, end in declared):
                continue
            value = match.group()
            if len(value) > MAX_CANDIDATE_CHARS:
                continue
            if attempts >= MAX_CANDIDATES or decoded_bytes >= MAX_DECODED_BYTES:
                break
            attempts += 1
            decoded = decode_candidate(value)
            if decoded is None:
                continue
            decoded_bytes += len(decoded)
            if decoded_bytes > MAX_DECODED_BYTES:
                break
            try:
                inner = decoded.decode("utf-8")
            except UnicodeDecodeError:
                continue
            if any(ord(character) < 32 and character not in "\r\n\t" for character in inner):
                continue
            if self._has_sensitive_content(inner):
                results.append(self._result(match.start(), match.end(), "base64.sensitive-content"))
        return EntityRecognizer.remove_duplicates(results)

    def analyze_declared(self, text, entities):
        """Find complete declared values without decoding or running content rules."""
        requested = set(entities or self.supported_entities)
        results = []
        for match in _CONTEXT.finditer(text):
            start, end = match.span("value")
            if text[start] in "\"'":
                start, end = start + 1, end - 1
            while start < end and text[start].isspace():
                start += 1
            while end > start and text[end - 1].isspace():
                end -= 1
            if start < end:
                key = re.sub(r"[_-](?:base64|b64)$", "", match.group("key"), flags=re.I)
                entity_type = "BASE64_DATA"
                for recognizer in self._typed_recognizers:
                    if recognizer.analyze(f'{key}="synthetic-value-123"', recognizer.supported_entities):
                        entity_type = recognizer.supported_entities[0]
                        break
                if entity_type in requested:
                    results.append(self._result(start, end, "base64.declared", entity_type))
        return results

    def analyze_document(self, text, entities):
        """Resolve complete source containers before overlapping chunk scans.

        The logistic cascade runs only here: once per document and outside
        Presidio's per-chunk duplicate removal, whose cost grows with the square
        of the number of findings. Chunk-level analyze() keeps the content
        policy, so everything protected by content stays protected.
        """
        requested = set(entities or self.supported_entities)
        declared_results = self.analyze_declared(text, self.supported_entities)
        results = [r for r in declared_results if r.entity_type in requested]
        if self.policy != "logistic" or "BASE64_DATA" not in requested:
            return results
        from recognizers.base64_logistic import find_spans, merge_spans, subtract_spans
        from recognizers.json_strings import iter_json_string_values

        spans = find_spans(text)
        # JSON escapes (\u003d, \") split values in the raw text; scan decoded strings too.
        for decoded, offsets in iter_json_string_values(text):
            spans.extend((offsets[start], offsets[end]) for start, end in find_spans(decoded))
        # Declared values of every type keep their span and class, even when the
        # request excludes that class.
        declared = [(r.start, r.end) for r in declared_results]
        for start, end in subtract_spans(text, merge_spans(spans), declared):
            results.append(
                self._result(start, end, "base64.logistic-cascade", encoded_container=False)
            )
        return results

    def _has_sensitive_content(self, text):
        for recognizer in self._content_recognizers:
            findings = recognizer.analyze(text, recognizer.supported_entities, None)
            if any(finding.score >= 0.35 for finding in findings):
                return True
        return False

    def _result(self, start, end, rule, entity_type="BASE64_DATA", encoded_container=True):
        return RecognizerResult(
            entity_type=entity_type, start=start, end=end, score=0.95,
            recognition_metadata={
                RecognizerResult.RECOGNIZER_NAME_KEY: self.name,
                RecognizerResult.RECOGNIZER_IDENTIFIER_KEY: self.id,
                DETECTION_SOURCE_METADATA_KEY: SOURCE_STRUCTURAL,
                "deterministic_rule_id": rule,
                "encoded_container": encoded_container,
            },
        )
