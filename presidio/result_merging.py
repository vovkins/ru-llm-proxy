"""Source-aware merging for overlapping Analyzer results."""

from __future__ import annotations

import re
from bisect import bisect_left, bisect_right
from dataclasses import dataclass
from functools import lru_cache
from typing import Iterable

from presidio_analyzer import RecognizerResult
from string_spans import StringSpans


DETECTION_SOURCE_METADATA_KEY = "ru_llm_detection_source"
NER_MODEL_METADATA_KEY = "ru_llm_ner_model"
# Set by a model profile whose subword names may stop inside a Latin word.
NER_NAME_WORD_PIECES_KEY = "ru_llm_ner_name_word_pieces"

SOURCE_NER = "ner"
SOURCE_NATIVE_CREDENTIAL = "native_credential"
SOURCE_STRUCTURAL = "structural"
SOURCE_CONTEXTUAL_CONTRACT = "contextual_contract"
SOURCE_NONE = "none"

CONTRACT_CONTEXT_SCORE = 0.85
CONTRACT_CONTEXT_RECOGNIZER_NAME = "ContractContextFallback"

_KNOWN_SOURCES = frozenset(
    {
        SOURCE_NER,
        SOURCE_NATIVE_CREDENTIAL,
        SOURCE_STRUCTURAL,
        SOURCE_CONTEXTUAL_CONTRACT,
    }
)
_STRUCTURED_CONTAINER_TYPES = frozenset(
    {"PRIVATE_KEY", "DB_URL", "JWT", "API_KEY", "BASE64_DATA"}
)
_CREDENTIAL_ENTITY_TYPES = frozenset(
    {"LOGIN", "PASSWORD", "AUTH_TOKEN", "SECRET_KEY"}
)
_INERT_NER_CREDENTIAL_VALUES = frozenset(
    {
        "changeme",
        "example",
        "false",
        "none",
        "null",
        "placeholder",
        "sample",
        "test",
        "true",
        "your-key",
        "your-token",
        "your_api_key",
    }
)
_ENTITY_SPECIFICITY = {
    "PRIVATE_KEY": 0,
    "DB_URL": 1,
    "JWT": 2,
    "BASE64_DATA": 3,
    "API_KEY": 4,
    "BEARER_TOKEN": 5,
    "SECRET_KEY": 5,
    "AUTH_TOKEN": 6,
}

_CONTRACT_CONTEXT_RE = re.compile(
    r"(?i)(?<![\w])(?:"
    r"договор(?:а|у|ом|е|ы|ов|ам|ами|ах)?|"
    r"контракт(?:а|у|ом|е|ы|ов|ам|ами|ах)?|"
    r"госконтракт(?:а|у|ом|е|ы|ов|ам|ами|ах)?|"
    r"гос\.?\s+контракт(?:а|у|ом|е|ы|ов|ам|ами|ах)?|"
    r"государственн(?:ый|ого|ому|ым|ом|ые|ых|ыми)\s+"
    r"контракт(?:а|у|ом|е|ы|ов|ам|ами|ах)?|"
    r"соглашени(?:е|я|ю|ем|и|й|ям|ями|ях)|"
    r"contracts?|agreements?"
    r")(?![\w])"
)
# Identifier keys are assignment evidence, never sentence-wide context.
_CONTRACT_KEY_RE = re.compile(
    r"(?i)(?<![^\W_])(?:contract|agreement)s?[_\-.]?(?:id|no|nr|num|number|ref)$"
    r"|(?<![^\W_])(?:номер_договор\w*|nomer_dogovora|"
    r"dogovora?[_\-.]?(?:id|no|nr|num|nomer|number))$"
)
_CONTRACT_ASSIGNMENT_RE = re.compile(
    r"(?<![\w.\-])(?P<key>\"(?:[^\"\\\r\n]|\\.)+\"|"
    r"'(?:[^'\\\r\n]|\\.)+'|[A-Za-zА-Яа-яЁё_][\w.\-]*)"
    r"[ \t]*(?P<op>[:=])(?![:=])[ \t\r\n]*"
)
_CONTRACT_LABEL_KEY_RE = re.compile(
    r"(?i)(?:номер\s+(?:договора|контракта|соглашения)|"
    r"(?:contract|agreement)\s+(?:number|no\.?|id|ref))"
)
_CONTRACT_SCALAR_RE = re.compile(r"[^\s,;{}\[\]():=]+")
_CONTRACT_KEY_SEPARATOR_RE = re.compile(r"\s*:")
_CONTRACT_HINT_RE = re.compile(r"(?i)договор|контракт|соглашени|contract|agreement|nomer_dogovora|dogovor|\\u")

_CONTRACT_CANDIDATE_RE = re.compile(
    r"(?<![\w])"
    r"(?P<value>[A-Za-zА-Яа-яЁё0-9]"
    r"[A-Za-zА-Яа-яЁё0-9./\-\u00ad]{2,63})"
    r"(?![\w])"
)
_CONTRACT_DISQUALIFIER_RE = re.compile(
    r"(?i)(?<![\w])(?:верси(?:я|и|ю|ей)|редакци(?:я|и|ю|ей)|"
    r"шаблон(?:а|у|ом|е|ы|ов)?|template|version|revision)(?![\w])"
)
_EXPLICIT_CONTRACT_NUMBER_RE = re.compile(
    r"(?i)(?:номер|№|#|\bno\.?\b|\bnumber\b)"
)
_DATE_LIKE_RE = re.compile(r"\d{1,2}[./-]\d{1,2}[./-]\d{2,4}")
_DOTTED_VERSION_RE = re.compile(r"\d{1,3}(?:\.\d{1,3}){1,3}")
_SENTENCE_BOUNDARIES = frozenset(".!?;。！？")
_NAME_TYPES = frozenset({"PERSON", "ORGANIZATION"})
_JOINED_NAME_TYPES = frozenset({"PERSON", "LOCATION", "ORGANIZATION"})
_NAME_JOIN_GAP_RE = re.compile(r"[\s«»\"'„“”‘’\-–—]{1,3}")
_NAME_QUOTE_PAIRS = (("«", "»"), ("„", "“"), ("“", "”"))
_ESCAPE_LETTERS = frozenset('ntrbfu"/\\')
_ORG_GENERIC_LEAD_RE = re.compile(r"(?i)(?:организация|компания)[ \t\u00a0]+(?=[^\W\d_])")
# Only a separator glued to the last word; "Yahoo!" and a tokenized "вести ." keep theirs.
_ORG_TRAILING_PUNCTUATION = frozenset(".;,:")
# A span that ends with an initial such as "И." keeps its period.
_TRAILING_INITIAL_RE = re.compile(r"(?:^|(?<=[\s.]))[А-ЯЁA-Z]\.$")
_LATIN_LETTERS = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ")
# One or two Cyrillic initials right after a surname: Петров И. С.
_TRAILING_INITIALS_RE = re.compile(r"[ \t\u00a0]+[А-ЯЁ]\.(?:[ \t\u00a0]?[А-ЯЁ]\.)?(?![А-Яа-яЁё])")
_IDENTIFIER_CHARACTERS = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_"
)
# camelCase, ACRONYMWord, snake_case or letters mixed with digits.
_IDENTIFIER_SHAPE_RE = re.compile(r"[a-z][A-Z]|[A-Z]{2}[a-z]|_|[A-Za-z][0-9]|[0-9][A-Za-z]")
# Generic model predictions that never correct another detection's boundaries.
_STATISTICAL_RECOGNIZERS = frozenset({"SpacyRecognizer"})
# Rules that only add coverage; their open parts survive any overlap.
_ADDITIVE_RECOGNIZERS = frozenset({"RuAddressChainRecognizer", "RuPassportExtRecognizer"})
_LEGAL_FORM_RE = re.compile(r"(?:ООО|АО|ПАО|ОАО|ЗАО)", re.IGNORECASE)
_NAME_WORDS_RE = re.compile(r"[А-Яа-яЁё]+(?:[- \t\u00a0][А-Яа-яЁё]+){1,2}")
_PERSON_FIELD_RE = re.compile(r"(?i)(?<!\w)(?:фио|ф\.и\.о\.)\s*[:=]\s*$")
_PERSON_LABEL_RE = re.compile(
    r"(?im)(?:^|(?<=[;\r]))[ \t\u00a0]{0,16}"
    r"(?P<label>фио|ф\.и\.о\.)[ \t\u00a0]{0,16}[:=][ \t\u00a0]{0,16}"
)
_NAME_WORD = r"[А-ЯЁ][А-Яа-яЁё]{1,39}(?:-[А-ЯЁ][А-Яа-яЁё]{1,39})?"
_NAME_VALUE = rf"{_NAME_WORD}(?:[ \t\u00a0]{{1,16}}{_NAME_WORD}){{1,2}}"
_EXPLICIT_NAME_RE = re.compile(
    rf"(?<!\w)(?:"
    rf"(?P<person>(?i:фио|ф\.и\.о\.|full_name|полное имя|пользователь))"
    rf"[\"']?[ \t\u00a0]{{0,16}}[:=](?:[ \t\u00a0]{{0,16}}[\"'])?|"
    rf"(?P<organization>(?i:название организации))[ \t\u00a0]{{0,16}}[:=]|"
    rf"(?P<legal>(?i:ООО|АО|ПАО|ОАО|ЗАО))[ \t\u00a0]{{1,16}}"
    rf")(?(legal)|[ \t\u00a0]{{0,16}})(?P<value>{_NAME_VALUE})(?![\w-])"
)
_COMPANY_NAME_RE = re.compile(
    rf"(?<!\w)(?i:компания)[ \t\u00a0]{{1,16}}"
    rf"(?P<value>{_NAME_VALUE})(?![\w-])"
)
_NAME_FRAGMENT_TYPES = _NAME_TYPES | {"LOGIN"}
_SERVICE_NUMBER_RE = re.compile(
    r"(?i)(?<!\w)(?:номер|идентификатор)\s+"
    r"(?:заявки|заказа|строки)\s*[:=#№]?\s*$"
)
_INSTRUCTION_SPACE = r"[ \t\u00a0]{1,16}"
_LOGIN_INSTRUCTION_RE = re.compile(
    rf"(?:^|[.!?;]{_INSTRUCTION_SPACE}|[\r\n][ \t\u00a0]{{0,16}})"
    rf"(?:please{_INSTRUCTION_SPACE})?"
    rf"(?P<command>reply|respond|answer|return|print|output|copy)"
    rf"{_INSTRUCTION_SPACE}(?:only|exactly|solely|exclusively|just)"
    rf"{_INSTRUCTION_SPACE}"
    rf"(?:(?:with|using){_INSTRUCTION_SPACE})?"
    rf"(?:the|that|this){_INSTRUCTION_SPACE}"
    rf"(?:email(?:{_INSTRUCTION_SPACE}address)?|address|value|text|result)\b",
    re.IGNORECASE,
)


def _login_instruction_refinements(
    text: str, results: list[RecognizerResult]
) -> dict[tuple[int, int], int]:
    candidates = {}
    for result in results:
        if (
            detection_source(result) == SOURCE_NER
            and result.entity_type == "LOGIN"
            and _valid_span(result, len(text))
        ):
            candidates.setdefault(result.end, []).append(result)
    if not candidates:
        return {}
    independent = sorted(
        (r.start, r.end)
        for r in results
        if detection_source(r) != SOURCE_NER and _valid_span(r, len(text))
    )
    starts, max_ends = [], []
    for start, end in independent:
        starts.append(start)
        max_ends.append(max(end, max_ends[-1] if max_ends else 0))
    confirmed = {}
    for match in _LOGIN_INSTRUCTION_RE.finditer(text):
        start, end = match.span("command")
        if end not in candidates:
            continue
        prefix = text[max(0, match.start() - 32):match.start()].rstrip(" \t\u00a0\r")
        if prefix.endswith(("=", ":")):
            continue
        # Independent recognizers veto suppression, including wider containers.
        index = bisect_left(starts, end) - 1
        if index >= 0 and max_ends[index] > start:
            continue
        for result in candidates[end]:
            if result.start == start:
                confirmed[(result.start, result.end)] = result.start
            elif result.start < match.start():
                trim_end = match.start()
                # A newline does not prove preceding punctuation is a separator.
                while trim_end > result.start and text[trim_end - 1] in " \t\u00a0\r\n":
                    trim_end -= 1
                if trim_end > result.start:
                    confirmed[(result.start, result.end)] = trim_end
    return confirmed


@dataclass(frozen=True)
class MergeDecision:
    """One bounded, value-free decision made while merging results."""

    reason: str
    winner_source: str
    loser_source: str


@dataclass(frozen=True)
class MergeOutcome:
    """Merged results and safe diagnostics for observability."""

    results: tuple[RecognizerResult, ...]
    decisions: tuple[MergeDecision, ...]


@dataclass(frozen=True)
class NameContext:
    """Bounded grammar evidence; never sufficient for a new detection alone."""

    entity_type: str
    start: int
    end: int
    legal_form_end: int | None = None


def name_context_evidence(text: str) -> tuple[NameContext, ...]:
    contexts = []
    for pattern in (_EXPLICIT_NAME_RE, _COMPANY_NAME_RE):
        for match in pattern.finditer(text):
            start, end = match.span("value")
            # Never interpret a truncated three-word prefix of a longer name.
            tail = text[end : end + 18]
            if re.match(r"[ \t\u00a0]{1,16}[А-ЯЁ]", tail):
                continue
            if pattern is _COMPANY_NAME_RE:
                prefix = text[max(0, match.start() - 64) : match.start()].rstrip(" \t\u00a0")
                if not prefix and match.start() > 64:
                    continue
                if prefix and prefix[-1] not in ".!?;\r\n":
                    continue
                entity_type = "ORGANIZATION"
                legal_end = None
            else:
                entity_type = "PERSON" if match.group("person") else "ORGANIZATION"
                legal_end = match.end("legal") if match.group("legal") else None
                if entity_type == "PERSON" and any(
                    _LEGAL_FORM_RE.fullmatch(word) for word in text[start:end].split()
                ):
                    continue
                if legal_end is not None:
                    start = match.start("legal")
            if end - start <= 128:
                contexts.append(NameContext(entity_type, start, end, legal_end))
    return tuple(dict.fromkeys(contexts))


def detection_source(result: RecognizerResult) -> str:
    """Return a bounded source identifier for a result."""
    metadata = result.recognition_metadata or {}
    source = metadata.get(DETECTION_SOURCE_METADATA_KEY)
    if source in _KNOWN_SOURCES:
        return str(source)
    return SOURCE_STRUCTURAL


def _contract_candidates(text):
    """The existing number policy, independent of how its context is confirmed."""
    for match in _CONTRACT_CANDIDATE_RE.finditer(text):
        value = match.group("value")
        if not any(character.isdigit() for character in value):
            continue
        normalized = value.replace("\u00ad", "")
        if (
            _DATE_LIKE_RE.fullmatch(normalized)
            or _DOTTED_VERSION_RE.fullmatch(normalized)
            or (normalized.isdigit() and len(normalized) < 6)
        ):
            continue
        start, end = match.span("value")
        while end > start and text[end - 1] in ".-/\u00ad":
            end -= 1
        if end - start >= 3:
            yield start, end


def _contract_key(key):
    return bool(_CONTRACT_KEY_RE.search(key) or _CONTRACT_CONTEXT_RE.fullmatch(key)
                or _CONTRACT_LABEL_KEY_RE.fullmatch(key))


def _literal_view(text, literal):
    """Reuse validated JSON escape projection; leave other language literals raw."""
    import json
    from recognizers.json_strings import decoded_string_offsets

    prefix = text[max(0, literal.start - 3):literal.start]
    raw = re.search(r"(?i)(?<!\w)(?:r|br|rb|fr|rf)$", prefix) is not None
    if not raw and text[literal.start] == '"':
        try:
            json.loads(text[literal.start:literal.end])
        except (ValueError, RecursionError):
            pass
        else:
            return decoded_string_offsets(text, literal.start, literal.end)
    return (text[literal.content_start:literal.content_end],
            range(literal.content_start, literal.content_end + 1))


def _contract_bindings(text, strings):
    """Bind each assignment to one scalar, never to a container or next field."""
    import ast

    bindings = []
    keys = []
    for match in _CONTRACT_ASSIGNMENT_RE.finditer(text):
        left, right = match.span("key")
        owner = strings.at(left)
        if owner is not None and (owner.start, owner.end) != (left, right):
            # Analyze textual assignments inside a source string in its own view.
            continue
        key = match.group("key")
        quoted = key[0] in "\"'"
        if quoted:
            try:
                key = ast.literal_eval(key)
            except (ValueError, SyntaxError):
                continue
        before = left - 1
        while before >= 0 and text[before] in " \t":
            before -= 1
        structured = (quoted or match.group("op") == "=" or before < 0
                      or text[before] in "\r\n{[(,;")
        confirmed = _contract_key(key)
        keys.append((left, right))
        start = match.end()
        literal = strings.at(start)
        if literal is not None and literal.start == start:
            decoded, offsets = _literal_view(text, literal)
            bindings.append((start, literal.end, confirmed, structured, decoded, offsets))
        else:
            scalar = _CONTRACT_SCALAR_RE.match(text, start)
            if scalar:
                end = scalar.end()
                bindings.append((start, end, confirmed, structured,
                                 text[start:end], range(start, end + 1)))
                if (not quoted and match.group("op") == ":"
                        and _CONTRACT_CONTEXT_RE.fullmatch(key)
                        and next(_contract_candidates(text[start:end]), None) is not None):
                    # A prose label such as "Договоры: AA-... и BB-..." still
                    # introduces a list. Other assignment values remain scoped
                    # independently; null/containers never introduce prose.
                    keys.pop()
    return bindings, keys


def _plain_contract_evidence(text, strings):
    bindings, keys = _contract_bindings(text, strings)
    evidence = []
    for _, _, confirmed, _, decoded, offsets in bindings:
        if confirmed:
            evidence.extend((offsets[start], offsets[end])
                            for start, end in _contract_candidates(decoded))

    key_starts = [start for start, _ in keys]
    binding_starts = [start for start, *_ in bindings]
    for start, end in _contract_candidates(text):
        key_index = bisect_right(key_starts, start) - 1
        if key_index >= 0 and start < keys[key_index][1]:
            continue
        literal = strings.at(start)
        binding_index = bisect_right(binding_starts, start) - 1
        if binding_index >= 0:
            left, right, _, structured, _, _ = bindings[binding_index]
            if left <= start < right and structured and literal is None:
                continue
        context_start = max(0, start - 96, literal.content_start if literal else 0)
        for index in range(start - 1, context_start - 1, -1):
            if _is_sentence_boundary(text, index):
                context_start = index + 1
                break
        context = text[context_start:start]
        has_context = False
        for match in _CONTRACT_CONTEXT_RE.finditer(text, context_start, start):
            index = bisect_right(key_starts, match.start()) - 1
            if index >= 0 and match.start() < keys[index][1]:
                continue
            if strings.at(match.start()) is literal:
                has_context = True
                break
        disqualified = _CONTRACT_DISQUALIFIER_RE.search(context) is not None
        explicit = _EXPLICIT_CONTRACT_NUMBER_RE.search(context) is not None
        if has_context and (not disqualified or explicit):
            evidence.append((start, end))
    return evidence


def contract_context_evidence(
    text: str, *, _strings: StringSpans | None = None,
) -> tuple[tuple[int, int], ...]:
    """Separate assignment-owned values from natural-language contract context."""
    if _CONTRACT_HINT_RE.search(text) is None:
        return ()
    strings = _strings if _strings is not None else StringSpans(text)
    evidence = _plain_contract_evidence(text, strings)
    for literal in strings.spans:
        # Keys are not free-text context for a different value.
        if _CONTRACT_KEY_SEPARATOR_RE.match(text, literal.end):
            continue
        decoded, offsets = _literal_view(text, literal)
        for start, end in _plain_contract_evidence(decoded, StringSpans(decoded)):
            evidence.append((offsets[start], offsets[end]))
    return tuple(sorted(set(evidence)))


def _is_sentence_boundary(text: str, index: int) -> bool:
    character = text[index]
    if character not in _SENTENCE_BOUNDARIES:
        return False
    if character == "." and text[max(0, index - 2) : index].casefold() == "no":
        return False
    return True


def merge_results(
    text: str,
    results: Iterable[RecognizerResult],
    *,
    requested_entities: Iterable[str] | None = None,
    score_threshold: float = 0.0,
    sentence_starts: Iterable[int] = (),
) -> MergeOutcome:
    """Merge overlapping results using source and entity specificity."""
    if requested_entities is not None:
        requested_entities = tuple(requested_entities)
    decisions = []
    valid_results = []
    strings = StringSpans(text)
    contract_evidence = contract_context_evidence(text, _strings=strings)
    reference_spans = []
    normalized = []
    for result in results:
        if not _valid_span(result, len(text)):
            normalized.append(result)  # Preserve the existing invalid-span decision.
            continue
        literal = strings.at(result.start)
        # Existing specialized rules may deliberately cover a concatenated key
        # across several code literals. Keep that established native contract;
        # Statistical/additive findings crossing syntax widen to every touched
        # value instead of losing their tail or masking a service key alone.
        bound_to_values = (
            detection_source(result) == SOURCE_NER
            or _recognizer_name(result) in _ADDITIVE_RECOGNIZERS
        )
        ranges = [(result.start, result.end)]
        if bound_to_values:
            ranges = strings.mask_ranges(
                result.start, result.end, name=result.entity_type in _JOINED_NAME_TYPES,
            )
        elif literal is None or result.end <= literal.end:
            bounds = strings.align(
                result.start, result.end,
                name=result.entity_type in _JOINED_NAME_TYPES,
            )
            ranges = [bounds] if bounds else []
        for bounds in ranges:
            normalized.append(
                result if bounds == (result.start, result.end)
                else _copy_name(result, start=bounds[0], end=bounds[1])
            )
    results = normalized
    # The experimental cascade adds coverage; it must never compete with native
    # credential or NER findings for ownership of their existing ranges.
    logistic_results = [r for r in results if _is_logistic_base64(r)]
    results = [r for r in results if not _is_logistic_base64(r)]
    login_instructions = _login_instruction_refinements(text, results)
    if any(detection_source(r) == SOURCE_NER and r.entity_type in _CREDENTIAL_ENTITY_TYPES for r in results):
        from recognizers.credential_rules import credential_reference_spans
        reference_spans = credential_reference_spans(text)

    code_identifiers = None
    for result in results:
        source = detection_source(result)
        if not _valid_span(result, len(text)):
            decisions.append(
                MergeDecision("invalid_span", SOURCE_NONE, source)
            )
            continue
        if result.entity_type in _JOINED_NAME_TYPES:
            trimmed = result if strings.at(result.start) else _skip_escape_letter(text, result)
            if trimmed is None:
                decisions.append(MergeDecision("name_escape_letter_dropped", SOURCE_NONE, source))
                continue
            result = trimmed
        if result.entity_type == "ORGANIZATION" and source == SOURCE_NER:
            trimmed = _trim_model_organization(text, result)
            if trimmed is None:
                decisions.append(MergeDecision("organization_boundary_empty", SOURCE_NONE, source))
                continue
            if trimmed is not result:
                decisions.append(MergeDecision("organization_boundary_trimmed", source, source))
            result = trimmed
        if (
            source == SOURCE_NER
            and result.entity_type == "LOGIN"
            and (result.start, result.end) in login_instructions
        ):
            end = login_instructions[(result.start, result.end)]
            if end == result.start:
                decisions.append(
                    MergeDecision("login_instruction_suppressed", SOURCE_NONE, SOURCE_NER)
                )
                continue
            result = _copy_name(result, end=end)
            decisions.append(
                MergeDecision("login_instruction_tail", SOURCE_NER, SOURCE_NER)
            )
        if (
            source == SOURCE_NER
            and result.entity_type in _CREDENTIAL_ENTITY_TYPES
            and any(start <= result.start < result.end <= end for start, end in reference_spans)
        ):
            decisions.append(MergeDecision("credential_reference_suppressed", SOURCE_NONE, SOURCE_NER))
            continue
        if (
            source == SOURCE_NER
            and result.entity_type in _CREDENTIAL_ENTITY_TYPES
            and text[result.start : result.end].strip().casefold()
            in _INERT_NER_CREDENTIAL_VALUES
        ):
            decisions.append(
                MergeDecision(
                    "credential_placeholder_suppressed",
                    SOURCE_NONE,
                    SOURCE_NER,
                )
            )
            continue
        if (
            source == SOURCE_NER
            and result.entity_type in _JOINED_NAME_TYPES
            and (result.recognition_metadata or {}).get(NER_NAME_WORD_PIECES_KEY) is True
            and _cuts_latin_word(text, result.start, result.end)
            and not _token_has_other_detection(text, result, results)
        ):
            decisions.append(MergeDecision("ner_word_piece_suppressed", SOURCE_NONE, SOURCE_NER))
            continue
        identifier = (
            _cut_code_identifier(text, result.start, result.end)
            if source == SOURCE_NER and result.entity_type == "ORGANIZATION"
            else None
        )
        if identifier is not None and not any(
            _spans_overlap(other.start, other.end, *identifier)
            for other in results
            if other.entity_type != "ORGANIZATION" or detection_source(other) != SOURCE_NER
        ):
            if code_identifiers is None:
                code_identifiers = _code_identifier_spans(text, strings)
            if identifier in code_identifiers:
                decisions.append(
                    MergeDecision("ner_identifier_fragment_suppressed", SOURCE_NONE, SOURCE_NER)
                )
                continue
        if source == SOURCE_NER and result.entity_type == "CONTRACT_NUMBER":
            confirmed = any(
                _spans_overlap(result.start, result.end, start, end)
                for start, end in contract_evidence
            )
            if not confirmed:
                decisions.append(
                    MergeDecision("contract_unconfirmed", SOURCE_NONE, source)
                )
                continue
            result = _with_detection_source(
                result,
                SOURCE_CONTEXTUAL_CONTRACT,
            )
            decisions.append(
                MergeDecision(
                    "contract_context_confirmed",
                    SOURCE_CONTEXTUAL_CONTRACT,
                    SOURCE_NER,
                )
            )
        valid_results.append(result)

    contract_requested = (
        requested_entities is None
        or "CONTRACT_NUMBER" in requested_entities
    )
    if contract_requested and CONTRACT_CONTEXT_SCORE >= score_threshold:
        for start, end in contract_evidence:
            if any(
                result.entity_type == "CONTRACT_NUMBER"
                and result.start == start
                and result.end == end
                for result in valid_results
            ):
                continue
            valid_results.append(
                RecognizerResult(
                    entity_type="CONTRACT_NUMBER",
                    start=start,
                    end=end,
                    score=CONTRACT_CONTEXT_SCORE,
                    recognition_metadata={
                        DETECTION_SOURCE_METADATA_KEY: SOURCE_CONTEXTUAL_CONTRACT,
                        RecognizerResult.RECOGNIZER_NAME_KEY: (
                            CONTRACT_CONTEXT_RECOGNIZER_NAME
                        ),
                    },
                )
            )
            decisions.append(
                MergeDecision(
                    "contract_context_fallback",
                    SOURCE_CONTEXTUAL_CONTRACT,
                    SOURCE_NONE,
                )
            )

    valid_results, name_decisions = _refine_names(
        text, valid_results, frozenset(sentence_starts), score_threshold
    )
    decisions.extend(name_decisions)
    # Declared containers keep their span from the cascade even when the request
    # excludes their class; they must not be reclassified as BASE64_DATA.
    declared_containers = [
        (r.start, r.end) for r in valid_results
        if (r.recognition_metadata or {}).get("encoded_container") is True
    ]
    if requested_entities:
        requested = frozenset(requested_entities)
        valid_results = [r for r in valid_results if r.entity_type in requested]
    # A declared encoded value owns its full span, including inner detections.
    containers = sorted(
        (r.start, r.end) for r in valid_results
        if (r.recognition_metadata or {}).get("encoded_container") is True
    )
    container_starts = [start for start, _ in containers]
    container_ends = []
    for _, end in containers:
        container_ends.append(max(end, container_ends[-1] if container_ends else 0))
    retained = []
    for result in valid_results:
        index = bisect_right(container_starts, result.start) - 1
        if (
            index >= 0
            and result.end <= container_ends[index]
            and (result.recognition_metadata or {}).get("encoded_container") is not True
        ):
            decisions.append(MergeDecision(
                "overlap_preferred_source", SOURCE_STRUCTURAL, detection_source(result)
            ))
        else:
            retained.append(result)
    valid_results = retained
    ordered = sorted(valid_results, key=_priority_key)
    kept: list[RecognizerResult] = []
    overlapped: list[RecognizerResult] = []
    for candidate in ordered:
        conflict = next(
            (
                existing
                for existing in kept
                if _spans_overlap(
                    candidate.start,
                    candidate.end,
                    existing.start,
                    existing.end,
                )
            ),
            None,
        )
        if conflict is None:
            kept.append(candidate)
            continue

        decisions.append(
            MergeDecision(
                _conflict_reason(conflict, candidate),
                detection_source(conflict),
                detection_source(candidate),
            )
        )
        overlapped.append(candidate)

    for fragment in _uncovered_fragments(text, kept, overlapped):
        kept.append(fragment)
        decisions.append(
            MergeDecision("overlap_fragment_kept", detection_source(fragment), SOURCE_NONE)
        )
    kept, joined = _join_name_fragments(text, kept, strings)
    decisions.extend(
        MergeDecision("name_fragments_joined", detection_source(r), detection_source(r))
        for r in joined
    )

    kept.sort(key=lambda result: (result.start, result.end, result.entity_type))
    if logistic_results:
        kept, added_decisions = _add_logistic_coverage(
            text, kept, logistic_results, requested_entities, score_threshold,
            declared_containers,
        )
        decisions.extend(added_decisions)
    return MergeOutcome(tuple(kept), tuple(decisions))


def _trim_model_organization(text: str, result: RecognizerResult) -> RecognizerResult | None:
    """Drop a glued closing separator and a leading generic noun from a model organization.

    ``ООО Вектор.`` and ``Организация Northwind Research Ltd`` name the same
    companies as ``ООО Вектор`` and ``Northwind Research Ltd``. Only ``. ; , :``
    glued to the last word are dropped, and a trailing initial such as ``И.``
    keeps its period. Instruction tails after a sentence are handled by the
    name refinement below.
    """
    start, end = result.start, result.end
    lead = _ORG_GENERIC_LEAD_RE.match(text, start, end)
    if lead is not None:
        start = lead.end()
    while (
        end - start > 1
        and text[end - 1] in _ORG_TRAILING_PUNCTUATION
        and not text[end - 2].isspace()
        and _TRAILING_INITIAL_RE.search(text, start, end) is None
    ):
        end -= 1
    if (start, end) == (result.start, result.end):
        return result
    if not any(character.isalnum() for character in text[start:end]):
        return None
    return _copy_name(result, start=start, end=end)


def _skip_escape_letter(text: str, result: RecognizerResult) -> RecognizerResult | None:
    """Move a name start off the letter of a backslash escape such as ``\\nСервис``.

    Masking ``nСервис`` would leave a lone backslash and break the JSON or
    code string; a name that was only that letter is dropped.
    """
    start = result.start
    backslashes = 0
    cursor = start - 1
    while cursor >= 0 and text[cursor] == "\\":
        backslashes += 1
        cursor -= 1
    if backslashes % 2 and text[start] in _ESCAPE_LETTERS:
        start += 1
        if not any(character.isalnum() for character in text[start : result.end]):
            return None
        return _copy_name(result, start=start)
    return result


def _join_name_fragments(
    text: str,
    kept: list[RecognizerResult],
    strings: StringSpans,
) -> tuple[list[RecognizerResult], list[RecognizerResult]]:
    """Join same-type model names split by spaces, quotes or dashes; close edge quotes.

    ``ООО`` + ``«Лаборатория Север»`` or ``Москва - Гавана`` are one value; a
    gap of punctuation between two masks would otherwise stay visible. A
    model name that leaves a quote pair half-open takes the neighbouring quote,
    and a model surname takes the initials right after it (``Петров И. С.``),
    when no other result owns those characters. Deterministic results keep
    their boundaries.
    """
    ordered = sorted(kept, key=lambda r: (r.start, r.end))
    joined: list[RecognizerResult] = []
    out: list[RecognizerResult] = []
    for result in ordered:
        last = out[-1] if out else None
        if (
            last is not None
            and result.entity_type == last.entity_type
            and result.entity_type in _JOINED_NAME_TYPES
            and _is_statistical(last)
            and _is_statistical(result)
            and last.end <= result.start
            and strings.same_value(last.start, result.start)
            and _NAME_JOIN_GAP_RE.fullmatch(text[last.end : result.start])
        ):
            out[-1] = _copy_name(last, end=result.end, score=max(last.score, result.score))
            joined.append(out[-1])
        else:
            out.append(result)
    for index, result in enumerate(out):
        if result.entity_type not in _JOINED_NAME_TYPES or not _is_statistical(result):
            continue
        start, end = _balance_name_quotes(text, result.start, result.end)
        if result.entity_type == "PERSON":
            initials = _TRAILING_INITIALS_RE.match(text, end)
            if initials is not None:
                end = initials.end()
        bounds = strings.align(start, end)
        if bounds is None:
            continue
        start, end = bounds
        previous_end = out[index - 1].end if index > 0 else 0
        next_start = out[index + 1].start if index + 1 < len(out) else len(text)
        if (start, end) != (result.start, result.end) and start >= previous_end and end <= next_start:
            out[index] = _copy_name(result, start=start, end=end)
    return out, joined


def _balance_name_quotes(text: str, start: int, end: int) -> tuple[int, int]:
    value = text[start:end]
    for opening, closing in _NAME_QUOTE_PAIRS:
        if value.count(opening) > value.count(closing) and end < len(text) and text[end] == closing:
            end += 1
        elif value.count(closing) > value.count(opening) and start > 0 and text[start - 1] == opening:
            start -= 1
    if value.count('"') % 2 == 1:
        if end < len(text) and text[end] == '"':
            end += 1
        elif start > 0 and text[start - 1] == '"':
            start -= 1
    return start, end


def _cuts_latin_word(text: str, start: int, end: int) -> bool:
    """Whether a span starts or ends between two Latin letters (``Don|e``)."""
    return (
        (start > 0 and text[start - 1] in _LATIN_LETTERS and text[start] in _LATIN_LETTERS)
        or (end < len(text) and text[end - 1] in _LATIN_LETTERS and text[end] in _LATIN_LETTERS)
    )


def _token_has_other_detection(
    text: str,
    result: RecognizerResult,
    results: list[RecognizerResult],
) -> bool:
    """Whether a non-name detection touches the whitespace-delimited token.

    A model may cover a secret or login with name pieces next to other
    pieces; those must stay masked.
    """
    start, end = result.start, result.end
    while start > 0 and not text[start - 1].isspace():
        start -= 1
    while end < len(text) and not text[end].isspace():
        end += 1
    return any(
        other is not result
        and other.entity_type not in _JOINED_NAME_TYPES
        and _spans_overlap(other.start, other.end, start, end)
        for other in results
    )


def _cut_code_identifier(text: str, start: int, end: int) -> tuple[int, int] | None:
    """Return a containing Latin identifier, without asserting that it is code.

    Shape only identifies a possible cut. The caller separately requires a
    proven code-symbol span and keeps any run touched by another detection.
    Whole words and Cyrillic text are never suppressed by this rule.
    """
    for inside, outside in ((start, start - 1), (end - 1, end)):
        if not 0 <= outside < len(text) or not start <= inside < end:
            continue
        if text[inside] not in _IDENTIFIER_CHARACTERS or text[outside] not in _IDENTIFIER_CHARACTERS:
            continue
        run_start, run_end = min(inside, outside), max(inside, outside) + 1
        while run_start > 0 and text[run_start - 1] in _IDENTIFIER_CHARACTERS:
            run_start -= 1
        while run_end < len(text) and text[run_end] in _IDENTIFIER_CHARACTERS:
            run_end += 1
        if _IDENTIFIER_SHAPE_RE.search(text[run_start:run_end]):
            return run_start, run_end
    return None


def _code_identifier_spans(text: str, strings: StringSpans) -> set[tuple[int, int]]:
    """Prove local symbol roles in valid Python and explicit Python fences.

    A string value/comment, bare word, or an unrelated code block is not proof.
    Parsing never evaluates source; invalid/unknown syntax conservatively keeps
    the detection. Only targets, callable names, imports, declarations and
    literal __all__ export entries are eligible, not arbitrary data expressions.
    """
    import ast
    import tokenize

    parts = [(0, text)]
    for match in re.finditer(
        r"(?m)^[ \t]*```(?:python|py)[ \t]*\r?\n(?P<code>[\s\S]*?)^[ \t]*```[ \t]*\r?$",
        text,
    ):
        if strings.at(match.start()) is None:
            parts.append((match.start("code"), match.group("code")))
    spans = set()
    for base, source in parts:
        try:
            tree = ast.parse(source)
        except (SyntaxError, ValueError, RecursionError):
            continue
        # Python source lines use CR/LF, not every Unicode separator recognized
        # by str.splitlines() (e.g. a form feed inside a string literal).
        lines = re.findall(r"[^\r\n]*(?:\r\n|\r|\n|$)", source)
        starts = [0]
        for line in lines:
            starts.append(starts[-1] + len(line))

        byte_columns = {}

        def offset(row, column, *, byte_column=False):
            line = lines[row - 1]
            if byte_column and not line.isascii():
                if row not in byte_columns:
                    mapping = {0: 0}
                    byte_index = 0
                    for char_index, char in enumerate(line, 1):
                        byte_index += len(char.encode("utf-8"))
                        mapping[byte_index] = char_index
                    byte_columns[row] = mapping
                column = byte_columns[row][column]
            return starts[row - 1] + column

        def bounds(node):
            return (offset(node.lineno, node.col_offset, byte_column=True),
                    offset(node.end_lineno, node.end_col_offset, byte_column=True))

        roles = []
        proved = set()
        attribute_ends = set()

        def callable_roles(node):
            while isinstance(node, ast.Attribute):
                # Do not include arguments of factory(secret).method().
                attribute_ends.add(bounds(node)[1])
                node = node.value
            if isinstance(node, ast.Name):
                roles.append(bounds(node))

        nodes = list(ast.walk(tree))
        bare_labels = {
            id(node.target) for node in nodes
            if isinstance(node, ast.AnnAssign) and node.value is None
        }
        for node in nodes:
            if (isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store)
                    and id(node) not in bare_labels):
                roles.append(bounds(node))
            elif isinstance(node, ast.Call):
                callable_roles(node.func)
            elif isinstance(node, (ast.Import, ast.ImportFrom)):
                roles.append(bounds(node))
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                start, _ = bounds(node)
                header = re.compile(
                    r"(?:async\s+)?(?:def|class)\s+(?P<name>" + re.escape(node.name) + r")\b"
                ).match(source, start)
                if header:
                    roles.append(header.span("name"))
            if isinstance(node, (ast.Assign, ast.AugAssign)):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                if (any(isinstance(t, ast.Name) and t.id == "__all__" for t in targets)
                        and isinstance(node.value, (ast.List, ast.Tuple, ast.Set))):
                    for value in node.value.elts:
                        if not (isinstance(value, ast.Constant) and isinstance(value.value, str)
                                and value.value.isascii() and value.value.isidentifier()):
                            continue
                        left, right = bounds(value)
                        index = bisect_left(strings.starts, base + left)
                        if index < len(strings.spans):
                            literal = strings.spans[index]
                            if (literal.end <= base + right
                                    and text[literal.content_start:literal.content_end] == value.value):
                                proved.add((literal.content_start, literal.content_end))
        roles.sort()
        role_starts = [start for start, _ in roles]
        role_ends = []
        for _, end in roles:
            role_ends.append(max(end, role_ends[-1] if role_ends else 0))
        try:
            reader = iter(lines)
            for token in tokenize.generate_tokens(lambda: next(reader, "")):
                if token.type != tokenize.NAME:
                    continue
                start = offset(*token.start)
                end = offset(*token.end)
                index = bisect_right(role_starts, start) - 1
                if end in attribute_ends or (index >= 0 and end <= role_ends[index]):
                    proved.add((base + start, base + end))
        except (tokenize.TokenError, IndentationError):
            # ast.parse already validated syntax; keep any uncertain names.
            continue
        spans.update(proved)
    return spans


def _recognizer_name(result: RecognizerResult) -> str:
    metadata = result.recognition_metadata or {}
    return str(metadata.get("projected_recognizer_name", metadata.get(RecognizerResult.RECOGNIZER_NAME_KEY, "")))


def _is_statistical(result: RecognizerResult) -> bool:
    return (
        detection_source(result) == SOURCE_NER
        or _recognizer_name(result) in _STATISTICAL_RECOGNIZERS
    )


def _fragment_mode(
    result: RecognizerResult,
    kept: list[RecognizerResult],
) -> str | None:
    """Which characters of an overlapped result no winner covers may stay masked.

    Additive address/passport rules complete what other detections leave
    open. A model span keeps its whole remainder when every overlapping winner
    is statistical too. Against a deterministic winner, which defines the value
    boundaries, it keeps only a token glued to the right of a winner: a value
    cut short at ``&`` continues there, while labels such as ``token=``,
    ``Bearer`` or ``no.`` precede the value and stay visible.
    """
    if _recognizer_name(result) in _ADDITIVE_RECOGNIZERS:
        return "remainder"
    if detection_source(result) != SOURCE_NER:
        return None
    if all(
        _is_statistical(winner)
        for winner in kept
        if _spans_overlap(result.start, result.end, winner.start, winner.end)
    ):
        return "remainder"
    return "glued_right"


def _uncovered_fragments(
    text: str,
    kept: list[RecognizerResult],
    overlapped: list[RecognizerResult],
) -> list[RecognizerResult]:
    """Return the eligible parts of overlapped results that no winner covers.

    Overlap resolution drops a whole lower-priority result even when it is
    longer than the winner, so its other characters would reach the provider.
    Every overlapped result already passed the source filters above; each
    uncovered run keeps its type, score and metadata, trimmed of edge spaces.
    """
    modes = {id(r): _fragment_mode(r, kept) for r in overlapped}
    overlapped = [r for r in overlapped if modes[id(r)] is not None]
    if not overlapped:
        return []
    covered = bytearray(len(text))
    for result in kept:
        covered[result.start : result.end] = b"\x01" * (result.end - result.start)
    winner_ends = {result.end for result in kept}
    fragments = []
    for result in sorted(overlapped, key=lambda r: (r.start, r.start - r.end)):
        index = result.start
        while index < result.end:
            if covered[index]:
                index += 1
                continue
            run_end = index
            while run_end < result.end and not covered[run_end]:
                run_end += 1
            start, end = index, run_end
            if modes[id(result)] == "glued_right":
                if start not in winner_ends or text[start].isspace():
                    index = run_end
                    continue
                end = start
                while end < run_end and not text[end].isspace():
                    end += 1
            while start < end and text[start].isspace():
                start += 1
            while end > start and text[end - 1].isspace():
                end -= 1
            if start < end:
                fragments.append(_copy_name(result, start=start, end=end))
                # Trimming or glued_right may shorten the uncovered run. Only
                # the emitted span is covered; later eligible findings still
                # need to see its unmasked remainder.
                covered[start:end] = b"\x01" * (end - start)
            index = run_end
    return fragments


def _is_logistic_base64(result: RecognizerResult) -> bool:
    return (
        result.entity_type == "BASE64_DATA"
        and (result.recognition_metadata or {}).get("deterministic_rule_id")
        == "base64.logistic-cascade"
    )


def _add_logistic_coverage(
    text, native, candidates, requested_entities, score_threshold, declared_containers=(),
):
    """Union new Base64 ranges and subtract protected spans in one sorted sweep.

    Protected spans are retained native results plus declared containers of any
    class. Native results and types are unchanged. An intersecting container can
    need several placeholders, whose saved values still restore the original text.
    """
    if requested_entities and "BASE64_DATA" not in requested_entities:
        return native, []
    ordered = sorted(
        (r for r in candidates if _valid_span(r, len(text)) and r.score >= score_threshold),
        key=lambda r: (r.start, r.end),
    )
    unions = []
    for result in ordered:
        if unions and result.start <= unions[-1].end:
            previous = unions[-1]
            unions[-1] = _copy_name(
                previous, end=max(previous.end, result.end),
                score=max(previous.score, result.score),
            )
        else:
            unions.append(result)
    protected = sorted(
        [(r.start, r.end, detection_source(r)) for r in native]
        + [(start, end, SOURCE_STRUCTURAL) for start, end in declared_containers]
    )
    additions = []
    decisions = []
    protected_index = 0
    for result in unions:
        cursor = result.start
        while protected_index < len(protected) and protected[protected_index][1] <= cursor:
            protected_index += 1
        index = protected_index
        while index < len(protected) and protected[index][0] < result.end:
            start, end, source = protected[index]
            if start > cursor:
                additions.append(_copy_name(result, start=cursor, end=start))
            cursor = max(cursor, end)
            decisions.append(MergeDecision(
                "base64_logistic_native_preserved", source, SOURCE_STRUCTURAL
            ))
            index += 1
        if cursor < result.end:
            additions.append(_copy_name(result, start=cursor))
    # A remainder next to a native span must not start or end with a line break or spaces.
    trimmed = []
    for addition in additions:
        start, end = addition.start, addition.end
        while start < end and text[start].isspace():
            start += 1
        while end > start and text[end - 1].isspace():
            end -= 1
        if start < end:
            trimmed.append(_copy_name(addition, start=start, end=end))
    return sorted([*native, *trimmed], key=lambda r: (r.start, r.end, r.entity_type)), decisions


def _copy_name(
    result: RecognizerResult,
    *,
    entity_type: str | None = None,
    start: int | None = None,
    end: int | None = None,
    score: float | None = None,
) -> RecognizerResult:
    return RecognizerResult(
        entity_type=entity_type or result.entity_type,
        start=result.start if start is None else start,
        end=result.end if end is None else end,
        score=result.score if score is None else score,
        analysis_explanation=result.analysis_explanation,
        recognition_metadata=dict(result.recognition_metadata or {}),
    )


@lru_cache(maxsize=1)
def _russian_morphology():
    # Already installed by the Russian spaCy model; no second NER inference.
    import pymorphy3

    return pymorphy3.MorphAnalyzer()


def _is_unambiguous_imperative(word: str) -> bool:
    if not re.fullmatch(r"[А-Яа-яЁё]{2,32}", word):
        return False
    parses = _russian_morphology().parse(word)
    return bool(parses) and all("impr" in parse.tag for parse in parses)


def _refine_names(
    text: str,
    results: list[RecognizerResult],
    sentence_starts: frozenset[int],
    score_threshold: float,
) -> tuple[list[RecognizerResult], list[MergeDecision]]:
    decisions = []
    boundaries = sorted(sentence_starts)
    unique_names = {}
    retained = []
    for result in results:
        if (
            detection_source(result) != SOURCE_NER
            or result.entity_type not in _NAME_TYPES
        ):
            retained.append(result)
            continue
        key = (result.entity_type, result.start, result.end)
        previous = unique_names.get(key)
        if previous is not None:
            decisions.append(MergeDecision("exact_duplicate", SOURCE_NER, SOURCE_NER))
        if previous is None or _priority_key(result) < _priority_key(previous):
            unique_names[key] = result
    results = [*retained, *unique_names.values()]
    results, context_decisions = _refine_explicit_name_values(
        text, results, score_threshold
    )
    decisions.extend(context_decisions)
    refined = []
    for result in results:
        if (
            detection_source(result) != SOURCE_NER
            or result.entity_type not in _NAME_TYPES
        ):
            refined.append(result)
            continue
        value = text[result.start : result.end]
        if value.isdecimal():
            start = result.start
            lower_bound = max(0, start - 64)
            while start > lower_bound and text[start - 1].isdecimal():
                start -= 1
            complete_prefix = not start or not text[start - 1].isdecimal()
            if complete_prefix and _SERVICE_NUMBER_RE.search(
                text[max(0, start - 64) : start]
            ):
                decisions.append(
                    MergeDecision(
                        "name_service_number_suppressed", SOURCE_NONE, SOURCE_NER
                    )
                )
                continue
        if result.entity_type == "ORGANIZATION":
            if (
                _PERSON_FIELD_RE.search(text[max(0, result.start - 32) : result.start])
                and _NAME_WORDS_RE.fullmatch(value)
                and not any(_LEGAL_FORM_RE.fullmatch(word) for word in value.split())
            ):
                result = _copy_name(result, entity_type="PERSON")
                decisions.append(
                    MergeDecision("name_person_field", SOURCE_NER, SOURCE_NER)
                )
            elif _LEGAL_FORM_RE.fullmatch(value.split()[0] if value.split() else ""):
                # A period alone is not a boundary: require spaCy's sentence
                # start and an unambiguous dictionary imperative in the tail.
                for boundary in boundaries[
                    bisect_right(boundaries, result.start) : bisect_right(
                        boundaries, result.end - 1
                    )
                ]:
                    before = text[result.start : boundary].rstrip()
                    if not before.endswith("."):
                        continue
                    # A subword model may stop inside the word (Верн|и).
                    tail_end = result.end
                    while tail_end < len(text) and text[tail_end].isalpha():
                        tail_end += 1
                    tail = text[boundary:tail_end].strip()
                    if not _is_unambiguous_imperative(tail):
                        continue
                    end = result.start + len(before) - 1
                    while end > result.start and text[end - 1].isspace():
                        end -= 1
                    if len(text[result.start : end].split()) < 2 or any(
                        other is not result
                        and other.entity_type != "ORGANIZATION"
                        and _spans_overlap(boundary, result.end, other.start, other.end)
                        for other in results
                    ):
                        continue
                    result = _copy_name(result, end=end)
                    decisions.append(
                        MergeDecision("name_instruction_tail", SOURCE_NER, SOURCE_NER)
                    )
                    break
        refined.append(result)

    names = sorted(
        (
            r
            for r in refined
            if detection_source(r) == SOURCE_NER and r.entity_type in _NAME_TYPES
        ),
        key=lambda r: (r.start, r.end, -r.score),
    )
    removed = set()
    replacements = {}
    for left, right in zip(names, names[1:]):
        if id(left) in removed or id(right) in removed:
            continue
        if not (
            left.entity_type == "ORGANIZATION"
            and _LEGAL_FORM_RE.fullmatch(text[left.start : left.end])
            and right.entity_type == "PERSON"
            and left.end < right.start
            and right.start - left.end <= 16
            and text[left.end : right.start].isspace()
            and not any(c in "\r\n" for c in text[left.end : right.start])
            and right.end - right.start <= 128
            and _NAME_WORDS_RE.fullmatch(text[right.start : right.end])
        ):
            continue
        # Do not bridge another sensitive category or steal its protected span.
        if any(
            other is not left
            and other is not right
            and _spans_overlap(left.start, right.end, other.start, other.end)
            for other in refined
        ):
            continue
        replacements[id(left)] = _copy_name(
            left, end=right.end, score=min(left.score, right.score)
        )
        removed.add(id(right))
        decisions.append(MergeDecision("name_legal_form_join", SOURCE_NER, SOURCE_NER))
    return [
        replacements.get(id(r), r) for r in refined if id(r) not in removed
    ], decisions


def _refine_explicit_name_values(
    text: str, results: list[RecognizerResult], score_threshold: float
) -> tuple[list[RecognizerResult], list[MergeDecision]]:
    decisions = []
    if not any(
        detection_source(r) == SOURCE_NER and r.entity_type in _NAME_FRAGMENT_TYPES
        for r in results
    ):
        return results, decisions
    # Only a separate field header may be discarded, never its value or a
    # similarly named organization in prose. BIO may include the separator,
    # and a subword model may mark only pieces of the label (Ф. or .).
    headers = [(match.start("label"), match.end()) for match in _PERSON_LABEL_RE.finditer(text)]
    retained = []
    for result in results:
        if (
            detection_source(result) == SOURCE_NER
            and result.entity_type in _NAME_TYPES
            and any(start <= result.start and result.end <= end for start, end in headers)
        ):
            decisions.append(MergeDecision("name_field_label", SOURCE_NONE, SOURCE_NER))
        else:
            retained.append(result)
    ordered = sorted(retained, key=lambda r: (r.start, r.end))
    starts = [r.start for r in ordered]
    max_ends = []
    for r in ordered:
        max_ends.append(max(r.end, max_ends[-1] if max_ends else 0))
    removed = set()
    replacements = []
    previous_end = -1
    for context in sorted(name_context_evidence(text), key=lambda c: (c.start, -c.end)):
        if context.start < previous_end:
            continue
        previous_end = context.end
        overlapping = [
            r for r in ordered[
                bisect_right(max_ends, context.start) : bisect_left(starts, context.end)
            ]
            if _spans_overlap(context.start, context.end, r.start, r.end)
        ]
        fragments = [
            r for r in overlapping
            if detection_source(r) == SOURCE_NER
            and r.entity_type in _NAME_FRAGMENT_TYPES
            and r.score >= score_threshold
            and context.start <= r.start < r.end <= context.end
        ]
        if not fragments or not (
            context.entity_type == "PERSON"
            or any(r.entity_type in _NAME_TYPES for r in fragments)
        ):
            continue
        legal_prefix = [
            r for r in fragments
            if r.entity_type == "ORGANIZATION"
            and r.start == context.start and r.end == context.legal_form_end
        ]
        removable_location = [
            r for r in overlapping
            if legal_prefix and r.entity_type == "LOCATION"
            and detection_source(r) == SOURCE_STRUCTURAL
            and r.start == context.start and r.end == context.legal_form_end
        ]
        consumed = {id(r) for r in [*fragments, *removable_location]}
        if any(id(r) not in consumed for r in overlapping):
            continue
        # All letters must already be accepted NER evidence. Only whitespace
        # may be bridged, and a competing native credential always vetoes this.
        covered = bytearray(context.end - context.start)
        for fragment in fragments:
            covered[fragment.start - context.start : fragment.end - context.start] = (
                b"\x01" * (fragment.end - fragment.start)
            )
        if any(
            not covered[index] and not character.isspace()
            for index, character in enumerate(text[context.start : context.end])
        ):
            continue
        anchor = min(legal_prefix or fragments, key=_priority_key)
        merged = _copy_name(
            anchor, entity_type=context.entity_type,
            start=context.start, end=context.end,
            score=min(r.score for r in fragments),
        )
        removed.update(consumed)
        replacements.append(merged)
        if removable_location:
            decisions.append(
                MergeDecision("name_legal_prefix", SOURCE_NER, SOURCE_STRUCTURAL)
            )
        decisions.append(MergeDecision("name_explicit_value", SOURCE_NER, SOURCE_NER))
    return [r for r in retained if id(r) not in removed] + replacements, decisions


def _valid_span(result: RecognizerResult, text_length: int) -> bool:
    return (
        isinstance(result.start, int)
        and isinstance(result.end, int)
        and 0 <= result.start < result.end <= text_length
    )


def _with_detection_source(
    result: RecognizerResult,
    source: str,
) -> RecognizerResult:
    metadata = dict(result.recognition_metadata or {})
    metadata[DETECTION_SOURCE_METADATA_KEY] = source
    return RecognizerResult(
        entity_type=result.entity_type,
        start=result.start,
        end=result.end,
        score=result.score,
        analysis_explanation=result.analysis_explanation,
        recognition_metadata=metadata,
    )


def _source_rank(result: RecognizerResult) -> int:
    source = detection_source(result)
    if source == SOURCE_STRUCTURAL and (
        result.entity_type in _STRUCTURED_CONTAINER_TYPES
        or (result.recognition_metadata or {}).get("encoded_container") is True
    ):
        return 0
    if source == SOURCE_NATIVE_CREDENTIAL:
        return 1
    if source in {SOURCE_STRUCTURAL, SOURCE_CONTEXTUAL_CONTRACT}:
        return 2
    return 3


def _priority_key(result: RecognizerResult) -> tuple:
    source = detection_source(result)
    source_rank = _source_rank(result)
    specificity = _ENTITY_SPECIFICITY.get(result.entity_type, 50)
    metadata = result.recognition_metadata or {}
    if metadata.get("encoded_container") is True:
        specificity = _ENTITY_SPECIFICITY["BASE64_DATA"]
    recognizer_name = str(
        metadata.get(RecognizerResult.RECOGNIZER_NAME_KEY, "")
    )
    if source == SOURCE_CONTEXTUAL_CONTRACT:
        within_source_priority = (
            0 if recognizer_name == CONTRACT_CONTEXT_RECOGNIZER_NAME else 1,
            -float(result.score),
        )
    elif source in {SOURCE_NER, SOURCE_NATIVE_CREDENTIAL}:
        within_source_priority = (-float(result.score), specificity)
    else:
        within_source_priority = (specificity, -float(result.score))
    span_length = result.end - result.start
    span_priority = span_length if source == SOURCE_NATIVE_CREDENTIAL else -span_length
    return (
        source_rank,
        0 if metadata.get("json_string_projection") is True else 1,
        *within_source_priority,
        span_priority,
        result.start,
        result.end,
        result.entity_type,
        recognizer_name,
    )


def _conflict_reason(
    winner: RecognizerResult,
    loser: RecognizerResult,
) -> str:
    if (
        winner.start == loser.start
        and winner.end == loser.end
        and winner.entity_type == loser.entity_type
    ):
        return "exact_duplicate"
    if _source_rank(winner) != _source_rank(loser):
        return "overlap_preferred_source"
    if winner.entity_type != loser.entity_type:
        return "overlap_preferred_entity"
    if winner.score != loser.score:
        return "overlap_preferred_score"
    return "overlap_stable_order"


def _spans_overlap(
    left_start: int,
    left_end: int,
    right_start: int,
    right_end: int,
) -> bool:
    return left_start < right_end and right_start < left_end
