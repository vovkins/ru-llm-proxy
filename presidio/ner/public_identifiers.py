"""Trusted, opt-in exceptions for explicit technical NER references."""
import json
import re
from functools import lru_cache

MAX_CONFIG_BYTES = 64 * 1024
MAX_IDENTIFIERS = 256
MAX_IDENTIFIER_CHARS = 128
_IDENTIFIER = re.compile(r'[A-Za-z_][A-Za-z0-9_]{0,127}\Z')
_SENSITIVE_HINT = re.compile(
    r'(?i)(?<!\w)(?:password|pwd|pass|login|username|user_name|token|auth_token|'
    r'api_key|secret|secret_key|authorization|company|organization|organisation|'
    r'brand|org|парол\w*|логин\w*|токен\w*|секрет\w*|организац\w*|компани\w*|'
    r'контрагент\w*|ооо|пао|оао|зао|ао)(?!\w)'
)
_ASSIGNMENT_END = re.compile(
    r'(?:^|[;,({\[])[ \t\u00a0]*'
    r'(?:(?:const|let|var)[ \t\u00a0]+)?'
    r'["\']?[^\W\d][\w.$-]{0,127}["\']?[ \t\u00a0]*[:=][ \t\u00a0]*\Z'
)
_CONTEXTUAL_TECH_TERMS = frozenset({'PWA'})
_TECH_ROLE_LEFT = re.compile(
    r'(?i)(?<!\w)(?:аналитика|аналитики|аналитику|приложение|приложения|приложении|'
    r'веб[- ]приложение|веб[- ]приложения|технология|технологии)[ \t\u00a0]{1,16}\Z'
)
_WEB_APP_DEFINITION = re.compile(
    r'(?i)(?<!\w)progressive[ \t]{1,16}web[ \t]{1,16}(?:apps?|applications?)[ \t]{0,8}(?:\([ \t]{0,8})?\Z'
)
_TECH_ROLE_RIGHT = re.compile(
    r'(?i)(?:[-\u2010\u2011\u2013][ \t\u00a0]{0,8}|[ \t\u00a0]{1,16})'
    r'(?:приложение|приложения|приложению|приложении|приложений|приложением|'
    r'приложениями|приложениях|apps?|applications?)(?![\w$-])'
)


def parse_public_identifiers(raw):
    """Accept server-owned JSON, never request-supplied declarations."""
    if len(raw.encode('utf8')) > MAX_CONFIG_BYTES:
        raise ValueError('Public identifier configuration is too large')
    try:
        values = json.loads(raw)
    except (ValueError, RecursionError) as exc:
        raise ValueError('Public identifiers must be a JSON array') from exc
    if not isinstance(values, list) or len(values) > MAX_IDENTIFIERS:
        raise ValueError('Public identifiers must be a bounded JSON array')
    if any(not isinstance(v, str) or not _IDENTIFIER.fullmatch(v) for v in values):
        raise ValueError('Public identifiers must be exact bounded ASCII identifiers')
    return frozenset(values)


@lru_cache(maxsize=16)
def _pattern(identifiers):
    regular = identifiers - _CONTEXTUAL_TECH_TERMS
    contextual = identifiers & _CONTEXTUAL_TECH_TERMS
    alternatives = []
    if regular:
        values = '|'.join(re.escape(v) for v in sorted(regular, key=lambda v: (-len(v), v)))
        alternatives.append(r'(?:' + values + r')(?![\w$-])')
    if contextual:
        values = '|'.join(re.escape(v) for v in sorted(contextual))
        alternatives.append(r'(?:' + values + r')(?![\w$])')
    return re.compile(r'(?<![\w$-])(?:' + '|'.join(alternatives) + r')')


def allows_whole_technical_reference(text, start, end):
    """Only a registered technology term with an explicit same-line role."""
    if text[start:end] not in _CONTEXTUAL_TECH_TERMS:
        return False
    left, right = start, end
    if start > 0 and end < len(text) and text[start - 1] in '`"\'' and text[end] == text[start - 1]:
        left, right = start - 1, end + 1
    line_start = max(text.rfind('\n', 0, left), text.rfind('\r', 0, left)) + 1
    prefix_start = max(line_start, left - 96)
    following_role = _TECH_ROLE_RIGHT.match(text, right)
    if text[right:right + 1] in ('-', '\u2010', '\u2011', '\u2013'):
        # PWA-org and other larger hyphenated names are never a technology cue.
        return following_role is not None
    return bool(following_role or _TECH_ROLE_LEFT.search(text, prefix_start, left)
                or _WEB_APP_DEFINITION.search(text, prefix_start, left))


def public_identifier_spans(text, strings, identifiers):
    """Return exact technical references, not arbitrary structured data values.

    Scope is intentionally conservative: JSON-like documents, assignment-owned
    literals and nearby credential/company hints remain protected. The caller
    must also preserve non-technical whole findings, containers and independent types.
    """
    if not identifiers or text.lstrip().startswith(('{', '[', '"', "'")):
        return ()
    spans = []
    for match in _pattern(frozenset(identifiers)).finditer(text):
        start, end = match.span()
        line_start = max(text.rfind('\n', 0, start), text.rfind('\r', 0, start)) + 1
        line_end = min((p for p in (text.find('\n', end), text.find('\r', end), len(text)) if p >= 0))
        context = text[max(line_start, start - 96):min(line_end, end + 96)]
        if match.group() in _CONTEXTUAL_TECH_TERMS:
            if not allows_whole_technical_reference(text, start, end):
                continue
            # Preserve wrapped sensitive fields, e.g. Company:\nPWA application.
            context = text[max(0, line_start - 96):line_start] + context
        if _SENSITIVE_HINT.search(context):
            continue
        literal = strings.at(start)
        prefix_end = literal.start if literal is not None else start
        if literal is None and start > line_start and text[start - 1] == '`':
            prefix_end = start - 1
        prefix = text[max(line_start, prefix_end - 96):prefix_end]
        if literal is not None and prefix.rstrip().endswith(('[', '{', ',', '(', ':', '=')):
            continue
        if _ASSIGNMENT_END.search(prefix):
            continue
        spans.append((start, end))
    return tuple(spans)
