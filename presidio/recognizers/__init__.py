"""Russian PII recognizers for Presidio."""

from recognizers.ru_phone import RuPhoneRecognizer
from recognizers.ru_email import RuEmailRecognizer
from recognizers.ru_inn import RuInnRecognizer
from recognizers.ru_snils import RuSnilsRecognizer
from recognizers.ru_passport import RuPassportRecognizer
from recognizers.ru_bank_card import RuBankCardRecognizer
from recognizers.ru_address import RuAddressRecognizer

ALL_RECOGNIZERS = [
    RuPhoneRecognizer,
    RuEmailRecognizer,
    RuInnRecognizer,
    RuSnilsRecognizer,
    RuPassportRecognizer,
    RuBankCardRecognizer,
    RuAddressRecognizer,
]
