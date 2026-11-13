"""String replacements with explicit, local-only JSON provenance."""

import json
import re


JSON_NUMBER = re.compile(r'-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?\Z')
_PLACEHOLDER = re.compile(r'<[A-Z][A-Z0-9_]*_[1-9][0-9]*>\Z')
_JSON_ATOM_START = re.compile(r'["0-9-]')
_JSON_ESCAPE = re.compile(r'\\(?:u[0-9a-fA-F]{4}|["\\/bfnrt])')
_JSON_KEY_SUFFIX = re.compile(r'[ \t\r\n]*:')


class RestorationKeyCollision(ValueError):
    """Restoring a data key would silently overwrite another JSON member."""


class RestorationMapping(dict[str, str]):
    """Keep the existing dictionary API; copies/updates also retain provenance."""

    def __init__(self, values=(), *, json_numbers=None, json_strings=None, json_keys=None):
        super().__init__(values)
        self.json_numbers = set(
            getattr(values, 'json_numbers', ()) if json_numbers is None else json_numbers
        )
        self.json_strings = set(
            getattr(values, 'json_strings', ()) if json_strings is None else json_strings
        )
        self.json_keys = set(getattr(values, 'json_keys', ()) if json_keys is None else json_keys)

    def __setitem__(self, key, value):
        super().__setitem__(key, value)
        self.json_numbers.discard(key)
        self.json_strings.discard(key)
        self.json_keys.discard(key)

    def update(self, values=(), **kwargs):
        incoming = dict(values, **kwargs)
        numbers = set(getattr(values, 'json_numbers', ())) - kwargs.keys()
        strings = set(getattr(values, 'json_strings', ())) - kwargs.keys()
        keys = set(getattr(values, 'json_keys', ())) - kwargs.keys()
        super().update(incoming)
        self.json_numbers.difference_update(incoming)
        self.json_numbers.update(numbers)
        self.json_strings.difference_update(incoming)
        self.json_strings.update(strings)
        self.json_keys.difference_update(incoming)
        self.json_keys.update(keys)

    def copy(self):
        return RestorationMapping(self)

    def add_number(self, key: str, literal: str) -> None:
        self[key] = literal
        self.json_numbers.add(key)

    def add_string(self, key: str, literal: str) -> None:
        json.loads('"' + literal + '"')
        self[key] = literal
        self.json_strings.add(key)

    def argument_values(self) -> dict[str, str]:
        return {key: json.loads('"' + value + '"') if key in self.json_strings else value
                for key, value in self.items()}


def encode_mapping(mapping: dict[str, str]) -> str:
    if not isinstance(mapping, dict) or any(
        not isinstance(key, str) or not key or not isinstance(value, str)
        for key, value in mapping.items()
    ):
        raise ValueError('Invalid restoration mapping')
    numbers = getattr(mapping, 'json_numbers', set())
    if not isinstance(numbers, (set, frozenset)) or any(
        not isinstance(key, str) or not _PLACEHOLDER.fullmatch(key)
        or key not in mapping or not JSON_NUMBER.fullmatch(mapping[key])
        for key in numbers
    ):
        raise ValueError('Invalid JSON-number provenance')
    body = ({'version': 2, 'values': dict(mapping), 'json_numbers': sorted(numbers)}
            if numbers else dict(mapping))
    strings = getattr(mapping, 'json_strings', set())
    if not isinstance(strings, (set, frozenset)) or numbers & strings or any(
        not isinstance(key, str) or not _PLACEHOLDER.fullmatch(key) or key not in mapping
        for key in strings
    ):
        raise ValueError('Invalid JSON-string provenance')
    for key in strings:
        json.loads('"' + mapping[key] + '"')
    if strings:
        body = {'version': 3, 'values': dict(mapping),
                'json_numbers': sorted(numbers), 'json_strings': sorted(strings)}
    keys = getattr(mapping, 'json_keys', set())
    if not isinstance(keys, (set, frozenset)) or any(
        not isinstance(key, str) or key not in mapping for key in keys
    ) or numbers & keys:
        raise ValueError('Invalid JSON-key provenance')
    if keys:
        body = {'version': 4, 'values': dict(mapping), 'json_numbers': sorted(numbers),
                'json_strings': sorted(strings), 'json_keys': sorted(keys)}
    return json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def decode_mapping(body: str) -> RestorationMapping:
    value = json.loads(body)
    if not isinstance(value, dict):
        raise ValueError('Invalid restoration mapping')
    if type(value.get('version')) is int:
        numbers = value.get('json_numbers')
        version = value['version']
        fields = {'version', 'values', 'json_numbers'}
        if version in (3, 4):
            fields.add('json_strings')
        if version == 4:
            fields.add('json_keys')
        strings = value.get('json_strings', [])
        keys = value.get('json_keys', [])
        if (version not in (2, 3, 4) or set(value) != fields
                or not isinstance(value['values'], dict) or not isinstance(numbers, list)
                or any(not isinstance(key, str) for key in numbers)
                or len(numbers) != len(set(numbers))
                or not isinstance(strings, list) or any(not isinstance(key, str) for key in strings)
                or len(strings) != len(set(strings))
                or not isinstance(keys, list) or any(not isinstance(key, str) for key in keys)
                or len(keys) != len(set(keys))):
            raise ValueError('Invalid versioned restoration mapping')
        mapping = RestorationMapping(value['values'], json_numbers=numbers, json_strings=strings,
                                     json_keys=keys)
    else:
        mapping = RestorationMapping(value)
    encode_mapping(mapping)
    return mapping


def json_atom_spans(text: str) -> tuple[list[tuple[int, int]], list[tuple[int, int]]]:
    """Validate with the standard parser; scan offsets without reprinting source."""
    if not text.lstrip().startswith(('{', '[', '"', '-', *'0123456789')):
        return [], []

    def reject_constant(_value):
        raise ValueError('Non-JSON number')

    try:
        # Preserve arbitrary precision/exponents and avoid Python's int digit cap.
        decoder = json.JSONDecoder(parse_int=str, parse_float=str, parse_constant=reject_constant)
        decoder.decode(text)
    except (ValueError, RecursionError):
        return [], []
    numbers, strings = [], []
    cursor = 0
    while match := _JSON_ATOM_START.search(text, cursor):
        start = match.start()
        _value, cursor = decoder.raw_decode(text, start)
        if text[start] != '"':
            numbers.append((start, cursor))
        else:
            strings.append((start + 1, cursor - 1))
    return numbers, strings


def json_number_spans(text: str) -> list[tuple[int, int]]:
    return json_atom_spans(text)[0]


def json_key_spans(text: str) -> list[tuple[int, int]]:
    """Only keys of a validated JSON document, in original source offsets."""
    return [(start, end) for start, end in json_atom_spans(text)[1]
            if _JSON_KEY_SUFFIX.match(text, end + 1)]


def json_escape_spans(text: str, start: int, end: int) -> list[tuple[int, int]]:
    """Atomic escapes in an already validated JSON string, in source coordinates."""
    spans = []
    for match in _JSON_ESCAPE.finditer(text, start, end):
        left, right = match.span()
        if (spans and text[left:left + 2] == '\\u' and right - left == 6
                and 0xDC00 <= int(text[left + 2:right], 16) <= 0xDFFF):
            previous = spans[-1]
            if (previous[1] == left and text[previous[0]:previous[0] + 2] == '\\u'
                    and previous[1] - previous[0] == 6
                    and 0xD800 <= int(text[previous[0] + 2:left], 16) <= 0xDBFF):
                spans[-1] = (previous[0], right)
                continue
        spans.append((left, right))
    return spans
