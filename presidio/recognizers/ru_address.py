"""Russian address recognizer (basic patterns)."""

from presidio_analyzer import Pattern, PatternRecognizer


_LEFT_TOKEN_BOUNDARY = r"(?<![0-9A-Za-zА-Яа-яЁё])"
_CASED_CAPITAL = r"(?-i:[А-ЯЁ])"
_NAME_WORD = r"[А-ЯЁа-яё][А-Яа-яЁё-]{1,30}"
_NAME_WORD_CAPITALIZED = rf"{_CASED_CAPITAL}[А-Яа-яЁё-]{{1,30}}"
_NAME_WORDS = rf"{_NAME_WORD}(?:[ \t]+{_NAME_WORD}){{0,3}}"
_NAME_WORDS_CAPITALIZED = (
    rf"{_NAME_WORD_CAPITALIZED}(?:[ \t]+{_NAME_WORD}){{0,3}}"
)
_COUNT_NOUN_AFTER_HOUSE = (
    r"(?![ \t]*(?:лет|год(?:а|ов)?|раз|человек|"
    r"процент(?:а|ов)?|час(?:а|ов)?|минут(?:а|ы)?|дн(?:я|ей)|"
    r"метр(?:а|ов)?|километр(?:а|ов)?|км|рубл(?:ь|я|ей|ю|ем)|"
    r"дом(?:а|ов)?|квартир(?:а|ы)?|этаж(?:а|ей)?|"
    r"месяц(?:а|ев)?|недел(?:я|и|ь)|штук(?:а|и)?)\b)"
)
_HOUSE_NUMBER = rf"\d+[а-яё]?(?![0-9А-Яа-яЁё-]){_COUNT_NOUN_AFTER_HOUSE}"
_HOUSE_EXPLICIT = rf"[,\.]?[ \t]*(?:д\.|дом)[ \t]*{_HOUSE_NUMBER}"
_HOUSE_BARE = rf"[,\.]?[ \t]*{_HOUSE_NUMBER}"
_CITY_PREFIX = (
    _LEFT_TOKEN_BOUNDARY
    + rf"(?:г\.|гор\.|пос\.|с\.|дер\.)[ \t]*{_NAME_WORD_CAPITALIZED}[ \t,]+"
)
_STREET_TYPE_PREFIX = (
    _LEFT_TOKEN_BOUNDARY
    + r"(?:(?:улица|проспект|переулок|бульвар|шоссе)(?=[ \t])|"
    + r"(?:ул\.|пр-т|пер\.|б-р|ш\.)(?=[ \t]*[А-Яа-яЁё])|"
    + r"(?:ул|пер|ш)(?=[ \t]+[А-Яа-яЁё]))"
)


class RuAddressRecognizer(PatternRecognizer):
    """Recognize Russian postal addresses (basic pattern matching)."""

    PATTERNS = [
        # Full address with street, house, apartment
        Pattern(
            name="ru_address_full",
            regex=rf"(?:{_CITY_PREFIX})?{_STREET_TYPE_PREFIX}[ \t]*{_NAME_WORDS}{_HOUSE_EXPLICIT}[ \t]*(?:[,/][ \t]*(?:корп\.|корпус|стр\.)[ \t]*\d+[а-яё]?)?(?:[ \t]*[,\.]?[ \t]*(?:кв\.|квартира|оф\.|офис)[ \t]*\d+)?",
            score=0.7,
        ),
        # Street + explicit house without apartment.
        Pattern(
            name="ru_address_street_house_explicit",
            regex=rf"{_STREET_TYPE_PREFIX}[ \t]*{_NAME_WORDS}{_HOUSE_EXPLICIT}",
            score=0.6,
        ),
        # Street + bare house: stricter to avoid ordinary prose like "улица ... 10 лет".
        Pattern(
            name="ru_address_street_house_bare",
            regex=rf"{_STREET_TYPE_PREFIX}[ \t]*{_NAME_WORD_CAPITALIZED}{_HOUSE_BARE}",
            score=0.3,
        ),
        # Street name followed by type and house number: "Тверская улица, дом 7"
        Pattern(
            name="ru_address_name_type_house",
            regex=rf"{_NAME_WORDS}[ \t]+(?:улица|проспект|переулок|бульвар|шоссе|ул\.?|пер\.?){_HOUSE_EXPLICIT}",
            score=0.6,
        ),
        # City/town + street
        Pattern(
            name="ru_address_city_street",
            regex=rf"{_CITY_PREFIX}{_STREET_TYPE_PREFIX}[ \t]*{_NAME_WORDS_CAPITALIZED}",
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
