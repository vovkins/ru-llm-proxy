"""Optional right bound at an explicit numbered documentation-reference record.

This is not a rule that names cannot contain colons. Unknown source forms retain
the previous enclosing container. Returned offsets always refer to original text.
"""
import re

_START = r"(?<![^\r\n\u2028\u2029])"
_FIRST = re.compile(_START + r"[ \t]*1:(?:[ \t]|(?=[\r\n\u2028\u2029]|$))")
_LABEL = re.compile(
    _START + r"[ \t]*[1-9][0-9]{0,8}:[ \t]+"
    r"(?P<label>[A-Z][A-Za-z0-9_-]{1,31}):[ \t]+"
    r"(?P<value>[^\r\n\u2028\u2029]+)"
)
_ITEM = re.compile(r"(?<!`)`(?P<path>[^`\r\n]+)`(?!`)")
_PATH = re.compile(r"(?:[a-z][a-z-]*/)*[a-z*][a-z.*-]*\.md\Z")
_SEPARATORS = re.compile(r"[\s,;().]*(?:(?:и|или|правила|and|or|rules)[\s,;().]*)*\Z", re.I)
_QUOTES = frozenset("«»‹›“”„‟‘’‚‛")
_CUE = re.compile(
    r"(?<!\w)(?:ООО|ПАО|ОАО|ЗАО|АО|ИП)(?!\w)|"
    r"\b(?:password|passwd|login|token|auth[_ -]?token|secret[_ -]?key|api[_ -]?key|"
    r"authorization|host|server|endpoint|url|uri|database|пароль|токен|секрет|логин)\s*[:=]", re.I,
)
_DENIED_LABELS = frozenset({"password", "passwd", "login", "token", "authtoken", "secret", "secretkey",
                            "apikey", "authorization", "host", "server", "endpoint", "url", "uri", "database"})


def _balanced_ticks(prefix):
    opening = None
    for match in re.finditer(r"`+", prefix):
        length = match.end() - match.start()
        if opening is None:
            opening = length
        elif opening == length:
            opening = None
    return opening is None


def _reference_list(value):
    items = list(_ITEM.finditer(value))
    if len(items) < 2:
        return False
    for item in items:
        path = item.group("path")
        if (len(path) > 128 or ".." in path or not _PATH.fullmatch(path)
                or any(word in path for word in ("secret", "password", "credential", "token"))):
            return False
    remainder = _ITEM.sub("", value)
    return _SEPARATORS.fullmatch(remainder) is not None


def following_reference_label(text, start, end, paragraph):
    """Return a colon before a later reference-list value, or abstain.

    Caller has already proved the complete numbered paragraph and its grammar.
    The label itself remains inside the mask, including possible continuation
    of the positive finding. Only a subsequent value is separated.
    """
    floor, ceiling = paragraph
    if not 0 <= floor <= start < end <= ceiling <= len(text):
        return None
    if _CUE.search(text[floor:ceiling]):
        return None
    # An exact source run from line 1 is already required by the caller. Include
    # its prefix in the quote/code check, rather than guessing inherited state.
    first = None
    for match in _FIRST.finditer(text, 0, floor):
        first = match.start()
    if first is None:
        return None
    for record in _LABEL.finditer(text, end, ceiling):
        colon = record.end("label")
        if not end <= colon < ceiling:
            continue
        label = record.group("label").lower().replace("_", "").replace("-", "")
        if label in _DENIED_LABELS or not _reference_list(record.group("value")):
            continue
        prefix = text[first:colon]
        if any(char in _QUOTES for char in prefix) or not _balanced_ticks(prefix):
            continue
        return colon
    return None


def subword_reference_scope_allowed(text, start, end, paragraph):
    """Only narrow across a completely validated documentation reference list.

    Plain prose has no reference restriction. If shortening would expose .md
    literals, all such literals must belong to the same validated successor
    record. Unknown syntax retains the old enclosing container.
    """
    ceiling = paragraph[1]
    items = [m for m in _ITEM.finditer(text, end, ceiling)
             if m.group("path").lower().endswith(".md")]
    if not items:
        return True
    colon = following_reference_label(text, start, end, paragraph)
    if colon is None:
        return False
    for record in _LABEL.finditer(text, end, ceiling):
        if record.end("label") == colon:
            return all(record.start("value") <= m.start() < m.end() <= record.end("value")
                       for m in items)
    return False
