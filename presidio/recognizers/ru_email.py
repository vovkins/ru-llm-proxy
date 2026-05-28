"""Email address recognizer (language-agnostic, Russian context words)."""

from presidio_analyzer import Pattern, PatternRecognizer


class RuEmailRecognizer(PatternRecognizer):
    """Recognize email addresses with Russian context words."""

    # Standard email regex
    PATTERNS = [
        Pattern(
            name="email_address",
            regex=r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b",
            score=0.85,
        ),
    ]

    CONTEXT = [
        "email", "почта", "е-мейл", "электронная почта",
        "e-mail", "почтовый адрес", "ящик",
        "написать", "отправить", "связаться",
    ]

    def __init__(
        self,
        name: str = "RuEmailRecognizer",
        supported_language: str = "ru",
        supported_entity: str = "EMAIL_ADDRESS",
    ):
        super().__init__(
            supported_entity=supported_entity,
            patterns=self.PATTERNS,
            context=self.CONTEXT,
            name=name,
            supported_language=supported_language,
        )
