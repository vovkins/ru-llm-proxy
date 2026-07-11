"""Russian counterparty and bank-requisite recognizers."""

import re

from presidio_analyzer import Pattern, PatternRecognizer


_DIGIT_GROUP_SEPARATOR = r"[\s\-]?"
_BIK_CONTEXT_RE = re.compile(
    r"(?i)(?:\bбик\b|банковск\w*\s+идентификационн\w*\s+код\w*)"
)
_BIK_RE = re.compile(r"(?<!\d)([0-2]\d{8})(?!\d)")
_ACCOUNT_CHECK_WEIGHTS = (7, 1, 3)
_ACCOUNT_CONTEXT_BOOST_SCORE = 0.55


def _digits_only(value: str) -> str:
    """Return only decimal digits from a formatted value."""
    return "".join(ch for ch in value if ch.isdigit())


def _has_repeated_digits(digits: str) -> bool:
    """Return whether all digits are the same."""
    return len(set(digits)) == 1


def _validate_ogrn(digits: str) -> bool:
    """Validate 13-digit Russian OGRN checksum."""
    return (
        len(digits) == 13
        and digits.isdigit()
        and digits[0] in {"1", "5"}
        and (int(digits[:12]) % 11) % 10 == int(digits[12])
    )


def _validate_ogrnip(digits: str) -> bool:
    """Validate 15-digit Russian OGRNIP checksum."""
    return (
        len(digits) == 15
        and digits.isdigit()
        and digits[0] == "3"
        and (int(digits[:14]) % 13) % 10 == int(digits[14])
    )


def _validate_bik(digits: str) -> bool:
    """Validate Russian BIK structure without external directory lookup."""
    return (
        len(digits) == 9
        and digits.isdigit()
        and digits[0] in {"0", "1", "2"}
        and not _has_repeated_digits(digits)
    )


def _account_checksum_valid(account: str, bik: str, *, correspondent: bool) -> bool:
    """Validate a Russian bank account control key against a nearby BIK."""
    if not (_validate_bik(bik) and len(account) == 20 and account.isdigit()):
        return False

    prefix = f"0{bik[4:6]}" if correspondent else bik[-3:]
    control_value = sum(
        (int(ch) * _ACCOUNT_CHECK_WEIGHTS[index % 3]) % 10
        for index, ch in enumerate(prefix + account)
    )
    return control_value % 10 == 0


def _find_contextual_biks(text: str, start: int, end: int, window: int = 160) -> list[str]:
    """Find BIK values near an account span when a BIK label is also nearby."""
    left = max(0, start - window)
    right = min(len(text), end + window)
    candidates = []
    for match in _BIK_RE.finditer(text[left:right]):
        bik_start = left + match.start()
        bik_end = left + match.end()
        context_left = max(0, bik_start - 40)
        context_right = min(len(text), bik_end + 40)
        if _BIK_CONTEXT_RE.search(text[context_left:context_right]):
            candidates.append(match.group(1))
    return candidates


class RuKppRecognizer(PatternRecognizer):
    """Recognize Russian tax registration reason codes (KPP)."""

    PATTERNS = [
        Pattern(
            name="ru_kpp_9digit",
            regex=rf"(?<!\d)\d{{4}}{_DIGIT_GROUP_SEPARATOR}\d{{2}}"
            rf"{_DIGIT_GROUP_SEPARATOR}\d{{3}}(?!\d)",
            score=0.2,
        ),
    ]

    CONTEXT = [
        "кпп",
        "код причины постановки",
        "постановка на учет",
        "постановка на учёт",
        "реквизиты",
        "налоговый учет",
        "налоговый учёт",
    ]

    def __init__(
        self,
        name: str = "RuKppRecognizer",
        supported_language: str = "ru",
        supported_entity: str = "RU_KPP",
    ):
        super().__init__(
            supported_entity=supported_entity,
            patterns=self.PATTERNS,
            context=self.CONTEXT,
            name=name,
            supported_language=supported_language,
        )

    def invalidate_result(self, pattern_text: str) -> bool:
        """Reject structurally invalid KPP-like digit runs."""
        digits = _digits_only(pattern_text)
        return (
            len(digits) != 9
            or _has_repeated_digits(digits)
            or digits[:4] == "0000"
            or digits[-3:] == "000"
        )


class RuOgrnRecognizer(PatternRecognizer):
    """Recognize Russian OGRN values with checksum validation."""

    PATTERNS = [
        Pattern(
            name="ru_ogrn_13digit",
            regex=r"(?<!\d)[15]\d{12}(?!\d)",
            score=0.45,
        ),
    ]

    CONTEXT = [
        "огрн",
        "основной государственный регистрационный номер",
        "егрюл",
        "регистрационный номер",
        "реквизиты",
    ]

    def __init__(
        self,
        name: str = "RuOgrnRecognizer",
        supported_language: str = "ru",
        supported_entity: str = "RU_OGRN",
    ):
        super().__init__(
            supported_entity=supported_entity,
            patterns=self.PATTERNS,
            context=self.CONTEXT,
            name=name,
            supported_language=supported_language,
        )

    def validate_result(self, pattern_text: str):
        """Keep base score for valid OGRN and reject invalid checksums."""
        return None

    def invalidate_result(self, pattern_text: str) -> bool:
        """Reject OGRN-like numbers with invalid checksum."""
        return not _validate_ogrn(_digits_only(pattern_text))


class RuOgrnipRecognizer(PatternRecognizer):
    """Recognize Russian OGRNIP values with checksum validation."""

    PATTERNS = [
        Pattern(
            name="ru_ogrnip_15digit",
            regex=r"(?<!\d)3\d{14}(?!\d)",
            score=0.45,
        ),
    ]

    CONTEXT = [
        "огрнип",
        "основной государственный регистрационный номер ип",
        "егрип",
        "индивидуальный предприниматель",
        "регистрационный номер",
        "реквизиты",
    ]

    def __init__(
        self,
        name: str = "RuOgrnipRecognizer",
        supported_language: str = "ru",
        supported_entity: str = "RU_OGRNIP",
    ):
        super().__init__(
            supported_entity=supported_entity,
            patterns=self.PATTERNS,
            context=self.CONTEXT,
            name=name,
            supported_language=supported_language,
        )

    def validate_result(self, pattern_text: str):
        """Keep base score for valid OGRNIP and reject invalid checksums."""
        return None

    def invalidate_result(self, pattern_text: str) -> bool:
        """Reject OGRNIP-like numbers with invalid checksum."""
        return not _validate_ogrnip(_digits_only(pattern_text))


class RuBikRecognizer(PatternRecognizer):
    """Recognize Russian bank identification codes (BIK)."""

    PATTERNS = [
        Pattern(
            name="ru_bik_9digit",
            regex=r"(?<!\d)[0-2]\d{8}(?!\d)",
            score=0.2,
        ),
    ]

    CONTEXT = [
        "бик",
        "банковский идентификационный код",
        "код банка",
        "банк получателя",
        "банк плательщика",
        "реквизиты банка",
    ]

    def __init__(
        self,
        name: str = "RuBikRecognizer",
        supported_language: str = "ru",
        supported_entity: str = "RU_BIK",
    ):
        super().__init__(
            supported_entity=supported_entity,
            patterns=self.PATTERNS,
            context=self.CONTEXT,
            name=name,
            supported_language=supported_language,
        )

    def invalidate_result(self, pattern_text: str) -> bool:
        """Reject structurally invalid BIK-like digit runs."""
        return not _validate_bik(_digits_only(pattern_text))


class _RuBankAccountRecognizer(PatternRecognizer):
    """Base recognizer for Russian bank account numbers."""

    def analyze(self, text, entities, nlp_artifacts=None, regex_flags=None):
        """Run pattern analysis and prune accounts that fail nearby BIK checksum."""
        results = super().analyze(text, entities, nlp_artifacts, regex_flags)
        kept = []
        for result in results:
            account = _digits_only(text[result.start:result.end])
            nearby_biks = _find_contextual_biks(text, result.start, result.end)
            if nearby_biks and not any(
                _account_checksum_valid(
                    account,
                    bik,
                    correspondent=self._is_correspondent_account,
                )
                for bik in nearby_biks
            ):
                continue
            if self._has_required_context(text, result.start, result.end):
                result.score = max(result.score, _ACCOUNT_CONTEXT_BOOST_SCORE)
            kept.append(result)
        return kept

    def invalidate_result(self, pattern_text: str) -> bool:
        """Reject structurally invalid 20-digit account-like values."""
        digits = _digits_only(pattern_text)
        return (
            len(digits) != 20
            or _has_repeated_digits(digits)
            or self._has_invalid_account_prefix(digits)
        )

    @property
    def _is_correspondent_account(self) -> bool:
        raise NotImplementedError

    def _has_invalid_account_prefix(self, digits: str) -> bool:
        raise NotImplementedError

    def _has_required_context(self, text: str, start: int, end: int) -> bool:
        left = max(0, start - 70)
        right = min(len(text), end + 70)
        return bool(self._required_context_re.search(text[left:right]))


class RuSettlementAccountRecognizer(_RuBankAccountRecognizer):
    """Recognize Russian settlement account numbers."""

    PATTERNS = [
        Pattern(
            name="ru_settlement_account_20digit",
            regex=rf"(?<!\d)\d{{5}}{_DIGIT_GROUP_SEPARATOR}\d{{3}}"
            rf"{_DIGIT_GROUP_SEPARATOR}\d{{1}}{_DIGIT_GROUP_SEPARATOR}\d{{4}}"
            rf"{_DIGIT_GROUP_SEPARATOR}\d{{7}}(?!\d)",
            score=0.2,
        ),
    ]

    CONTEXT = [
        "расчетный счет",
        "расчётный счёт",
        "р/с",
        "р.с.",
        "счет получателя",
        "счёт получателя",
        "счет плательщика",
        "счёт плательщика",
        "банковские реквизиты",
    ]
    _required_context_re = re.compile(
        r"(?i)(?:"
        r"расч[её]тн\w*\s+сч[её]т\w*|"
        r"\bр\s*/\s*с\b|"
        r"\bр\s*\.?\s*с\.?\b|"
        r"сч[её]т\w*\s+(?:получател\w*|плательщик\w*)|"
        r"банковск\w*\s+реквизит\w*"
        r")"
    )

    def __init__(
        self,
        name: str = "RuSettlementAccountRecognizer",
        supported_language: str = "ru",
        supported_entity: str = "RU_SETTLEMENT_ACCOUNT",
    ):
        super().__init__(
            supported_entity=supported_entity,
            patterns=self.PATTERNS,
            context=self.CONTEXT,
            name=name,
            supported_language=supported_language,
        )

    @property
    def _is_correspondent_account(self) -> bool:
        return False

    def _has_invalid_account_prefix(self, digits: str) -> bool:
        return digits.startswith("301")


class RuCorrespondentAccountRecognizer(_RuBankAccountRecognizer):
    """Recognize Russian correspondent account numbers."""

    PATTERNS = [
        Pattern(
            name="ru_correspondent_account_20digit",
            regex=rf"(?<!\d)301\d{{2}}{_DIGIT_GROUP_SEPARATOR}\d{{3}}"
            rf"{_DIGIT_GROUP_SEPARATOR}\d{{1}}{_DIGIT_GROUP_SEPARATOR}\d{{4}}"
            rf"{_DIGIT_GROUP_SEPARATOR}\d{{7}}(?!\d)",
            score=0.2,
        ),
    ]

    CONTEXT = [
        "корреспондентский счет",
        "корреспондентский счёт",
        "корр счет",
        "корр счёт",
        "кор. счет",
        "кор. счёт",
        "корсчет",
        "корсчёт",
        "к/с",
        "к.с.",
        "банковские реквизиты",
    ]
    _required_context_re = re.compile(
        r"(?i)(?:"
        r"корр(?:еспондентск\w*|\.)?\s*сч[её]т\w*|"
        r"корсч[её]т\w*|"
        r"\bк\s*/\s*с\b|"
        r"\bк\s*\.?\s*с\.?\b|"
        r"банковск\w*\s+реквизит\w*"
        r")"
    )

    def __init__(
        self,
        name: str = "RuCorrespondentAccountRecognizer",
        supported_language: str = "ru",
        supported_entity: str = "RU_CORRESPONDENT_ACCOUNT",
    ):
        super().__init__(
            supported_entity=supported_entity,
            patterns=self.PATTERNS,
            context=self.CONTEXT,
            name=name,
            supported_language=supported_language,
        )

    @property
    def _is_correspondent_account(self) -> bool:
        return True

    def _has_invalid_account_prefix(self, digits: str) -> bool:
        return not digits.startswith("301")
