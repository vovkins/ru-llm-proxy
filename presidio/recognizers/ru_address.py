"""Russian address recognizer (basic patterns)."""

import re

from presidio_analyzer import Pattern, PatternRecognizer


_WORD_CHARS = r"0-9A-Za-zА-Яа-яЁё_"
_LEFT_TOKEN_BOUNDARY = rf"(?<![{_WORD_CHARS}])"
_SPACE = r"[^\S\r\n]*"
_SPACE_REQUIRED = r"[^\S\r\n]+"
_SPACE_OR_COMMA_REQUIRED = r"(?:[^\S\r\n]|,)+"
_STREET_TYPE = _LEFT_TOKEN_BOUNDARY + (
    rf"(?:ул\.{_SPACE}|ул{_SPACE_REQUIRED}|улица{_SPACE_REQUIRED}"
    rf"|пр-т{_SPACE_REQUIRED}|проспект{_SPACE_REQUIRED}"
    rf"|пер\.{_SPACE}|пер{_SPACE_REQUIRED}|переулок{_SPACE_REQUIRED}"
    rf"|б-р{_SPACE_REQUIRED}|бульвар{_SPACE_REQUIRED}"
    rf"|ш\.{_SPACE}|ш{_SPACE_REQUIRED}|шоссе{_SPACE_REQUIRED})"
)
_STREET_NAME_WORD = r"[А-ЯЁ][а-яёА-ЯЁ-]+"
_STREET_NAME = rf"{_STREET_NAME_WORD}(?:{_SPACE_REQUIRED}{_STREET_NAME_WORD}){{0,3}}"
_STREET_NAME_TYPE = r"(?:ул\.?|улица|проспект|пер\.?|переулок|бульвар|шоссе)"
_HOUSE_MARKER = r"(?:д\.|дом)"
_HOUSE_NUMBER = r"\d+[а-яё]?"
_PREFIX_STREET_MARKED_HOUSE = (
    rf"{_STREET_TYPE}{_STREET_NAME}[,\.]?{_SPACE}{_HOUSE_MARKER}{_SPACE}{_HOUSE_NUMBER}"
)
_PREFIX_STREET_BARE_HOUSE = (
    rf"{_STREET_TYPE}{_STREET_NAME_WORD}[,\.]?{_SPACE}{_HOUSE_NUMBER}"
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
                rf"(?:г\.|гор\.|пос\.|с\.|дер\.){_SPACE}[А-ЯЁ][а-яёА-ЯЁ\-]{{1,30}}"
                rf"{_SPACE_OR_COMMA_REQUIRED}(?:"
                rf"{_PREFIX_STREET_MARKED_HOUSE}|{_PREFIX_STREET_BARE_HOUSE}|"
                rf"{_STREET_TYPE}{_STREET_NAME}"
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
