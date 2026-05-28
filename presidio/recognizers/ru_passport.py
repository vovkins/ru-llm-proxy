"""Russian passport number recognizer.

Russian passport: series (2 digits for region + 2 digits for year) + number (6 digits).
Format: XX XX XXXXXX or XXXX XXXXXX.
"""


class RuPassportRecognizer(PatternRecognizer):
    """Recognize Russian passport series and numbers."""

    from presidio_analyzer import Pattern

    PATTERNS = [
        # Format: XX XX XXXXXX (with spaces)
        Pattern(
            name="ru_passport_spaced",
            regex=r"\b\d{2}\s\d{2}\s\d{6}\b",
            score=0.5,
        ),
        # Format: XXXX XXXXXX (series without space)
        Pattern(
            name="ru_passport_compact",
            regex=r"\b\d{4}\s\d{6}\b",
            score=0.4,
        ),
    ]

    CONTEXT = [
        "паспорт", "паспортные данные", "серия", "номер паспорта",
        "удостоверение личности", "документ",
        "passport", "удл",
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

    def validate_result(self, pattern_text: str) -> bool:
        """Basic validation: series should be in valid range."""
        digits = "".join(c for c in pattern_text if c.isdigit())
        if len(digits) != 10:
            return False
        series = int(digits[:4])
        # First 2 digits of series: region code (01-99)
        region = int(digits[:2])
        if region < 1 or region > 99:
            return False
        # Year part of series: typically 00-99, but realistically 97-30
        # We keep it broad to avoid false negatives
        return True
