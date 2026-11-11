"""Conservative complete initial/surname units, not a general PERSON detector.

Used only for an unresolved positive PERSON cluster after stock bounds fail.
No negative veto, model inference, text rewriting or credential interpretation.
"""
import ast
from bisect import bisect_left, bisect_right
from itertools import chain
import re

_MARK = r"[\u0300-\u036f]*"
_CAP = rf"[А-ЯЁ]{_MARK}"
_LOWER = rf"[а-яё]{_MARK}"
_WORD = rf"{_CAP}(?:{_LOWER}){{1,63}}"
_SURNAME = rf"{_WORD}(?:[-\u2010\u2011]{_WORD}){{0,2}}"
_SPACE = r"[ \t\u00a0\u2009]{0,16}"
_WS = rf"{_SPACE}(?:(?:\r\n|[\r\n\u2028\u2029]){_SPACE})?"
_INITIAL = rf"{_CAP}\."
_INITIALS = rf"{_INITIAL}(?:{_WS}{_INITIAL})?"
_NAME = re.compile(
    rf"(?<![\w\u0300-\u036f\-\u2010\u2011])"
    rf"(?:{_INITIALS}{_WS}{_SURNAME}|{_SURNAME}[ \t\u00a0]{{1,16}}{_INITIALS})"
    rf"(?![\w\u0300-\u036f\-\u2010\u2011])"
)
_ORG = re.compile(r"(?<!\w)(?:ООО|ПАО|ОАО|ЗАО|АО|ИП)(?!\w)")
_CREDENTIAL = re.compile(
    r"(?:password|passwd|token|secret(?:_key)?|api_key|пароль|секрет|токен)"
    r"\s*[:=]\s*\(?\s*$", re.I,
)
_PREVIOUS_INITIAL = re.compile(rf"{_INITIAL}{_WS}$")
_CODE_START = re.compile(r"(?:const|let|var|function|return|def|class|import|from|await|yield)\b")
_LINE_END = re.compile(r"\r\n|\r|\n|\u2028|\u2029")
_SOURCE_SYNTAX = re.compile(r"""[{}\[\]"'\\=<>]""")


def _inline_ranges(text):
    """Linear scan; an unclosed inline span stays unavailable for recovery."""
    start = 0
    for separator in chain(_LINE_END.finditer(text), (None,)):
        end = separator.start() if separator is not None else len(text)
        opening = None
        for marker in re.finditer(r"`+", text[start:end]):
            length = marker.end() - marker.start()
            if opening is None:
                opening = (start + marker.start(), length)
            elif length == opening[1]:
                yield opening[0], start + marker.end()
                opening = None
        if opening is not None:
            yield opening[0], end
        if separator is not None:
            start = separator.end()


def _python(text):
    try:
        ast.parse(text)
    except (SyntaxError, ValueError, TypeError, RecursionError):
        return False
    return True


def initial_person_ranges(text):
    """Yield bounded canonical units; ambiguous neighbours keep the refusal."""
    if "\x00" in text:
        return
    inline = list(_inline_ranges(text))
    inline_starts = [left for left, _ in inline]
    endings = list(_LINE_END.finditer(text))
    line_starts = [0] + [m.end() for m in endings]
    line_ends = [m.start() for m in endings] + [len(text)]
    for ordinal, match in enumerate(_NAME.finditer(text)):
        if ordinal >= 256:
            return
        a, b = match.span()
        code_index = bisect_left(inline_starts, b) - 1
        if b - a > 128 or (code_index >= 0 and inline[code_index][1] > a):
            continue
        prefix, suffix = text[max(0, a - 128):a], text[b:b + 128]
        before = prefix.rstrip()
        if not before and a > len(prefix):
            continue
        # Require a real punctuation/start boundary, not a suffix of a name.
        if before and before[-1] not in "(:;,!?." and not prefix.endswith(("\n", "\r", "\u2028", "\u2029")):
            continue
        if a and text[a - 1] == ".":
            continue
        if _PREVIOUS_INITIAL.search(prefix) or _CREDENTIAL.search(prefix):
            continue
        # Name-looking text next to a legal form is not an independent PERSON unit.
        clause = re.split(r"[\n\r:;!?]", prefix)[-1]
        if _ORG.search(clause[-100:]):
            continue
        after = suffix.lstrip()
        if not after and b + len(suffix) < len(text):
            continue
        if after and after[0] not in "):;,!?.":
            continue
        # Comma may precede another part of an inverted name. Abstain on ambiguity.
        if after.startswith(",") and re.match(rf"\s*{_CAP}(?:\.|{_LOWER})", after[1:]):
            continue
        first_line = bisect_right(line_starts, a) - 1
        last_line = bisect_left(line_ends, b)
        line_start, line_end = line_starts[first_line], line_ends[last_line]
        if last_line - first_line > 1 or line_end - line_start > 1024:
            continue
        line = re.sub(r"^\s*[0-9]+:\s*", "", text[line_start:line_end]).strip()
        if _SOURCE_SYNTAX.search(line) or _CODE_START.match(line) or _python(line):
            continue
        yield a, b
