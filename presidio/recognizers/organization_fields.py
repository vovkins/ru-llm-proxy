"""Additive name coverage from explicit, bounded legal-organization fields.

No model inference, general prose detection, code evaluation or Base64 decode.
The generic legal prefix is context, never the new mask's target.
"""
from bisect import bisect_right
import json
import re

from presidio_analyzer import RecognizerResult
from string_spans import StringSpans
from result_merging import (
    DETECTION_SOURCE_METADATA_KEY, SOURCE_STRUCTURAL, SOURCE_NONE,
    MergeDecision, name_context_evidence,
)
from recognizers.json_strings import decoded_string_offsets

CONFIDENCE = .85
_KEY = r'(?:organization(?:[_-]?name)?|company(?:[_-]?name)?|org_name|организация|название[ \t]+организации|исполнитель|поставщик|заказчик)'
_HEADER = re.compile(rf'(?<![\w.-])(?P<quote>["\x27]?)(?:{_KEY})(?P=quote)[ \t\u00a0]{{0,16}}[:=][ \t\u00a0]{{0,16}}', re.IGNORECASE)
_FIELD_KEY = re.compile(_KEY, re.IGNORECASE)
_JSON_MEMBER = re.compile(r'[ \t\r\n]*:[ \t\r\n]*')
_LITERAL_END = re.compile(r'[ \t\u00a0]{0,16}(?:\.[ \t\u00a0]{0,16})?\Z')
_RAW_END = re.compile(r'(?:[ \t\u00a0]{0,16}\.[ \t\u00a0]{0,16}(?=\r|\n|\u2028|\u2029|\Z)|[ \t\r\n\u00a0\u2028\u2029]{0,16}\Z)')
_LEGAL = re.compile(r'(?i)(?:ООО|АО|ПАО|ОАО|ЗАО)\Z')
_PREFIX = re.compile(r'(?i)(?:ООО|АО|ПАО|ОАО|ЗАО)[ \t\u00a0]{1,16}')
_WORD = r'[A-ZА-ЯЁ][A-Za-zА-Яа-яЁё0-9]{1,39}(?:-[A-ZА-ЯЁ][A-Za-zА-Яа-яЁё0-9]{1,39})?'
_UNQUOTED_NAME = re.compile(rf'{_WORD}(?:[ \t\u00a0]{{1,16}}{_WORD}){{0,2}}(?![\w-])')
_QUOTES = {'«':'»','„':'“','“':'”','"':'"',"'":"'"}
MAX_NAME_CHARS = 128
MAX_RAW_LITERAL = 2048
MAX_CONTEXT = 192


def _value(text, start=0):
    """Return a name-only range for a legal value starting exactly at start."""
    contexts = name_context_evidence(text[start:start + MAX_CONTEXT])
    context = next((c for c in contexts if c.start == 0 and c.legal_form_end is not None), None)
    if context is not None:
        left, right = start + context.legal_form_end, start + context.end
        while left < right and text[left] in ' \t\u00a0':left += 1
        if not any(_LEGAL.fullmatch(word) for word in text[left:right].split()):
            return left, right
        return None
    prefix = _PREFIX.match(text, start)
    if prefix is None:return None
    left = prefix.end()
    if left >= len(text):return None
    if text[left] in _QUOTES:
        closing = _QUOTES[text[left]]
        right = text.find(closing, left + 1, min(len(text),left + MAX_NAME_CHARS + 3))
        if right < 0:return None
        body = text[left+1:right]
        if (not body or len(body)>MAX_NAME_CHARS or not any(ch.isalnum() for ch in body)
                or not all(ch.isalnum() or ch in " \t\u00a0_-'’–—&.,+" for ch in body)):
            return None
        # Include the inner quote pair, while preserving the actual outer
        # JSON/Python literal. Its source escape projection remains atomic.
        return left,right+1
    name = _UNQUOTED_NAME.match(text,left)
    if name is None or name.end()-left>MAX_NAME_CHARS:return None
    if any(_LEGAL.fullmatch(word) for word in name.group().split()):return None
    return left,name.end()


def organization_field_spans(text, check=lambda: None):
    if not isinstance(text, str):
        return ()
    first = _HEADER.search(text)
    escaped_json_keys = '\\u' in text and text.lstrip(' \t\r\n').startswith(('{', '['))
    if first is None and not escaped_json_keys:
        return ()
    check()
    strings = StringSpans(text)
    check()
    spans = []
    starts = [m.end() for m in _HEADER.finditer(text, first.start())] if first else []
    if escaped_json_keys:
        def reject_constant(_):
            raise ValueError('Non-JSON constant')
        try:
            json.loads(text, parse_int=str, parse_float=str, parse_constant=reject_constant)
        except (ValueError, RecursionError):
            pass
        else:
            check()
            for key in strings.spans:
                check()
                if key.end-key.start > 256 or text[key.start] != '"':
                    continue
                member = _JSON_MEMBER.match(text, key.end)
                raw = text[key.start:key.end]
                if member is not None and '\\u' in raw:
                    try:
                        decoded_key = json.loads(raw)
                    except ValueError:
                        continue
                    if isinstance(decoded_key, str) and _FIELD_KEY.fullmatch(decoded_key):
                        starts.append(member.end())
    for start in sorted(set(starts)):
        check()
        literal = strings.at(start)
        if literal is not None and literal.start == start:
            # Only complete, bounded ordinary literals; no Python f/raw/triple
            # evaluation. JSON escapes use the existing exact offset projector.
            if literal.end - literal.start > MAX_RAW_LITERAL or literal.content_start != literal.start + 1:
                continue
            opening = text[literal.start]
            if opening == '"':
                token = text[literal.start:literal.end]
                try:
                    json.loads(token)
                    decoded, offsets = decoded_string_offsets(text, literal.start, literal.end)
                except (ValueError, IndexError):
                    continue
            elif opening == "'" and '\\' not in text[literal.content_start:literal.content_end]:
                decoded = text[literal.content_start:literal.content_end]
                offsets = range(literal.content_start, literal.content_end + 1)
            else:
                continue
            leading = len(decoded) - len(decoded.lstrip(' \t\u00a0'))
            value = _value(decoded, leading)
            if value is not None and _LITERAL_END.match(decoded, value[1]):
                spans.append((offsets[value[0]], offsets[value[1]]))
        else:
            value = _value(text, start)
            if value is None:
                continue
            # A declaration inside a literal cannot borrow a newline or end
            # belonging outside that literal to close its value.
            owned_end = literal is not None and value[1] == literal.content_end
            plain_end = literal is None and _RAW_END.match(text, value[1]) is not None
            if owned_end or plain_end:
                spans.append(value)
    return tuple(sorted(set(spans)))


def add_organization_field_coverage(text, existing, *, requested_entities=None, score_threshold=.35, check=lambda: None):
    """Keep every old result unchanged and add only uncovered name characters."""
    if score_threshold > CONFIDENCE or requested_entities and 'ORGANIZATION' not in requested_entities:
        return existing, ()
    spans = organization_field_spans(text, check)
    if not spans:
        return existing, ()
    occupied = []
    for left, right in sorted((r.start, r.end) for r in existing):
        if occupied and left <= occupied[-1][1]:
            occupied[-1] = (occupied[-1][0], max(right, occupied[-1][1]))
        else:
            occupied.append((left, right))
    ends = [right for _, right in occupied]
    gaps = []
    for left, right in spans:
        check()
        cursor = left
        i = bisect_right(ends, cursor)
        for index in range(i, len(occupied)):
            a, b = occupied[index]
            if a >= right:
                break
            if cursor < a:
                gaps.append((cursor, min(a, right)))
            cursor = max(cursor, b)
            if cursor >= right:
                break
        if cursor < right:
            gaps.append((cursor, right))
    additions = []
    previous_end = -1
    for left, right in sorted(set(gaps)):
        left = max(left, previous_end)
        while left < right and text[left].isspace(): left += 1
        while right > left and text[right-1].isspace(): right -= 1
        if left >= right:
            continue
        additions.append(RecognizerResult('ORGANIZATION', left, right, CONFIDENCE, recognition_metadata={
            DETECTION_SOURCE_METADATA_KEY: SOURCE_STRUCTURAL,
            RecognizerResult.RECOGNIZER_NAME_KEY: 'OrganizationFieldRule',
            'organization_field_value': True,
        }))
        previous_end = right
    if not additions:
        return existing, ()
    return tuple(sorted([*existing, *additions], key=lambda r:(r.start,r.end,r.entity_type))), (
        MergeDecision('organization_field_value_added', SOURCE_STRUCTURAL, SOURCE_NONE),
    )
