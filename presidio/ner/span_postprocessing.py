"""Expand NER fragments within a value; leave conflicts to the common merger.

No finding is discarded or relabelled here. Existing string syntax bounds
quoted values; URI components use the standard parser. Unknown scalar values
keep punctuation, rather than guessing which part belongs to a secret.
"""

from __future__ import annotations

from bisect import bisect_right
import re
from urllib.parse import urlsplit

from string_spans import StringSpans
from model_profiles import (
    SPAN_POSTPROCESSING_NONE,
    SPAN_POSTPROCESSING_SECRETS_CONTRACTS,
    SPAN_POSTPROCESSING_MODES,
)

Span = tuple[int, int, str, float]
CREDENTIAL_TYPES = frozenset({"LOGIN", "PASSWORD", "SECRET_KEY", "AUTH_TOKEN"})
CONTRACT_TYPES = frozenset({"CONTRACT_NUMBER"})
_SCALAR = re.compile(r"[^\s\"'`,;()\[\]{}<>]+")
_ASSIGNMENT = re.compile(r"[^\W\d][\w.-]*=(?!=)")
_URI = re.compile(r"(?<![A-Za-z0-9+.-])[A-Za-z][A-Za-z0-9+.-]*://[^\s\"'`<>]+")


def _uri_values(text):
    """Raw source offsets, without percent-decoding or rewriting the URI."""
    values, owners = [], []
    for match in _URI.finditer(text):
        raw = match.group()
        try:
            uri = urlsplit(raw)
        except ValueError:
            continue
        found = []
        authority_start = match.start() + raw.index("://") + 3
        userinfo, separator, _ = uri.netloc.rpartition("@")
        if separator:
            login, colon, password = userinfo.partition(":")
            if login:
                found.append((authority_start, authority_start + len(login)))
            if colon and password:
                found.append((authority_start + len(login) + 1, authority_start + len(userinfo)))
        if uri.query:
            offset = match.start() + raw.index("?") + 1
            for pair in uri.query.split("&"):
                key, equals, value = pair.partition("=")
                if equals and value:
                    found.append((offset + len(key) + 1, offset + len(pair)))
                offset += len(pair) + 1
        if found:
            values.extend(found)
            owners.append(match.span())
    return sorted(values), owners


class _ValueBoundaries:
    def __init__(self, text, *, contracts):
        self.text = text
        self.strings = StringSpans(text)
        self.scalars = [match.span() for match in _SCALAR.finditer(text)]
        self.scalar_starts = [start for start, _ in self.scalars]
        self.scalar_cache = {}
        self.uri_values, self.uri_owners = _uri_values(text)
        self.uri_starts = [start for start, _ in self.uri_values]
        self.owner_starts = [start for start, _ in self.uri_owners]
        if contracts:
            from result_merging import contract_context_evidence
            self.contracts = contract_context_evidence(text, _strings=self.strings)
        else:
            self.contracts = []
        self.contract_starts = [start for start, _ in self.contracts]

    @staticmethod
    def _containing(ranges, starts, start, end):
        index = bisect_right(starts, start) - 1
        if index >= 0 and end <= ranges[index][1]:
            return ranges[index]
        return None

    def bounds(self, start, end, label):
        if not 0 <= start < end <= len(self.text):
            return start, end
        if label in CREDENTIAL_TYPES:
            uri = self._containing(self.uri_values, self.uri_starts, start, end)
            if uri is not None:
                return uri
            if self._containing(self.uri_owners, self.owner_starts, start, end):
                return start, end
        elif label in CONTRACT_TYPES:
            contract = self._containing(self.contracts, self.contract_starts, start, end)
            if contract is not None:
                return contract
        literal = self.strings.at(start)
        if literal is not None:
            if literal.content_start <= start < end <= literal.content_end:
                return literal.content_start, literal.content_end
            return start, end
        scalar = self._containing(self.scalars, self.scalar_starts, start, end)
        if scalar is None:
            return start, end
        if scalar not in self.scalar_cache:
            left, right = scalar
            assignment = _ASSIGNMENT.match(self.text, left, right)
            if assignment:
                left = assignment.end()
            elif ":" in self.text[left:right]:
                # Reuse strict key grammars; a colon alone does not prove that
                # an unlabelled secret starts with a field name.
                from recognizers.credential_rules import (
                    LoginRecognizer,
                    PasswordRecognizer,
                    SecretKeyRecognizer,
                    AuthTokenRecognizer,
                )
                for recognizer in (LoginRecognizer, PasswordRecognizer, SecretKeyRecognizer, AuthTokenRecognizer):
                    for rule in recognizer.RULES:
                        if not rule.rule_id.startswith("credential.kv."):
                            continue
                        match = rule.regex.match(self.text, left, right)
                        if match:
                            left = match.start("value")
                            break
                    else:
                        continue
                    break
            self.scalar_cache[scalar] = left, right
        left, right = self.scalar_cache[scalar]
        return (left, right) if left <= start else (start, right)


def expand_value_spans(text: str, spans: list[Span], mode: str) -> list[Span]:
    """Preserve input order and duplicates for source-evidence associations."""
    if mode not in SPAN_POSTPROCESSING_MODES:
        raise ValueError(f"unknown span post-processing mode: {mode}")
    labels = CREDENTIAL_TYPES | (CONTRACT_TYPES if mode == SPAN_POSTPROCESSING_SECRETS_CONTRACTS else frozenset())
    if mode == SPAN_POSTPROCESSING_NONE or not any(label in labels for _, _, label, _ in spans):
        return list(spans)
    boundaries = _ValueBoundaries(text, contracts=any(label in CONTRACT_TYPES for _, _, label, _ in spans))
    repaired = []
    for start, end, label, score in spans:
        if label in labels:
            start, end = boundaries.bounds(start, end, label)
        repaired.append((start, end, label, score))
    return repaired


def postprocess_spans(text: str, spans: list[Span], mode: str) -> list[Span]:
    repaired = expand_value_spans(text, spans, mode)
    labels = CREDENTIAL_TYPES | (CONTRACT_TYPES if mode == SPAN_POSTPROCESSING_SECRETS_CONTRACTS else frozenset())
    if mode == SPAN_POSTPROCESSING_NONE or not any(label in labels for _, _, label, _ in spans):
        return repaired
    # Only exact duplicates are redundant; scores/types and overlaps survive.
    return sorted(set(repaired))
