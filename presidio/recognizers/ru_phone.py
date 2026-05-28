"""Russian phone number recognizer."""

import re
from typing import List

from presidio_analyzer import Pattern, PatternRecognizer


class RuPhoneRecognizer(PatternRecognizer):
    """Recognize Russian phone numbers in various formats.

    Supports:
      - +7 (XXX) XXX-XX-XX
      - +7XXXXXXXXXX
      - 8XXXXXXXXXX
      - 8 (XXX) XXX-XX-XX
      - With spaces, dashes, dots as separators
    """

    PATTERNS = [
        # +7 (XXX) XXX-XX-XX or variations
        Pattern(
            name="ru_phone_with_country_code",
            regex=r"(?:\+?7|8)[\s\-\.]?\(?\d{3}\)?[\s\-\.]?\d{3}[\s\-\.]?\d{2}[\s\-\.]?\d{2}",
            score=0.85,
        ),
        # 10-digit number starting with 9 (mobile without prefix)
        Pattern(
            name="ru_phone_mobile_10digit",
            regex=r"(?<!\d)9\d{9}(?!\d)",
            score=0.5,
        ),
    ]

    CONTEXT = [
        "телефон", "тел", "тел.", "мобильный", "сотовый",
        "звонить", "позвонить", "набрать", "номер",
        "phone", "tel", "mobile", "call",
        "контактный", "т.", "mob.",
    ]

    def __init__(
        self,
        name: str = "RuPhoneRecognizer",
        supported_language: str = "ru",
        supported_entity: str = "PHONE_NUMBER",
    ):
        super().__init__(
            supported_entity=supported_entity,
            patterns=self.PATTERNS,
            context=self.CONTEXT,
            name=name,
            supported_language=supported_language,
        )

    def validate_result(self, pattern_text: str) -> bool:
        """Validate that the matched text looks like a real phone number."""
        digits = re.sub(r"\D", "", pattern_text)
        # After stripping non-digits, should have 10 or 11 digits
        if len(digits) == 11 and digits[0] in ("7", "8"):
            return True
        if len(digits) == 10:
            return True
        return False
