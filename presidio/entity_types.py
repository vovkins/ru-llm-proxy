"""Public entity contract shared by Analyzer code and proxy metadata."""

DETERMINISTIC_ENTITY_TYPES = frozenset(
    {
        "PHONE_NUMBER",
        "EMAIL_ADDRESS",
        "RU_INN",
        "RU_KPP",
        "RU_OGRN",
        "RU_OGRNIP",
        "RU_BIK",
        "RU_SETTLEMENT_ACCOUNT",
        "RU_CORRESPONDENT_ACCOUNT",
        "RU_SNILS",
        "RU_PASSPORT",
        "CREDIT_CARD",
        "RU_ADDRESS",
        "INTERNAL_IP",
        "INTERNAL_DOMAIN",
        "HOSTNAME",
        "DB_URL",
        "JWT",
        "BEARER_TOKEN",
        "PRIVATE_KEY",
        "API_KEY",
        "SECRET_KEY",
        "AUTH_TOKEN",
        "LOGIN",
        "PASSWORD",
    }
)

NER_ENTITY_TYPES = frozenset(
    {
        "PERSON",
        "LOCATION",
        "ORGANIZATION",
        "LOGIN",
        "PASSWORD",
        "AUTH_TOKEN",
        "SECRET_KEY",
        "CONTRACT_NUMBER",
    }
)

SUPPORTED_ENTITY_TYPES = DETERMINISTIC_ENTITY_TYPES | NER_ENTITY_TYPES
