"""Source-coordinate containers for unresolved token-window evidence.

This is a fallback boundary resolver, not a detector. It never decodes input,
evaluates code, or treats an artificial Analyzer chunk as a document boundary.
"""

from bisect import bisect_right
from dataclasses import dataclass
import re
import unicodedata
from urllib.parse import urlsplit

from string_spans import StringSpans


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


class BoundaryValues:
    """Lazy, request-local index of complete original-source containers."""

    def __init__(self, text):
        self.text = text
        self.content_start = len(text) - len(text.lstrip())
        self.content_end = len(text.rstrip())
        self._prepared = False

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

    def resolve(self, start, end):
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
        if not self._not_document(bounds):
            return None
        left, right = bounds
        # A prose paragraph must not swallow syntax or several string values.
        if (self.string_owners.intersects(left, right)
                or any(c in self.text[left:right] for c in "{}[]")):
            return None
        return bounds


@dataclass(frozen=True)
class BoundaryContext:
    values: BoundaryValues
    offset: int = 0
