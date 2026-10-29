"""Email address recognizer (language-agnostic, Russian context words)."""

from presidio_analyzer import Pattern, PatternRecognizer


_LETTERS = r"A-Za-z\p{Script=Cyrillic}"
_LOCAL = rf"[{_LETTERS}0-9_][{_LETTERS}0-9._%+\-]*"
_DOMAIN = rf"[{_LETTERS}0-9.\-]+"
_SUFFIX = rf"(?:xn--[A-Za-z0-9\-]+|[{_LETTERS}]{{2,}})"


class RuEmailRecognizer(PatternRecognizer):
    """Recognize email addresses with Russian context words."""

    # Match complete supported values, including an encoded final domain label.
    # Unicode word guards prevent a match ending inside an unsupported suffix.
    PATTERNS = [
        Pattern(
            name="email_address",
            regex=rf"(?<![\w@]){_LOCAL}@{_DOMAIN}\.{_SUFFIX}(?![\w@\-])",
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
