"""Source-coordinate containers for unresolved token-window evidence.

This is a fallback boundary resolver, not a detector. It never decodes input,
evaluates code, or treats an artificial Analyzer chunk as a document boundary.
"""

from bisect import bisect_right
from dataclasses import dataclass
from itertools import chain
import logging
import re
import unicodedata
from urllib.parse import urlsplit

from string_spans import StringSpans
from .initial_names import initial_person_ranges


_REFERENCES = re.compile(
    r"(?<![\w+./-])(?:[A-Za-z][A-Za-z0-9+.-]*://|//|/|"
    r"[A-Za-z0-9._~-]+/)[^\s\"'`<>]+"
)
_LINE_BREAK = r"(?:\r\n|\r(?!\n)|(?<!\r)\n|\u2028|\u2029)"
_LINE_END = re.compile(_LINE_BREAK)
_BLANK_LINE = re.compile(rf"{_LINE_BREAK}[ \t]*{_LINE_BREAK}")
_FENCE = re.compile(r"(?:\A|(?<=[\r\n\u2028\u2029]))[ ]{0,3}(`{3,}|~{3,})([^\r\n\u2028\u2029]*)")
_BAD_PERCENT = re.compile(r"%(?![0-9A-Fa-f]{2})")
_KEY_SEPARATOR = re.compile(r"\s*:")
_NUMBERED_SOURCE_LINE = re.compile(r"[ \t]*([1-9][0-9]{0,8}):(?:[ \t]+(.*))?\Z")
MAX_NUMBERED_PARAGRAPH_CHARS = 2048
_TICKS = re.compile(r"`+")
_INLINE_CODE = re.compile(r"(?<![\\`])(`+)(?!`)[^`\r\n\u2028\u2029]+\1(?!`)")
_GROUP_MARKS = re.compile(r"""[(){}\[\]"'\u00ab\u00bb\u201c\u201d\u2018\u2019]""")
_CONTEXT_PAIRS = {"{": "}", "[": "]", "\u00ab": "\u00bb", "\u201c": "\u201d", "\u2018": "\u2019"}
_PROSE_INTRO = re.compile(r"(?:[^\W\d_]+[ \t]+[^\W\d_]+|`)[ \t]+\Z")
_LIST_MARKER = re.compile(r"( {0,3})([-+*]|[0-9]{1,9}[.)])[ \t]+")
_LINE_NUMBER = re.compile(r"([0-9]{1,9}):[ \t]+")
_CONTINUATION_LIST = re.compile(rf"{_LINE_BREAK}(?:{_LINE_NUMBER.pattern})?{_LIST_MARKER.pattern}")
_BOLD_HEADING = re.compile(r"\*\*([^*]+)\*\*[ \t]+")
_MAX_PROSE_CONTAINER_CHARS = 4096
_INSERT_MARGIN = 128
_INSERT_LABEL = re.compile(r"[ \t]*([1-9][0-9]{0,8}):[ \t]+[^\W\d_]+(?:[ \t]+[^\W\d_]+){0,7}")
_INSERT_SEPARATOR = re.compile(r"(?:[ \t]*:|[ \t]+-)[ \t]+\Z")
_INSERT_TAIL = re.compile(r"[ \t]*[.!?]?[ \t]*\Z")
_CODE_CALL = re.compile(r"[\w.)]\(")
logger = logging.getLogger(__name__)


class _Ranges:
    def __init__(self, ranges):
        self.ranges = sorted(set(ranges))
        self.starts = [start for start, _ in self.ranges]
        self.prefix_ends = []
        for _, end in self.ranges:
            self.prefix_ends.append(max(end, self.prefix_ends[-1] if self.prefix_ends else 0))

    def containing(self, start, end):
        index = bisect_right(self.starts, start) - 1
        best = None
        while index >= 0 and self.prefix_ends[index] >= end:
            left, right = self.ranges[index]
            if end <= right and (best is None or right - left < best[1] - best[0]):
                best = left, right
            index -= 1
        return best

    def intersects(self, start, end):
        index = bisect_right(self.starts, end - 1) - 1
        return index >= 0 and self.prefix_ends[index] > start

    def overlapping(self, start, end):
        first = bisect_right(self.prefix_ends, start)
        last = bisect_right(self.starts, end - 1)
        for index in range(first, last):
            left, right = self.ranges[index]
            if start < right:
                yield left, right


def _references(text):
    components, owners = [], []
    for match in _REFERENCES.finditer(text):
        raw = match.group()
        # Parentheses can be URL data. Strip only one visible outer wrapper,
        # not every unmatched suffix, and never rewrite an unwrapped URL.
        preceding = text[match.start() - 1:match.start()] if match.start() else ""
        closing = {"(": ")", "[": "]"}.get(preceding)
        if closing and raw.endswith(closing) and raw.count(closing) > raw.count(preceding):
            raw = raw[:-1]
        if not raw or "\\" in raw or _BAD_PERCENT.search(raw):
            continue
        try:
            parsed = urlsplit(raw)
            if parsed.netloc:
                parsed.port
        except ValueError:
            continue
        # Derive offsets from raw delimiters, not a reconstructed/decoded URL.
        before_fragment, hash_mark, fragment = raw.partition("#")
        before_query, query_mark, query = before_fragment.partition("?")
        path_start = len(parsed.scheme) + 1 if parsed.scheme else 0
        if before_query[path_start:path_start + 2] == "//":
            authority_start = path_start + 2
            authority_end = before_query.find("/", authority_start)
            if authority_end < 0:
                authority_end = len(before_query)
            authority = before_query[authority_start:authority_end]
            if authority != parsed.netloc:
                continue
            components.append((match.start() + authority_start, match.start() + authority_end))
            userinfo, at, _ = authority.rpartition("@")
            if at:
                login, colon, password = userinfo.partition(":")
                if login:
                    components.append((match.start() + authority_start,
                                       match.start() + authority_start + len(login)))
                if colon and password:
                    components.append((match.start() + authority_start + len(login) + 1,
                                       match.start() + authority_start + len(userinfo)))
            path_start = authority_end
        if before_query[path_start:] != parsed.path:
            continue
        position = match.start() + path_start
        for segment in parsed.path.split("/"):
            if segment:
                components.append((position, position + len(segment)))
            position += len(segment) + 1
        if query_mark:
            position = match.start() + len(before_query) + 1
            for pair in query.split("&"):
                key, equals, value = pair.partition("=")
                if equals and value:
                    components.append((position + len(key) + 1, position + len(pair)))
                position += len(pair) + 1
            if query:
                components.append((match.start() + len(before_query) + 1,
                                   match.start() + len(before_fragment)))
        if hash_mark and fragment:
            components.append((match.start() + len(before_fragment) + 1,
                               match.start() + len(raw)))
        owners.append((match.start(), match.start() + len(raw)))
        left, right = match.start(), match.start() + len(raw)
        # A finding on link markup cannot be repaired by dropping the delimiter.
        if left and right < len(text) and (text[left - 1], text[right]) in (("(", ")"), ("<", ">")):
            owners.append((left - 1, right + 1))
    return components, owners


def _paragraphs(text, start, end):
    boundaries = []
    for separator in _BLANK_LINE.finditer(text, start, end):
        boundaries.append((start, separator.start()))
        start = separator.end()
    boundaries.append((start, end))
    for start, end in boundaries:
        while start < end and text[start].isspace():
            start += 1
        while end > start and text[end - 1].isspace():
            end -= 1
        if start < end:
            yield start, end


def _plain_fenced_paragraph(text):
    return all(char.isalnum() or char.isspace() or char in "_.,:!?-\u2013\u2014\u2026"
               or unicodedata.category(char).startswith("M") for char in text)


def _inline_syntax(text, fences, strings):
    closed = _Ranges(m.span() for m in _INLINE_CODE.finditer(text)
                     if not fences.intersects(*m.span()) and not strings.intersects(*m.span()))
    unknown_start = next((m.start() for m in _TICKS.finditer(text)
                          if not any(index.containing(*m.span()) for index in (fences, strings, closed))),
                         len(text))
    # Do not infer new containers after an unmatched or unsupported code delimiter.
    return closed, unknown_start


def _plain_prose(text):
    depth = 0
    for char in text:
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth < 0:
                return False
        elif not (char.isalnum() or char.isspace() or char in ".,:;!?%+-\u2013\u2014\u2026"
                  or unicodedata.category(char).startswith("M")):
            return False
    return depth == 0


def _prose_parts(text, start, end, inline, references):
    if (end - start > _MAX_PROSE_CONTAINER_CHARS or _BLANK_LINE.search(text, start, end)
            or _CONTINUATION_LIST.search(text, start, end)):
        return
    parts = []
    for left, right in inline.overlapping(start, end):
        if left < start or right > end:
            return  # Never split a code span at the edge of a container.
        parts.append((start, left))
        start = right
    parts.append((start, end))
    bounded = []
    for left, right in parts:
        value = text[left:right]
        trimmed = value.strip()
        if not _plain_prose(trimmed) or references.intersects(left, right):
            return
        if any(char.isalnum() for char in trimmed):
            left += len(value) - len(value.lstrip())
            bounded.append((left, left + len(trimmed)))
    yield from bounded


def _parenthesis_pairs(text, opaque, unknown_start):
    depth, opening = 0, 0
    context = []
    inside_markup = False
    unknown_string = False
    for match in _GROUP_MARKS.finditer(text, 0, unknown_start):
        position = match.start()
        if any(index.intersects(*match.span()) for index in opaque):
            continue
        marker = match[0]
        if marker in _CONTEXT_PAIRS:
            context.append(_CONTEXT_PAIRS[marker])
        elif context and marker == context[-1]:
            context.pop()
        elif marker in "\"'":
            # A quote not covered by the string index may be unclosed. An
            # apostrophe inside a word is not a string delimiter.
            unknown_string |= not (marker == "'" and position > 0 and position + 1 < len(text)
                                   and text[position - 1].isalnum() and text[position + 1].isalnum())
        elif marker == "(":
            if depth == 0:
                opening = position
                inside_markup = bool(context) or unknown_string
            depth += 1
        elif marker == ")" and depth:
            depth -= 1
            if depth == 0:
                yield opening, position, inside_markup


def _parenthetical_values(text, pairs, inline, fences, strings, references):
    for opening, closing, _ in pairs:
        if not _PROSE_INTRO.search(text, max(0, opening - 64), opening):
            continue
        if fences.intersects(opening, closing + 1) or strings.intersects(opening, closing + 1):
            continue
        yield from _prose_parts(text, opening + 1, closing, inline, references)


def _insert_intro(text, opening, inline):
    floor = max(0, opening - _INSERT_MARGIN)
    line_break = max(text.rfind(char, floor, opening) for char in "\r\n\u2028\u2029")
    if line_break < 0 and floor:
        return None
    label = _INSERT_LABEL.match(text, line_break + 1, opening)
    if label is None:
        return None
    separator = _INSERT_SEPARATOR.search(text, label.end(), opening)
    if separator is None:
        return None
    between = text[label.end():separator.start()]
    if between.strip():
        left = label.end() + len(between) - len(between.lstrip(" \t"))
        right = label.end() + len(between.rstrip(" \t"))
        if left == label.end() or inline.containing(left, right) != (left, right):
            return None
    return int(label[1])


def _insert_ends_record(text, closing, number):
    # A newline after ')' alone does not exclude a continuation of the value.
    tail = closing + 1
    ceiling = min(len(text), tail + _INSERT_MARGIN)
    newline = _LINE_END.search(text, tail, ceiling)
    end = newline.start() if newline else ceiling
    if not _INSERT_TAIL.fullmatch(text, tail, end):
        return False
    if newline is None:
        return ceiling == len(text)
    if newline.end() == len(text):
        return True
    following = _LINE_END.search(text, newline.end(), min(len(text), newline.end() + _INSERT_MARGIN))
    if following is None:
        return False
    next_record = _NUMBERED_SOURCE_LINE.fullmatch(text, newline.end(), following.start())
    return (not text[newline.end():following.start()].strip()
            or next_record is not None and int(next_record[1]) == number + 1
            and not (next_record[2] or "").strip())


def _numbered_parenthetical_inserts(text, pairs, inline, fences, strings, references):
    """Whole prose inserts anchored to consecutive source records."""
    for opening, closing, inside_markup in pairs:
        start = opening + 1
        if (inside_markup or not 0 < closing - start <= _MAX_PROSE_CONTAINER_CHARS
                or fences.intersects(opening, closing + 1)
                or strings.intersects(opening, closing + 1)
                or _BLANK_LINE.search(text, start, closing)):
            continue
        number = _insert_intro(text, opening, inline)
        if number is None:
            continue
        for newline in _LINE_END.finditer(text, start, closing):
            continuation = _LINE_NUMBER.match(text, newline.end(), closing)
            if (continuation is None or int(continuation[1]) != number + 1
                    or _LIST_MARKER.match(text, continuation.end(), closing)):
                break
            number += 1
        else:
            if (_insert_ends_record(text, closing, number)
                    and _numbered_prose(text, start, closing, inline, references, reject_calls=True)):
                yield start, closing


def _list_items(text, content_end):
    def line(start, end):
        number = _LINE_NUMBER.match(text, start, end)
        marker = _LIST_MARKER.match(text, number.end() if number else start, end)
        if marker is None:
            return None
        return marker.end(), end, marker[1], marker[2][-1], int(number[1]) if number else None

    def peers(first, second):
        if first is None or second is None or first[2:4] != second[2:4]:
            return False
        return (first[4] is None and second[4] is None or
                first[4] is not None and second[4] == first[4] + 1)

    def lines():
        start = 0
        for separator in _LINE_END.finditer(text):
            yield line(start, separator.start())
            start = separator.end()
        yield line(start, len(text))

    previous = None
    items = lines()
    current = next(items)
    for following in chain(items, (None,)):
        if current and (peers(current, following) or current[1] >= content_end and peers(previous, current)):
            # A peer marker or the original document end closes the item. A
            # mere newline may be a continuation, even in a numbered excerpt.
            yield current[:2]
        previous, current = current, following


def _numbered_prose(text, start, end, inline, references, *, reject_calls=False):
    # Whole closed inline-code spans may stay inside a masked paragraph. They
    # cannot determine its ends or lend their punctuation to the prose grammar.
    stack = []
    cursor = start
    pairs = {"(": ")", "\u00ab": "\u00bb"}
    for left, right in chain(inline.overlapping(start, end), ((end, end),)):
        if (left < cursor or right > end or (cursor < left and references.intersects(cursor, left))
                or reject_calls and _CODE_CALL.search(text, cursor, left)):
            return False
        for char in text[cursor:left]:
            if char in pairs:
                stack.append(pairs[char])
            elif char in pairs.values():
                if not stack or stack.pop() != char:
                    return False
            elif not (char.isalnum() or char.isspace() or char in "_.:,;!?%+-\u2013\u2014\u2026"
                      or unicodedata.category(char).startswith("M")):
                return False
        cursor = right
    return not stack


def _numbered_source_paragraphs(text, inline, fences, strings, references, unknown_start):
    """Consecutive plain source runs from line 1, bounded by numbered blank records."""
    previous = None
    preceding_blank = False
    usable = False
    left = right = None
    start = 0
    for separator in chain(_LINE_END.finditer(text), (None,)):
        end = separator.start() if separator is not None else len(text)
        if end > unknown_start:
            break
        match = _NUMBERED_SOURCE_LINE.fullmatch(text, start, end)
        if match is None:
            previous = None
        else:
            number = int(match[1])
            if previous is None or number != previous + 1:
                usable = number == 1
                preceding_blank = False
                left = right = None
            previous = number
            if usable and not (match[2] or "").strip():
                if left is not None:
                    usable = (
                        right - left <= MAX_NUMBERED_PARAGRAPH_CHARS
                        and not fences.intersects(left, right)
                        and not strings.intersects(left, right)
                        and _numbered_prose(text, left, right, inline, references)
                    )
                    if usable and preceding_blank and separator is not None:
                        yield left, right
                preceding_blank = True
                left = right = None
            elif usable:
                body_start = match.start(2)
                if _LIST_MARKER.match(text, body_start, end):
                    usable = False  # Do not combine distinct list values.
                else:
                    left = body_start if left is None else left
                    right = end
                    if right - left > MAX_NUMBERED_PARAGRAPH_CHARS:
                        usable = False
        if separator is not None:
            start = separator.end()


class BoundaryValues:
    """Lazy, request-local index of complete original-source containers."""

    def __init__(self, text, *, confirmed_value=False):
        self.text = text
        self.confirmed_value = confirmed_value
        self.content_start = len(text) - len(text.lstrip())
        self.content_end = len(text.rstrip())
        self._prepared = False
        self._inline = None
        self._pairs = None
        self._structured_prose = None
        self._numbered_paragraphs = None
        self._initial_persons = None
        self._parenthetical_inserts = None

    def _prepare(self):
        if self._prepared:
            return
        strings = StringSpans(self.text)
        self.literals = _Ranges(
            (span.content_start, span.content_end) for span in strings.spans
            if _KEY_SEPARATOR.match(self.text, span.end) is None
        )
        self.string_owners = _Ranges((span.start, span.end) for span in strings.spans)
        components, owners = _references(self.text)
        self.components, self.references = _Ranges(components), _Ranges(owners)
        fences, bodies, opening = [], [], None
        for match in _FENCE.finditer(self.text):
            marker = match.group(1)
            if opening is None:
                line_end = _LINE_END.match(self.text, match.end())
                opening = (match.start(), marker, line_end.end() if line_end else match.end(),
                           marker[0] != "`" or "`" not in match.group(2))
            elif (marker[0] == opening[1][0] and len(marker) >= len(opening[1])
                  and not match.group(2).strip(" \t")):
                fences.append((opening[0], match.end()))
                if opening[3]:
                    bodies.append((opening[2], match.start()))
                opening = None
        if opening is not None:
            fences.append((opening[0], len(self.text)))
        self.fences = _Ranges(fences)
        self.paragraphs = _Ranges(_paragraphs(self.text, 0, len(self.text)))
        self.fenced_paragraphs = _Ranges(
            bounds for start, end in bodies for bounds in _paragraphs(self.text, start, end)
        )
        self._prepared = True

    def _not_document(self, bounds):
        return bounds is not None and (
            bounds[0] > self.content_start or bounds[1] < self.content_end
        )

    def _markup(self):
        if self._inline is None:
            self._inline = _inline_syntax(self.text, self.fences, self.string_owners)
        return self._inline

    def is_plain_text_span(self, start, end):
        if self.confirmed_value or not 0 <= start < end <= len(self.text):
            return False
        self._prepare()
        inline, unknown_start = self._markup()
        return end <= unknown_start and not any(index.intersects(start, end) for index in (
            self.string_owners, self.references, self.fences, inline,
        ))

    def _parentheses(self):
        if self._pairs is None:
            inline, unknown_start = self._markup()
            opaque = inline, self.fences, self.string_owners, self.references
            self._pairs = tuple(_parenthesis_pairs(self.text, opaque, unknown_start))
        return self._pairs

    def _resolve_structured_prose(self, start, end):
        if self._structured_prose is None:
            inline, unknown_start = self._markup()
            ranges = list(_parenthetical_values(self.text, self._parentheses(), inline, self.fences,
                                                self.string_owners, self.references))
            for left, right in _list_items(self.text, self.content_end):
                if (right > unknown_start or self.fences.intersects(left, right)
                        or self.string_owners.intersects(left, right)):
                    continue
                heading = _BOLD_HEADING.match(self.text, left, right)
                if heading and _plain_prose(heading[1]):
                    left = heading.end()
                ranges.extend(_prose_parts(self.text, left, right, inline, self.references))
            self._structured_prose = _Ranges(ranges)
        bounds = self._structured_prose.containing(start, end)
        return bounds if self._not_document(bounds) else None

    def resolve(self, start, end, *, entity_type=None):
        bounds = self._resolve_existing(start, end)
        if bounds is not None or not 0 <= start < end <= len(self.text):
            return bounds

        inline, unknown_start = self._markup()
        if entity_type == "PERSON":
            if self._initial_persons is None:
                self._initial_persons = _Ranges(
                    (left, right) for left, right in initial_person_ranges(self.text)
                    if right <= unknown_start and not any(index.intersects(left, right) for index in (
                        self.string_owners, self.fences, inline, self.references,
                    ))
                )
            person = self._initial_persons.containing(start, end)
            if self._not_document(person):
                logger.debug("presidio_ner_boundary_resolved kind=initials")
                return person

        if self._numbered_paragraphs is None:
            self._numbered_paragraphs = _Ranges(
                _numbered_source_paragraphs(self.text, inline, self.fences, self.string_owners,
                                            self.references, unknown_start)
            )
        paragraph = self._numbered_paragraphs.containing(start, end)
        if self._not_document(paragraph):
            logger.debug("presidio_ner_boundary_resolved kind=numbered_paragraph")
            return paragraph
        if self._parenthetical_inserts is None:
            self._parenthetical_inserts = _Ranges(
                _numbered_parenthetical_inserts(self.text, self._parentheses(), inline, self.fences,
                                                self.string_owners, self.references)
            )
        insert = self._parenthetical_inserts.containing(start, end)
        if self._not_document(insert):
            logger.debug("presidio_ner_boundary_resolved kind=numbered_parenthetical")
            return insert
        return None

    def _resolve_existing(self, start, end):
        if not 0 <= start < end <= len(self.text):
            return None
        self._prepare()
        candidates = [index.containing(start, end)
                      for index in (self.components, self.references, self.literals)]
        candidates = [bounds for bounds in candidates if self._not_document(bounds)]
        if candidates:
            return min(candidates, key=lambda bounds: bounds[1] - bounds[0])
        bounds = self.paragraphs.containing(start, end)
        if bounds is not None and self.fences.intersects(*bounds):
            # Only closed bodies have paragraphs independent of their delimiters.
            bounds = self.fenced_paragraphs.containing(start, end)
            if bounds is not None and not _plain_fenced_paragraph(self.text[slice(*bounds)]):
                return None
        fallback = (0, len(self.text)) if self.confirmed_value else None
        if not self._not_document(bounds):
            return fallback or self._resolve_structured_prose(start, end)
        left, right = bounds
        # A prose paragraph must not swallow syntax or several string values.
        if (self.string_owners.intersects(left, right)
                or any(c in self.text[left:right] for c in "{}[]")):
            return fallback or self._resolve_structured_prose(start, end)
        return bounds


@dataclass
class PersonWindowBudget:
    """One additional inference shared by all chunks of an Analyzer request."""

    used: bool = False


@dataclass(frozen=True)
class BoundaryContext:
    values: BoundaryValues
    offset: int = 0
    person_window: PersonWindowBudget | None = None
