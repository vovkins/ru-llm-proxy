"""Bounded, immutable alias allocation for one complete incoming request."""

from dataclasses import dataclass
import hashlib
import json
import re
from types import MappingProxyType

from .dictionary_policy import DictionaryPolicyAmbiguousRequestError

MAX_REFERENCE_CHARACTERS = 8 * 1024 * 1024
MAX_PLANNED_ORIGINALS = 10000
MAX_ALLOCATION_ATTEMPTS = 4096
_NATIVE_MASK = re.compile(r'<[A-Z][A-Z0-9_]*_[1-9][0-9]*>\Z')


@dataclass(frozen=True, repr=False)
class DictionaryRequestPlan:
    policy: object
    aliases: object

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


def build_request_plan(policy, texts, existing_mapping=None, *, reference_texts=None):
    texts = tuple(texts)
    if any(not isinstance(text, str) for text in texts):
        raise DictionaryPolicyAmbiguousRequestError('Invalid dictionary planning input')
    matches_by_text = tuple(policy._select_matches(text) for text in texts)
    if not any(match.rule.restore for matches in matches_by_text for match in matches):
        return DictionaryRequestPlan(policy, MappingProxyType({}))
    reference_texts = texts if reference_texts is None else tuple(reference_texts)
    if any(not isinstance(text, str) for text in reference_texts):
        raise DictionaryPolicyAmbiguousRequestError('Invalid dictionary planning reference')
    references = tuple(text.casefold() for text in _reference_spellings(reference_texts))
    inherited = existing_mapping or {}
    occupied = dict(inherited)
    aliases = {}
    counter = 0

    def conflicts(candidate, original, *, trusted=False):
        folded = candidate.casefold()
        if any(key.casefold() == folded and value != original for key, value in occupied.items()):
            return True
        if not trusted and any(folded in text for text in references):
            return True
        if any(pattern.search(candidate) for pattern in policy._patterns.values()):
            return True
        return False

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
                tag = hashlib.sha256(match.rule.rule_id.encode()).hexdigest()[:10]
                while True:
                    counter += 1
                    if counter > MAX_ALLOCATION_ATTEMPTS:
                        raise DictionaryPolicyAmbiguousRequestError('Dictionary allocation limit exceeded')
                    replacement = f'rp_{tag}_{counter:06d}_x'
                    # Prefix-free against inherited/planned keys, including ordinary aliases.
                    overlaps = any(k.casefold() in replacement.casefold() or replacement.casefold() in k.casefold()
                                   for k in occupied)
                    if not overlaps and not conflicts(replacement, match.original):
                        break
            aliases[key] = replacement
            occupied[replacement] = match.original
    return DictionaryRequestPlan(policy, MappingProxyType(aliases))
