"""String replacements with explicit, local-only JSON-number provenance."""

import json
import re


JSON_NUMBER = re.compile(r'-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?\Z')
_PLACEHOLDER = re.compile(r'<[A-Z][A-Z0-9_]*_[1-9][0-9]*>\Z')
_JSON_ATOM_START = re.compile(r'["0-9-]')


class RestorationMapping(dict[str, str]):
    """Keep the existing dictionary API; copies/updates also retain provenance."""

    def __init__(self, values=(), *, json_numbers=None):
        super().__init__(values)
        self.json_numbers = set(
            getattr(values, 'json_numbers', ()) if json_numbers is None else json_numbers
        )

    def __setitem__(self, key, value):
        super().__setitem__(key, value)
        self.json_numbers.discard(key)

    def update(self, values=(), **kwargs):
        incoming = dict(values, **kwargs)
        numbers = set(getattr(values, 'json_numbers', ())) - kwargs.keys()
        super().update(incoming)
        self.json_numbers.difference_update(incoming)
        self.json_numbers.update(numbers)

    def copy(self):
        return RestorationMapping(self)

    def add_number(self, key: str, literal: str) -> None:
        self[key] = literal
        self.json_numbers.add(key)


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
    return json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def decode_mapping(body: str) -> RestorationMapping:
    value = json.loads(body)
    if not isinstance(value, dict):
        raise ValueError('Invalid restoration mapping')
    if type(value.get('version')) is int:
        numbers = value.get('json_numbers')
        if (value['version'] != 2 or set(value) != {'version', 'values', 'json_numbers'}
                or not isinstance(value['values'], dict) or not isinstance(numbers, list)
                or any(not isinstance(key, str) for key in numbers)
                or len(numbers) != len(set(numbers))):
            raise ValueError('Invalid versioned restoration mapping')
        mapping = RestorationMapping(value['values'], json_numbers=numbers)
    else:
        mapping = RestorationMapping(value)
    encode_mapping(mapping)
    return mapping


def json_number_spans(text: str) -> list[tuple[int, int]]:
    """Validate with the standard parser; scan offsets without reprinting source."""
    if not text.lstrip().startswith(('{', '[', '"', '-', *'0123456789')):
        return []

    def reject_constant(_value):
        raise ValueError('Non-JSON number')

    try:
        # Preserve arbitrary precision/exponents and avoid Python's int digit cap.
        decoder = json.JSONDecoder(parse_int=str, parse_float=str, parse_constant=reject_constant)
        decoder.decode(text)
    except (ValueError, RecursionError):
        return []
    spans = []
    cursor = 0
    while match := _JSON_ATOM_START.search(text, cursor):
        start = match.start()
        _value, cursor = decoder.raw_decode(text, start)
        if text[start] != '"':
            spans.append((start, cursor))
    return spans
