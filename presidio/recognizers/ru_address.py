"""Russian address recognizer (basic patterns)."""

from presidio_analyzer import Pattern, PatternRecognizer


_WORD_CHARS = r"0-9A-Za-zА-Яа-яЁё_"
_LEFT_TOKEN_BOUNDARY = rf"(?<![{_WORD_CHARS}])"
_STREET_TYPE = _LEFT_TOKEN_BOUNDARY + (
    r"(?:ул\.\s*|ул\s+|улица\s+|пр-т\s+|проспект\s+|пер\.\s*|пер\s+"
    r"|переулок\s+|б-р\s+|бульвар\s+|ш\.\s*|ш\s+|шоссе\s+)"
)
_STREET_NAME_WORD = r"[А-ЯЁ][а-яёА-ЯЁ-]+"
_STREET_NAME = rf"{_STREET_NAME_WORD}(?:\s+{_STREET_NAME_WORD}){{0,3}}"
_STREET_NAME_TYPE = r"(?:ул\.?|улица|проспект|пер\.?|переулок|бульвар|шоссе)"


class RuAddressRecognizer(PatternRecognizer):
    """Recognize Russian postal addresses (basic pattern matching)."""

    PATTERNS = [
        # Full address with street, house, apartment
        Pattern(
            name="ru_address_full",
            regex=(
                rf"{_STREET_TYPE}{_STREET_NAME}[,\.]?\s*"
                r"(?:д\.|дом)?\s*\d+[а-яё]?"
                r"\s*(?:[,/]\s*(?:корп\.|корпус|стр\.)\s*\d+[а-яё]?)?"
                r"(?:\s*[,\.]?\s*(?:кв\.|квартира|оф\.|офис)\s*\d+)?"
            ),
            score=0.7,
        ),
        # Street + house (without apartment)
        Pattern(
            name="ru_address_street_house",
            regex=(
                rf"{_STREET_TYPE}{_STREET_NAME}[,\.]?\s*"
                r"(?:д\.|дом)?\s*\d+[а-яё]?"
            ),
            score=0.6,
        ),
        # Street name followed by type and house number: "Тверская улица, дом 7"
        Pattern(
            name="ru_address_name_type_house",
            regex=(
                rf"{_LEFT_TOKEN_BOUNDARY}{_STREET_NAME}\s+{_STREET_NAME_TYPE}"
                r"[,\.]?\s*(?:д\.|дом)\s*\d+[а-яё]?"
            ),
            score=0.6,
        ),
        # City/town + street
        Pattern(
            name="ru_address_city_street",
            regex=(
                r"(?:г\.|гор\.|пос\.|с\.|дер\.)\s*[А-ЯЁ][а-яёА-ЯЁ\-]{1,30}"
                rf"[\s,]+{_STREET_TYPE}{_STREET_NAME}"
            ),
            score=0.6,
        ),
    ]

    CONTEXT = [
        "адрес", "проживает", "зарегистрирован", "место жительства",
        "дом", "квартира", "улица", "район",
        "прописка", "регистрация", "фактический адрес",
    ]

    def __init__(
        self,
        name: str = "RuAddressRecognizer",
        supported_language: str = "ru",
        supported_entity: str = "RU_ADDRESS",
    ):
        super().__init__(
            supported_entity=supported_entity,
            patterns=self.PATTERNS,
            context=self.CONTEXT,
            name=name,
            supported_language=supported_language,
        )
