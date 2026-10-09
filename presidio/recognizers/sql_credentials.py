"""Credentials in bounded INSERT statements containing literal VALUES only."""

import re

from presidio_analyzer import EntityRecognizer, RecognizerResult

from recognizers.credential_rules import (
    AuthTokenRecognizer,
    LoginRecognizer,
    PasswordRecognizer,
    SecretKeyRecognizer,
    _recognition_metadata,
)


_INSERT = re.compile(
    r"(?i)\bINSERT\s+INTO\s+[\w.\"`\[\]]+\s*"
    r"\((?P<columns>[\w\s,\"`\[\]]{1,2048})\)\s+VALUES\s*"
)
_COLUMN = re.compile(r'\s*(?:[\w]+|"[\w]+"|`[\w]+`|\[[\w]+\])\s*')
_VALUE = r"(?:'(?:''|[^'])*'|NULL|[+-]?\d+(?:\.\d+)?)"
_ROW = re.compile(rf"\s*\((?P<values>\s*{_VALUE}(?:\s*,\s*{_VALUE})*\s*)\)", re.I)
_LITERAL = re.compile(_VALUE, re.I)
MAX_INSERT_CHARS = 8192


class SqlCredentialRecognizer(EntityRecognizer):
    """Map explicit SQL columns to literals without evaluating SQL expressions."""

    def __init__(self, supported_language="ru"):
        super().__init__(
            supported_entities=["LOGIN", "PASSWORD", "AUTH_TOKEN", "SECRET_KEY"],
            supported_language=supported_language,
            name="SqlCredentialRecognizer",
            version="1.0.0",
        )
        self._value_recognizers = (
            LoginRecognizer(), PasswordRecognizer(),
            AuthTokenRecognizer(), SecretKeyRecognizer(),
        )

    def load(self):
        pass

    def analyze(self, text, entities, nlp_artifacts=None):
        requested = set(entities or self.supported_entities)
        results = []
        for insert in _INSERT.finditer(text):
            columns = insert.group("columns").split(",")
            if not all(_COLUMN.fullmatch(column) for column in columns):
                continue
            columns = [column.strip().strip('"`[]') for column in columns]
            cursor = insert.end()
            limit = min(len(text), insert.start() + MAX_INSERT_CHARS)
            rows = []
            while cursor < limit:
                row = _ROW.match(text, cursor, limit)
                if not row:
                    rows = []
                    break
                literals = list(_LITERAL.finditer(text, row.start("values"), row.end("values")))
                if len(literals) != len(columns):
                    rows = []
                    break
                rows.append(literals)
                cursor = row.end()
                tail = re.match(r"\s*(?P<delimiter>[,;]|$)", text[cursor:limit])
                if not tail:
                    rows = []
                    break
                if not tail.group("delimiter") and limit != len(text):
                    rows = []
                    break
                cursor += tail.end()
                if tail.group("delimiter") != ",":
                    break
            else:
                rows = []
            for literals in rows:
                for column, literal in zip(columns, literals):
                    raw = literal.group()
                    if not raw.startswith("'"):
                        continue
                    # Keep source spelling (including doubled SQL quotes) for restoration.
                    probe = f"{column}={raw}"
                    for recognizer in self._value_recognizers:
                        for finding in recognizer.analyze(probe, list(requested)):
                            if finding.start < len(column) + 2:
                                continue
                            results.append(RecognizerResult(
                                entity_type=finding.entity_type,
                                start=literal.start() + 1,
                                end=literal.end() - 1,
                                score=0.95,
                                recognition_metadata=_recognition_metadata(self, "credential.sql.literal"),
                            ))
        return EntityRecognizer.remove_duplicates(results)
