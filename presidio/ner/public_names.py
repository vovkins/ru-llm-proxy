"""Server-approved public names do not waive independent sensitive findings."""
import json
import re
from functools import lru_cache

from ner.public_identifiers import MAX_CONFIG_BYTES, MAX_IDENTIFIERS, MAX_IDENTIFIER_CHARS

PUBLIC_NAME_NER_TYPES = frozenset({'ORGANIZATION', 'LOGIN'})
_NAME = re.compile(r'[^\W\d][\w-]{0,127}\Z')
_CREDENTIAL_HINT = re.compile(
    r'(?i)(?<!\w)(?:password|pwd|pass|passwd|login|username|user\s+name|token|'
    r'key|apikey|apisecret|accesstoken|authtoken|clientsecret|secret|authorization|'
    r'authentication|bearer|credential\w*|confidential|'
    r'парол\w*|логин\w*|токен\w*|секрет\w*|ключ\w*|уч[её]тн\w*|конфиденциал\w*)(?!\w)'
)
_CAMEL_EDGE = re.compile(r'(?<=[a-z])(?=[A-Z])')
_STRUCTURAL_EDGES = frozenset('/\\`"\'')


def finding_fits_public_name(text, start, end, span):
    """Only name characters and at most8 adjacent path/quote delimiters per edge.

    A trailing slash in a NER finding must not turn an exact public path segment
    into a private name. Letters, spaces and wider containers remain protected.
    """
    left, right = span
    if not (start < right and left < end):
        return False
    prefix = text[start:left] if start < left else ''
    suffix = text[right:end] if end > right else ''
    return (len(prefix) <= 8 and len(suffix) <= 8
            and all(ch in _STRUCTURAL_EDGES for ch in prefix + suffix))


def parse_public_names(raw):
    """Exact word-like names, not patterns, paths, emails or client instructions."""
    if len(raw.encode('utf8')) > MAX_CONFIG_BYTES:
        raise ValueError('Public name configuration is too large')
    try:
        names = json.loads(raw)
    except (ValueError, RecursionError) as exc:
        raise ValueError('Public names must be a JSON array') from exc
    if not isinstance(names, list) or len(names) > MAX_IDENTIFIERS:
        raise ValueError('Public names must be a bounded JSON array')
    if any(not isinstance(name, str) or len(name) > MAX_IDENTIFIER_CHARS
           or not _NAME.fullmatch(name) for name in names):
        raise ValueError('Public names must be exact bounded word-like names')
    return frozenset(names)


@lru_cache(maxsize=16)
def _pattern(names):
    alternatives = '|'.join(re.escape(name) for name in sorted(names, key=lambda v: (-len(v), v)))
    return re.compile(r'(?<![\w$-])(?:' + alternatives + r')(?![\w$-])')


def public_name_spans(text, names):
    """Names may be data values; credential context and email components may not.

    Company hints intentionally do not veto this explicit disclosure policy.
    Only exact entries match: no arbitrary prefix/substring exemption for paths
    or identifiers. Nearby credential cues conservatively veto the exception;
    the merge caller additionally preserves every independent finding/container.
    """
    if not names:
        return ()
    spans = []
    for match in _pattern(frozenset(names)).finditer(text):
        start, end = match.span()
        line_start = max(text.rfind('\n', 0, start), text.rfind('\r', 0, start)) + 1
        line_end = min(p for p in (text.find('\n', end), text.find('\r', end), len(text)) if p >= 0)
        context = text[max(0, line_start - 128, start - 256):min(line_end, end + 128)]
        context = _CAMEL_EDGE.sub(' ', context).replace('_', ' ').replace('-', ' ')
        if _CREDENTIAL_HINT.search(context):
            continue
        # Also veto email-like components if an independent email detector misses
        # an incomplete address. This guard only keeps findings; it adds no mask.
        token_start = start
        while token_start > max(0, start - 320) and not text[token_start - 1].isspace() and text[token_start - 1] not in '<>"\'`':
            token_start -= 1
        token_end = end
        while token_end < min(len(text), end + 320) and not text[token_end].isspace() and text[token_end] not in '<>"\'`':
            token_end += 1
        if '@' in text[token_start:token_end]:
            continue
        spans.append((start, end))
    return tuple(spans)
