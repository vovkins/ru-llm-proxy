"""Russian address recognizer (basic patterns).

Detects common Russian address patterns:
  - ул. / улица / проспект / пр-т / переулок / пер. / бульвар / б-р
  - д. / дом / корп. / стр. / кв. / офис / оф.
  - City/town prefixes: г. / гор. / пос. / с. / дер.
"""


class RuAddressRecognizer(PatternRecognizer):
    """Recognize Russian postal addresses (basic pattern matching)."""

    from presidio_analyzer import Pattern

    PATTERNS = [
        # Full address with street, house, apartment
        Pattern(
            name="ru_address_full",
            regex=r"(?:ул\.|улица|пр-т|проспект|пер\.|переулок|б-р|бульвар|шоссе|ш\.)\s*[А-ЯЁ][а-яёА-ЯЁ\s\-]{1,50}?[,\.]?\s*(?:д\.|дом)\s*\d+[а-яё]?\s*(?:[,/]\s*(?:корп\.|корпус|стр\.)\s*\d+[а-яё]?)?(?:\s*[,\.]?\s*(?:кв\.|квартира|оф\.|офис)\s*\d+)?",
            score=0.7,
        ),
        # Street + house (without apartment)
        Pattern(
            name="ru_address_street_house",
            regex=r"(?:ул\.|улица|пр-т|проспект|пер\.|переулок|б-р|бульвар|шоссе|ш\.)\s*[А-ЯЁ][а-яёА-ЯЁ\s\-]{1,50}?[,\.]?\s*(?:д\.|дом)\s*\d+[а-яё]?",
            score=0.6,
        ),
        # City/town + street
        Pattern(
            name="ru_address_city_street",
            regex=r"(?:г\.|гор\.|пос\.|с\.|дер\.)\s*[А-ЯЁ][а-яёА-ЯЁ\-]{1,30}[\s,]+(?:ул\.|улица|пр-т|проспект)\s*[А-ЯЁ][а-яёА-ЯЁ\s\-]{1,40}",
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
