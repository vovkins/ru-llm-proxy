"""Bounded, immutable alias allocation for one complete incoming request."""

from bisect import bisect_left, bisect_right
from dataclasses import dataclass
from functools import cached_property
import hashlib
import json
import re
from types import MappingProxyType

from .dictionary_policy import (
    DictionaryOutputCollisionError, DictionaryPolicyAmbiguousRequestError,
    aliases_collide_with_irreversible_output, irreversible_outputs,
)
from .restoration_mapping import json_atom_spans, json_escape_spans

MAX_REFERENCE_CHARACTERS = 8 * 1024 * 1024
MAX_PLANNED_ORIGINALS = 10000
MAX_ALLOCATION_ATTEMPTS = 4096
_NATIVE_MASK = re.compile(r'<[A-Z][A-Z0-9_]*_[1-9][0-9]*>')
_FOLDED_NATIVE_MASK = re.compile(r'<[a-z][a-z0-9_]*_[1-9][0-9]*>')
_NEUTRAL_DICTIONARY_MASK = re.compile(r'<DICT_[1-9][0-9]*>')


@dataclass(frozen=True, repr=False)
class DictionaryRequestPlan:
    policy: object
    aliases: object | None
    irreversible_outputs: frozenset[str]
    matches_by_text: object
    assembled_placeholders: frozenset[str] = frozenset()
    reference_texts: tuple[str, ...] = ()

    @cached_property
    def literal_dictionary_placeholders(self) -> frozenset[str]:
        return dictionary_literal_placeholders(self.reference_texts)

    def irreversible_placeholders(self):
        return self.assembled_placeholders | {
            match.group() for value in self.irreversible_outputs for match in _NATIVE_MASK.finditer(value)
        }

    def matches_for(self, text):
        try:
            return self.matches_by_text[text]
        except KeyError:
            raise DictionaryPolicyAmbiguousRequestError('Unplanned dictionary field') from None

    def replacement(self, rule, original):
        try:
            return self.aliases[(rule.rule_id, original)]
        except KeyError:
            raise DictionaryPolicyAmbiguousRequestError('Unplanned dictionary source') from None


def _reference_spellings(texts):
    """Reserve raw strings and real JSON scalar spellings; never decode code/base64."""
    budget = MAX_REFERENCE_CHARACTERS
    for text in texts:
        budget -= len(text)
        if budget < 0:
            raise DictionaryPolicyAmbiguousRequestError('Dictionary planning limit exceeded')
        yield text
        if not text.lstrip().startswith(('{', '[', '"')):
            continue
        try:
            value = json.loads(text, parse_int=str, parse_float=str,
                               parse_constant=lambda value: (_ for _ in ()).throw(ValueError()))
        except (ValueError, RecursionError):
            continue
        stack = [value]
        while stack:
            item = stack.pop()
            if isinstance(item, str):
                budget -= len(item)
                if budget < 0:
                    raise DictionaryPolicyAmbiguousRequestError('Dictionary planning limit exceeded')
                yield item
            elif isinstance(item, dict):
                stack.extend(item.keys())
                stack.extend(item.values())
            elif isinstance(item, list):
                stack.extend(item)


def dictionary_literal_placeholders(texts) -> frozenset[str]:
    """Reserve neutral tags that can be decoded by the JSON restorer."""
    # Only Unicode escapes can hide the ASCII characters in a neutral tag.
    escaped_json = dict.fromkeys(
        text for text in texts
        if '\\u' in text and text.lstrip().startswith(('{', '[', '"'))
    )
    return frozenset(
        match.group() for spelling in _reference_spellings(escaped_json)
        for match in _NEUTRAL_DICTIONARY_MASK.finditer(spelling)
    )


def _fold_output(text, ranges, start, end, escapes=()):
    """Retain substitution boundaries through casefold and JSON escapes."""
    if not escapes and start == 0 and end == len(text) and text.isascii():
        return text.lower(), ranges
    boundaries = sorted({start, end, *(point for left, right, _ in ranges for point in (left, right))})
    units = iter(escapes)
    unit = next(units, None)
    parts, offsets = [], {}
    cursor, size = start, 0

    def append(value):
        nonlocal size
        folded = value.casefold()
        parts.append(folded)
        size += len(folded)

    for boundary in boundaries:
        while unit and unit[1] <= boundary:
            left, right = unit
            append(text[cursor:left])
            append(json.loads('"' + text[left:right] + '"'))
            cursor = right
            unit = next(units, None)
        if unit and unit[0] < boundary:
            left, right = unit
            append(text[cursor:left])
            cursor = left
            offsets[boundary] = (size, size + len(json.loads('"' + text[left:right] + '"').casefold()))
        else:
            append(text[cursor:boundary])
            cursor = boundary
            offsets[boundary] = (size, size)
    return ''.join(parts), [(offsets[left][0], offsets[right][1], alias) for left, right, alias in ranges]


def _output_views(result):
    text = result.text
    irreversible = set(result.irreversible_spans)
    ranges = [(left, right, None if (left, right) in irreversible else text[left:right])
              for left, right in result.replacement_spans]
    yield _fold_output(text, ranges, 0, len(text))
    # The tool restorer also sees decoded JSON keys/values, including Unicode escapes.
    if '\\' in text:
        starts = [left for left, _, _ in ranges]
        ends = [right for _, right, _ in ranges]
        for start, end in json_atom_spans(text)[1]:
            overlaps = ranges[bisect_right(ends, start):bisect_left(starts, end)]
            if not any(alias is None for _, _, alias in overlaps):
                continue
            escapes = json_escape_spans(text, start, end)
            if escapes:
                clipped = [(max(start, left), min(end, right), alias) for left, right, alias in overlaps]
                yield _fold_output(text, clipped, start, end, escapes)


def _output_collisions(results, aliases, *, native_placeholders):
    folded_aliases = [(alias, alias.casefold()) for alias in aliases if alias]
    for result in results:
        if not result.irreversible_spans:
            continue
        for text, ranges in _output_views(result):
            irreversible = [(left, right) for left, right, alias in ranges if alias is None]
            irreversible_ends = [right for _, right in irreversible]
            for match in _FOLDED_NATIVE_MASK.finditer(text):
                index = bisect_right(irreversible_ends, match.start())
                if index < len(irreversible) and irreversible[index][0] < match.end():
                    native_placeholders.add(match.group().upper())
            starts = [left for left, _, _ in ranges]
            ends = [right for _, right, _ in ranges]
            for alias, needle in folded_aliases:
                length = len(needle)
                for left, right in irreversible:
                    start = max(0, left - length + 1)
                    end = right + length - 1
                    while (start := text.find(needle, start, end)) != -1:
                        overlaps = ranges[bisect_right(ends, start):bisect_left(starts, start + length)]
                        contributors = tuple(key for _, _, key in overlaps if key is not None)
                        yield alias, contributors
                        start += 1


def validate_fixed_output(plan, texts, inherited, reserved_placeholders):
    occupied = dict(inherited or {})
    reserved = set(reserved_placeholders) | plan.irreversible_placeholders()
    results = []
    for text in texts:
        result = plan.policy.apply(text, occupied, reserved, request_plan=plan)
        occupied.update(result.mapping)
        if result.irreversible_spans:
            results.append(result)
    assembled = set()
    if next(_output_collisions(results, occupied, native_placeholders=assembled), None) is not None:
        raise DictionaryOutputCollisionError('Irreversible output forms a reversible alias')
    return frozenset(assembled)


def build_request_plan(policy, texts, existing_mapping=None, *, reference_texts=None):
    texts = tuple(texts)
    if any(not isinstance(text, str) for text in texts):
        raise DictionaryPolicyAmbiguousRequestError('Invalid dictionary planning input')
    selected = policy._request_matches(texts)
    matches_by_text = selected.values()
    irreversible = irreversible_outputs(matches_by_text)
    inherited = existing_mapping or {}
    if any(aliases_collide_with_irreversible_output(alias, irreversible) for alias in inherited):
        raise DictionaryOutputCollisionError('Irreversible output contains an inherited alias')
    references = ()
    if any(match.rule.restore for matches in matches_by_text for match in matches):
        reference_texts = texts if reference_texts is None else tuple(reference_texts)
        if any(not isinstance(text, str) for text in reference_texts):
            raise DictionaryPolicyAmbiguousRequestError('Invalid dictionary planning reference')
        references = tuple(text.casefold() for text in _reference_spellings(reference_texts))
    occupied = dict(inherited)
    aliases = {}
    counter = 0

    def conflicts(candidate, original, *, trusted=False):
        folded = candidate.casefold()
        if aliases_collide_with_irreversible_output(candidate, irreversible):
            return True
        if any(key.casefold() == folded and value != original for key, value in occupied.items()):
            return True
        if not trusted and any(folded in text for text in references):
            return True
        if any(pattern.search(candidate) for pattern in policy._patterns.values()):
            return True
        return False

    def allocate(key, *, output_collision=False):
        nonlocal counter
        rule_id, original = key
        tag = hashlib.sha256(rule_id.encode()).hexdigest()[:10]
        while True:
            counter += 1
            if counter > MAX_ALLOCATION_ATTEMPTS:
                error = DictionaryOutputCollisionError if output_collision else DictionaryPolicyAmbiguousRequestError
                raise error('Dictionary allocation limit exceeded')
            replacement = f'rp_{tag}_{counter:06d}_x'
            # Prefix-free against inherited/planned keys, including ordinary aliases.
            overlaps = any(k.casefold() in replacement.casefold() or replacement.casefold() in k.casefold()
                           for k in occupied)
            if not overlaps and not conflicts(replacement, original):
                return replacement

    for matches in matches_by_text:
        for match in matches:
            if not match.rule.restore:
                continue
            key = match.rule.rule_id, match.original
            if key in aliases:
                continue
            if len(aliases) >= MAX_PLANNED_ORIGINALS:
                raise DictionaryPolicyAmbiguousRequestError('Dictionary planning limit exceeded')
            replacement = match.rule.replacement
            trusted = (
                inherited.get(replacement) == match.original
                and not _NATIVE_MASK.fullmatch(replacement)
                and replacement not in getattr(inherited, 'json_numbers', ())
                and replacement not in getattr(inherited, 'json_strings', ())
            )
            if conflicts(replacement, match.original, trusted=trusted):
                replacement = allocate(key)
            aliases[key] = replacement
            occupied[replacement] = match.original

    assembled = set()
    if irreversible:
        fields = [text for text, matches in selected.items() if any(not match.rule.restore for match in matches)]
        while True:
            preview = DictionaryRequestPlan(policy, aliases, irreversible, selected)
            outputs = (policy.apply(text, request_plan=preview) for text in fields)
            movable = {alias: key for key, alias in aliases.items() if alias not in inherited}
            reallocate = {}
            assembled.clear()
            for alias, contributors in _output_collisions(outputs, occupied, native_placeholders=assembled):
                candidate = next((item for item in (alias, *contributors) if item in movable), None)
                if candidate is None:
                    raise DictionaryOutputCollisionError('Irreversible output forms an inherited alias')
                reallocate[candidate] = movable[candidate]
            if not reallocate:
                break
            for alias, key in reallocate.items():
                del occupied[alias]
                replacement = aliases[key] = allocate(key, output_collision=True)
                occupied[replacement] = key[1]
    return DictionaryRequestPlan(policy, MappingProxyType(aliases), irreversible, selected, frozenset(assembled))
