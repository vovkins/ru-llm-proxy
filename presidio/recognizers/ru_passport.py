"""Russian identity document recognizer exposed as RU_PASSPORT."""

import re

from presidio_analyzer import Pattern, PatternRecognizer


class RuPassportRecognizer(PatternRecognizer):
    """Recognize common Russian passport and identity document numbers."""

    PATTERNS = [
        # Format: XX XX XXXXXX (with spaces)
        Pattern(
            name="ru_passport_spaced",
            regex=r"(?<!\d)\d{2}\s+\d{2}\s*(?:№\s*)?\d{6}(?!\d)",
            score=0.5,
        ),
        # Format: XXXX XXXXXX (series without space)
        Pattern(
            name="ru_passport_compact",
            regex=r"(?<!\d)\d{4}\s+(?:№\s*)?\d{6}(?!\d)",
            score=0.4,
        ),
        # Foreign passport: XX XXXXXXX
        Pattern(
            name="ru_foreign_passport",
            regex=r"(?<!\d)\d{2}\s+(?:№\s*)?\d{7}(?!\d)",
            score=0.25,
        ),
        # Military identity card: two Cyrillic letters and seven digits.
        Pattern(
            name="ru_military_id",
            regex=(
                r"(?<![А-Яа-яЁё])(?i:[А-ЯЁ]{2})\s+"
                r"(?:№\s*)?\d{7}(?!\d)"
            ),
            score=0.25,
        ),
        # Birth certificate: Roman part, Cyrillic series, and six digits.
        Pattern(
            name="ru_birth_certificate",
            regex=(
                r"(?<![A-Za-zА-Яа-яЁё])"
                r"(?i:[IVXLCDM]{1,4}\s*[-‐‑‒–—]\s*[А-ЯЁ]{2})\s+"
                r"(?:№\s*)?\d{6}(?!\d)"
            ),
            score=0.25,
        ),
    ]

    CONTEXT = [
        "паспорт", "загранпаспорт", "паспортные данные", "серия",
        "номер паспорта", "военный билет", "военный", "билет",
        "свидетельство о рождении", "свидетельство", "рождение",
        "удостоверение личности", "документ", "passport", "удл",
    ]

    def __init__(
        self,
        name: str = "RuPassportRecognizer",
        supported_language: str = "ru",
        supported_entity: str = "RU_PASSPORT",
    ):
        super().__init__(
            supported_entity=supported_entity,
            patterns=self.PATTERNS,
            context=self.CONTEXT,
            name=name,
            supported_language=supported_language,
        )

    def validate_result(self, pattern_text: str):
        """Keep the pattern score so ambiguous formats still require context."""
        return None

    def invalidate_result(self, pattern_text: str) -> bool:
        """Reject candidates that do not match a supported document shape."""
        normalized = re.sub(r"[\s№]", "", pattern_text).upper()

        if normalized.isdigit():
            if len(normalized) == 10:
                return not 1 <= int(normalized[:2]) <= 99
            return len(normalized) != 9

        if re.fullmatch(r"[А-ЯЁ]{2}\d{7}", normalized):
            return False

        return (
            re.fullmatch(
                r"[IVXLCDM]{1,4}[-‐‑‒–—][А-ЯЁ]{2}\d{6}",
                normalized,
            )
            is None
        )
