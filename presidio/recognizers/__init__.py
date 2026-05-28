"""Russian PII recognizers for Presidio."""

from presidio.recognizers.ru_phone import RuPhoneRecognizer
from presidio.recognizers.ru_email import RuEmailRecognizer
from presidio.recognizers.ru_inn import RuInnRecognizer
from presidio.recognizers.ru_snils import RuSnilsRecognizer
from presidio.recognizers.ru_passport import RuPassportRecognizer
from presidio.recognizers.ru_bank_card import RuBankCardRecognizer
from presidio.recognizers.ru_address import RuAddressRecognizer

ALL_RECOGNIZERS = [
    RuPhoneRecognizer,
    RuEmailRecognizer,
    RuInnRecognizer,
    RuSnilsRecognizer,
    RuPassportRecognizer,
    RuBankCardRecognizer,
    RuAddressRecognizer,
]
