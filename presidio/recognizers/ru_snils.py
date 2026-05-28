"""Russian SNILS (pension insurance number) recognizer.

SNILS: 11 digits, format XXX-XXX-XXX YY or XXXXXXXXXYY.
Checksum validation: last 2 digits are control sum.
"""


def _validate_snils(digits: str) -> bool:
    """Validate SNILS checksum."""
    if len(digits) != 11:
        return False
    # SNILS cannot start with 001 or 000
    if digits[:3] in ("000", "001"):
        return False

    total = sum(int(digits[i]) * (9 - i) for i in range(9))
    check = total % 101
    # Control number is last 2 digits
    control = int(digits[9:11])
    return check == control


class RuSnilsRecognizer(PatternRecognizer):
    """Recognize Russian SNILS numbers."""

    from presidio_analyzer import Pattern

    PATTERNS = [
        Pattern(
            name="ru_snils_formatted",
            regex=r"\b\d{3}[-\s]?\d{3}[-\s]?\d{3}[-\s]?\d{2}\b",
            score=0.4,
        ),
        Pattern(
            name="ru_snils_11digit",
            regex=r"(?<!\d)\d{11}(?!\d)",
            score=0.2,
        ),
    ]

    CONTEXT = [
        "снилс", "страховой номер", "пенсионный",
        "пенсия", "страховое свидетельство",
        "сзо", "адвр", "снилс",
    ]

    def __init__(
        self,
        name: str = "RuSnilsRecognizer",
        supported_language: str = "ru",
        supported_entity: str = "RU_SNILS",
    ):
        super().__init__(
            supported_entity=supported_entity,
            patterns=self.PATTERNS,
            context=self.CONTEXT,
            name=name,
            supported_language=supported_language,
        )

    def validate_result(self, pattern_text: str) -> bool:
        """Validate SNILS using checksum."""
        digits = "".join(c for c in pattern_text if c.isdigit())
        return _validate_snils(digits)
