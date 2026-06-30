"""Russian INN (Individual Taxpayer Number) recognizer.

INN for individuals: 12 digits, checksum validation.
INN for legal entities: 10 digits, checksum validation.
"""

import os

from presidio_analyzer import Pattern, PatternRecognizer


def _validate_inn_10(digits: str) -> bool:
    """Validate 10-digit INN (legal entity)."""
    weights = [2, 4, 10, 3, 5, 9, 4, 6, 8]
    total = sum(int(d) * w for d, w in zip(digits[:9], weights))
    check = total % 11
    if check > 9:
        check = 0
    return check == int(digits[9])


def _validate_inn_12(digits: str) -> bool:
    """Validate 12-digit INN (individual)."""
    weights1 = [7, 2, 4, 10, 3, 5, 9, 4, 6, 8]
    weights2 = [3, 7, 2, 4, 10, 3, 5, 9, 4, 6, 8]

    total1 = sum(int(d) * w for d, w in zip(digits[:10], weights1))
    check1 = total1 % 11
    if check1 > 9:
        check1 = 0
    if check1 != int(digits[10]):
        return False

    total2 = sum(int(d) * w for d, w in zip(digits[:11], weights2))
    check2 = total2 % 11
    if check2 > 9:
        check2 = 0
    return check2 == int(digits[11])


class RuInnRecognizer(PatternRecognizer):
    """Recognize Russian INN (tax identification numbers)."""

    STRICT_PATTERNS = [
        Pattern(
            name="ru_inn_12digit",
            regex=r"(?<!\d)\d{12}(?!\d)",
            score=0.3,
        ),
        Pattern(
            name="ru_inn_10digit",
            regex=r"(?<!\d)\d{10}(?!\d)",
            score=0.2,
        ),
    ]
    CHECKSUM_ONLY_PATTERNS = [
        Pattern(
            name="ru_inn_12digit",
            regex=r"(?<!\d)\d{12}(?!\d)",
            score=0.4,
        ),
        Pattern(
            name="ru_inn_10digit",
            regex=r"(?<!\d)\d{10}(?!\d)",
            score=0.4,
        ),
    ]

    CONTEXT = [
        "инн", "индивидуальный номер налогоплательщика",
        "налог", "налоговый", "налоговая",
        "inn", " taxpayer",
        "огрн", "огрнип", "кпп",
    ]

    def __init__(
        self,
        name: str = "RuInnRecognizer",
        supported_language: str = "ru",
        supported_entity: str = "RU_INN",
    ):
        patterns = (
            self.CHECKSUM_ONLY_PATTERNS
            if _detect_bare_inn_by_checksum()
            else self.STRICT_PATTERNS
        )
        super().__init__(
            supported_entity=supported_entity,
            patterns=patterns,
            context=self.CONTEXT,
            name=name,
            supported_language=supported_language,
        )

    def _has_valid_checksum(self, pattern_text: str) -> bool:
        """Return whether INN text has a valid checksum."""
        digits = pattern_text.strip()
        if not digits.isdigit():
            return False
        if len(digits) == 10:
            return _validate_inn_10(digits)
        if len(digits) == 12:
            return _validate_inn_12(digits)
        return False

    def validate_result(self, pattern_text: str):
        """Keep base score for valid INN instead of boosting to MAX_SCORE."""
        return None

    def invalidate_result(self, pattern_text: str) -> bool:
        """Reject INN-like numbers with an invalid checksum."""
        return not self._has_valid_checksum(pattern_text)

    def enhance_score_with_context(self, text, patterns):
        """Boost score when context words are found."""
        # Base pattern scores are low (0.2-0.3) because digits alone
        # are ambiguous. Context words increase confidence significantly.
        return super().enhance_score_with_context(text, patterns)


def _detect_bare_inn_by_checksum() -> bool:
    """Return whether checksum-valid bare INN passes the default API threshold."""
    raw_value = os.getenv("PRESIDIO_ANALYZER_DETECT_BARE_INN_BY_CHECKSUM", "true")
    normalized = raw_value.strip().lower()
    return normalized not in {"0", "false", "no", "off"}
