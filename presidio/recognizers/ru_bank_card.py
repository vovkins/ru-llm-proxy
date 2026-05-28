"""Russian bank card number recognizer."""

from presidio_analyzer import Pattern, PatternRecognizer


def _luhn_check(card_number: str) -> bool:
    """Validate card number using Luhn algorithm."""
    digits = [int(d) for d in card_number]
    odd_digits = digits[-1::-2]
    even_digits = digits[-2::-2]
    total = sum(odd_digits)
    for d in even_digits:
        total += sum(divmod(d * 2, 10))
    return total % 10 == 0


class RuBankCardRecognizer(PatternRecognizer):
    """Recognize bank card numbers with Luhn validation."

    PATTERNS = [
        # Standard 16-digit cards (Visa, Mastercard, МИР)
        Pattern(
            name="ru_card_16",
            regex=r"\b\d{4}[\s\-]?\d{4}[\s\-]?\d{4}[\s\-]?\d{4}\b",
            score=0.6,
        ),
        # 18-digit cards (some Russian banks)
        Pattern(
            name="ru_card_18",
            regex=r"\b\d{4}[\s\-]?\d{4}[\s\-]?\d{4}[\s\-]?\d{4}[\s\-]?\d{2}\b",
            score=0.4,
        ),
    ]

    CONTEXT = [
        "карта", "банковская карта", "номер карты",
        "карточка", "кредитная", "дебетовая",
        "visa", "mastercard", "мир",
        "счёт", "оплата", "платёж",
        "cvv", "cvc",
    ]

    def __init__(
        self,
        name: str = "RuBankCardRecognizer",
        supported_language: str = "ru",
        supported_entity: str = "CREDIT_CARD",
    ):
        super().__init__(
            supported_entity=supported_entity,
            patterns=self.PATTERNS,
            context=self.CONTEXT,
            name=name,
            supported_language=supported_language,
        )

    def validate_result(self, pattern_text: str) -> bool:
        """Validate card number using Luhn algorithm."""
        digits = "".join(c for c in pattern_text if c.isdigit())
        if len(digits) < 13 or len(digits) > 19:
            return False
        return _luhn_check(digits)
