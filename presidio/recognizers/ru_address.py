"""Russian address recognizer (basic patterns)."""

from presidio_analyzer import Pattern, PatternRecognizer


_LEFT_TOKEN_BOUNDARY = r"(?<![0-9A-Za-zА-Яа-яЁё])"
_CASED_CAPITAL = r"(?-i:[А-ЯЁ])"
_NAME_TAIL = r"[а-яёА-ЯЁ\s\-]"
_CITY_PREFIX = (
    _LEFT_TOKEN_BOUNDARY
    + rf"(?:г\.|гор\.|пос\.|с\.|дер\.)\s*{_CASED_CAPITAL}[а-яёА-ЯЁ\-]{{1,30}}[\s,]+"
)
_STREET_TYPE_PREFIX = (
    _LEFT_TOKEN_BOUNDARY
    + r"(?:улица|проспект|переулок|бульвар|шоссе|ул\.?|пр-т|пер\.?|б-р|ш\.?)(?=\s)"
)


class RuAddressRecognizer(PatternRecognizer):
    """Recognize Russian postal addresses (basic pattern matching)."""

    PATTERNS = [
        # Full address with street, house, apartment
        Pattern(
            name="ru_address_full",
            regex=rf"(?:{_CITY_PREFIX})?{_STREET_TYPE_PREFIX}\s+{_CASED_CAPITAL}{_NAME_TAIL}{{1,50}}?[,\.]?\s*(?:д\.|дом)?\s*\d+[а-яё]?\s*(?:[,/]\s*(?:корп\.|корпус|стр\.)\s*\d+[а-яё]?)?(?:\s*[,\.]?\s*(?:кв\.|квартира|оф\.|офис)\s*\d+)?",
            score=0.7,
        ),
        # Street + house (without apartment)
        Pattern(
            name="ru_address_street_house",
            regex=rf"{_STREET_TYPE_PREFIX}\s+{_CASED_CAPITAL}{_NAME_TAIL}{{1,50}}?[,\.]?\s*(?:д\.|дом)?\s*\d+[а-яё]?",
            score=0.6,
        ),
        # Street name followed by type and house number: "Тверская улица, дом 7"
        Pattern(
            name="ru_address_name_type_house",
            regex=rf"{_CASED_CAPITAL}{_NAME_TAIL}{{1,50}}?\s+(?:улица|проспект|переулок|бульвар|шоссе|ул\.?|пер\.?)[,\.]?\s*(?:д\.|дом)\s*\d+[а-яё]?",
            score=0.6,
        ),
        # City/town + street
        Pattern(
            name="ru_address_city_street",
            regex=rf"{_CITY_PREFIX}{_STREET_TYPE_PREFIX}\s+{_CASED_CAPITAL}{_NAME_TAIL}{{1,40}}",
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
