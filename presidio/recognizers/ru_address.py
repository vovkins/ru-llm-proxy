"""Russian address recognizer (basic patterns)."""

import re

from presidio_analyzer import Pattern, PatternRecognizer


_WORD_CHARS = r"0-9A-Za-zА-Яа-яЁё_"
_LEFT_TOKEN_BOUNDARY = rf"(?<![{_WORD_CHARS}])"
_SPACE = r"[^\S\r\n]*"
_SPACE_REQUIRED = r"[^\S\r\n]+"
_SPACE_OR_COMMA_REQUIRED = r"(?:[^\S\r\n]|,)+"
_STREET_TYPE = _LEFT_TOKEN_BOUNDARY + (
    rf"(?:(?i:ул\.){_SPACE}|(?i:ул){_SPACE_REQUIRED}|(?i:улица){_SPACE_REQUIRED}"
    rf"|(?i:пр-т){_SPACE_REQUIRED}|(?i:проспект){_SPACE_REQUIRED}"
    rf"|(?i:пер\.){_SPACE}|(?i:пер){_SPACE_REQUIRED}|(?i:переулок){_SPACE_REQUIRED}"
    rf"|(?i:б-р){_SPACE_REQUIRED}|(?i:бульвар){_SPACE_REQUIRED}"
    rf"|(?i:ш\.){_SPACE}|(?i:ш){_SPACE_REQUIRED}|(?i:шоссе){_SPACE_REQUIRED})"
)
_STREET_NAME_WORD = r"[А-ЯЁа-яё][а-яёА-ЯЁ-]+"
_STREET_NAME_WORD_CAPITALIZED = r"[А-ЯЁ][а-яёА-ЯЁ-]+"
_STREET_NAME = rf"{_STREET_NAME_WORD}(?:{_SPACE_REQUIRED}{_STREET_NAME_WORD}){{0,3}}"
_STREET_NAME_CAPITALIZED = (
    rf"{_STREET_NAME_WORD_CAPITALIZED}"
    rf"(?:{_SPACE_REQUIRED}{_STREET_NAME_WORD}){{0,3}}"
)
_STREET_NAME_TYPE = r"(?i:ул\.?|улица|проспект|пер\.?|переулок|бульвар|шоссе)"
_HOUSE_MARKER = r"(?i:д\.|дом)"
_COUNT_NOUN_AFTER_HOUSE = (
    r"(?![^\S\r\n]*(?:лет|год(?:а|ов)?|раз|человек|"
    r"процент(?:а|ов)?|метр(?:а|ов)?|час(?:а|ов)?|минут(?:а|ы)?)\b)"
)
_HOUSE_NUMBER = rf"\d+[а-яё]?(?![0-9А-Яа-яЁё-]){_COUNT_NOUN_AFTER_HOUSE}"
_PREFIX_STREET_MARKED_HOUSE = (
    rf"{_STREET_TYPE}{_STREET_NAME}[,\.]?{_SPACE}{_HOUSE_MARKER}{_SPACE}{_HOUSE_NUMBER}"
)
_PREFIX_STREET_BARE_HOUSE = (
    rf"{_STREET_TYPE}{_STREET_NAME_WORD_CAPITALIZED}[,\.]?{_SPACE}{_HOUSE_NUMBER}"
)
_ADDRESS_REGEX_FLAGS = re.DOTALL | re.MULTILINE


class RuAddressRecognizer(PatternRecognizer):
    """Recognize Russian postal addresses (basic pattern matching)."""

    PATTERNS = [
        # Full address with street, house, apartment
        Pattern(
            name="ru_address_full",
            regex=(
                rf"(?:{_PREFIX_STREET_MARKED_HOUSE}|{_PREFIX_STREET_BARE_HOUSE})"
                rf"{_SPACE}(?:[,/]{_SPACE}(?:корп\.|корпус|стр\.){_SPACE}{_HOUSE_NUMBER})?"
                rf"(?:{_SPACE}[,\.]?{_SPACE}(?:кв\.|квартира|оф\.|офис){_SPACE}\d+)?"
            ),
            score=0.7,
        ),
        # Street + house (without apartment)
        Pattern(
            name="ru_address_street_house",
            regex=(
                rf"(?:{_PREFIX_STREET_MARKED_HOUSE}|{_PREFIX_STREET_BARE_HOUSE})"
            ),
            score=0.6,
        ),
        # Street name followed by type and house number: "Тверская улица, дом 7"
        Pattern(
            name="ru_address_name_type_house",
            regex=(
                rf"{_LEFT_TOKEN_BOUNDARY}{_STREET_NAME}{_SPACE_REQUIRED}{_STREET_NAME_TYPE}"
                rf"[,\.]?{_SPACE}{_HOUSE_MARKER}{_SPACE}{_HOUSE_NUMBER}"
            ),
            score=0.6,
        ),
        # City/town + street
        Pattern(
            name="ru_address_city_street",
            regex=(
                rf"(?i:г\.|гор\.|пос\.|с\.|дер\.){_SPACE}[А-ЯЁ][а-яёА-ЯЁ\-]{{1,30}}"
                rf"{_SPACE_OR_COMMA_REQUIRED}(?:"
                rf"{_PREFIX_STREET_MARKED_HOUSE}|{_PREFIX_STREET_BARE_HOUSE}|"
                rf"{_STREET_TYPE}{_STREET_NAME_CAPITALIZED}"
                r")"
            ),
            score=0.75,
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
            global_regex_flags=_ADDRESS_REGEX_FLAGS,
        )
