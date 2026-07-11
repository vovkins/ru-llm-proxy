"""Deterministic reversible dictionary substitutions for provider-bound text."""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import re
from typing import Any


class DictionaryPolicyConfigError(ValueError):
    """Raised when dictionary substitution configuration is invalid."""


class DictionaryPolicyAmbiguousRequestError(ValueError):
    """Raised when a request cannot be restored deterministically."""


@dataclass(frozen=True)
class DictionarySubstitutionRule:
    """One deterministic source -> replacement substitution rule."""

    rule_id: str
    source: str
    replacement: str
    case_sensitive: bool = False
    whole_phrase: bool = True
    restore: bool = True

    def compile_pattern(self) -> re.Pattern[str]:
        flags = re.UNICODE
        if not self.case_sensitive:
            flags |= re.IGNORECASE

        escaped_source = re.escape(self.source)
        if self.whole_phrase:
            pattern = rf"(?<!\w){escaped_source}(?!\w)"
        else:
            pattern = escaped_source
        return re.compile(pattern, flags)


@dataclass(frozen=True)
class DictionarySubstitutionResult:
    """Result of applying dictionary substitutions to one text field."""

    text: str
    mapping: dict[str, str]
    replacement_spans: tuple[tuple[int, int], ...]
    rule_counts: dict[str, int]

    @property
    def substitution_count(self) -> int:
        return sum(self.rule_counts.values())


@dataclass(frozen=True)
class _RuleMatch:
    start: int
    end: int
    rule: DictionarySubstitutionRule
    original: str


class DictionarySubstitutionPolicy:
    """Applies validated exact phrase substitutions and builds restore mapping."""

    def __init__(self, rules: list[DictionarySubstitutionRule]):
        self.rules = sorted(
            rules,
            key=lambda rule: (-len(rule.source), rule.rule_id),
        )
        self._patterns = {rule.rule_id: rule.compile_pattern() for rule in self.rules}

    @classmethod
    def from_config(cls, config: Any) -> "DictionarySubstitutionPolicy":
        """Build a policy from parsed JSON config."""
        entries = config.get("substitutions") if isinstance(config, dict) else config
        if entries is None:
            entries = []
        if not isinstance(entries, list):
            raise DictionaryPolicyConfigError("substitutions must be a JSON array")

        rules: list[DictionarySubstitutionRule] = []
        seen_ids: set[str] = set()
        restore_replacements: dict[str, str] = {}

        for index, entry in enumerate(entries):
            if not isinstance(entry, dict):
                raise DictionaryPolicyConfigError(
                    f"substitution {index + 1} must be an object"
                )
            if entry.get("enabled", True) is False:
                continue

            rule_id = _safe_rule_id(entry.get("id") or entry.get("rule_id"))
            if not rule_id:
                raise DictionaryPolicyConfigError(
                    f"substitution {index + 1} is missing id"
                )
            if rule_id in seen_ids:
                raise DictionaryPolicyConfigError(f"duplicate substitution id {rule_id}")
            seen_ids.add(rule_id)

            source = _required_text(entry.get("source"), f"{rule_id}.source")
            replacement = _required_text(
                entry.get("replacement"),
                f"{rule_id}.replacement",
            )
            if source.casefold() == replacement.casefold():
                raise DictionaryPolicyConfigError(
                    f"substitution {rule_id} source and replacement must differ"
                )

            match_config = entry.get("match") or {}
            if not isinstance(match_config, dict):
                raise DictionaryPolicyConfigError(f"{rule_id}.match must be an object")

            restore = _bool_value(entry.get("restore", True))
            if restore:
                replacement_key = replacement.casefold()
                previous = restore_replacements.get(replacement_key)
                if previous and previous != rule_id:
                    raise DictionaryPolicyConfigError(
                        "duplicate restore replacement "
                        f"{replacement!r} in {previous} and {rule_id}"
                    )
                restore_replacements[replacement_key] = rule_id

            rules.append(
                DictionarySubstitutionRule(
                    rule_id=rule_id,
                    source=source,
                    replacement=replacement,
                    case_sensitive=_bool_value(
                        match_config.get("case_sensitive", False)
                    ),
                    whole_phrase=_bool_value(match_config.get("whole_phrase", True)),
                    restore=restore,
                )
            )

        return cls(rules)

    @classmethod
    def from_json_text(cls, raw_config: str) -> "DictionarySubstitutionPolicy":
        """Build a policy from JSON text."""
        try:
            parsed = json.loads(raw_config)
        except (TypeError, ValueError) as exc:
            raise DictionaryPolicyConfigError("invalid dictionary JSON") from exc
        return cls.from_config(parsed)

    @classmethod
    def from_file(cls, path: str | Path) -> "DictionarySubstitutionPolicy":
        """Build a policy from a UTF-8 JSON file."""
        config_path = Path(path)
        try:
            raw_config = config_path.read_text(encoding="utf-8")
        except OSError as exc:
            raise DictionaryPolicyConfigError(
                f"cannot read dictionary substitutions file: {config_path}"
            ) from exc
        return cls.from_json_text(raw_config)

    def apply(self, text: str) -> DictionarySubstitutionResult:
        """Return substituted text, restore mapping, and replacement spans."""
        if not text or not self.rules:
            return DictionarySubstitutionResult(text, {}, (), {})

        matches = self._select_matches(text)
        if not matches:
            return DictionarySubstitutionResult(text, {}, (), {})

        self._raise_if_ambiguous_original_replacement(text, matches)

        parts: list[str] = []
        mapping: dict[str, str] = {}
        replacement_spans: list[tuple[int, int]] = []
        rule_counts: dict[str, int] = {}
        last_end = 0
        output_length = 0

        for match in sorted(matches, key=lambda item: item.start):
            unchanged = text[last_end:match.start]
            parts.append(unchanged)
            output_length += len(unchanged)
            replacement_start = output_length
            parts.append(match.rule.replacement)
            replacement_end = replacement_start + len(match.rule.replacement)
            output_length = replacement_end
            replacement_spans.append((replacement_start, replacement_end))

            if match.rule.restore:
                mapping[match.rule.replacement] = match.original
            rule_counts[match.rule.rule_id] = rule_counts.get(match.rule.rule_id, 0) + 1
            last_end = match.end

        parts.append(text[last_end:])
        return DictionarySubstitutionResult(
            text="".join(parts),
            mapping=mapping,
            replacement_spans=tuple(replacement_spans),
            rule_counts=rule_counts,
        )

    def _select_matches(self, text: str) -> list[_RuleMatch]:
        selected: list[_RuleMatch] = []
        occupied: list[tuple[int, int]] = []

        for rule in self.rules:
            pattern = self._patterns[rule.rule_id]
            for match in pattern.finditer(text):
                start, end = match.span()
                if start >= end or _overlaps_any(start, end, occupied):
                    continue
                selected.append(
                    _RuleMatch(
                        start=start,
                        end=end,
                        rule=rule,
                        original=text[start:end],
                    )
                )
                occupied.append((start, end))

        return selected

    @staticmethod
    def _raise_if_ambiguous_original_replacement(
        text: str,
        matches: list[_RuleMatch],
    ) -> None:
        """Reject requests where replacement text already appears before mutation."""
        for match in matches:
            if not match.rule.restore:
                continue
            if match.rule.replacement in text:
                raise DictionaryPolicyAmbiguousRequestError(
                    f"replacement already present for rule {match.rule.rule_id}"
                )


def _overlaps_any(start: int, end: int, spans: list[tuple[int, int]]) -> bool:
    return any(start < span_end and end > span_start for span_start, span_end in spans)


def _safe_rule_id(value: Any) -> str:
    normalized = re.sub(
        r"[^a-zA-Z0-9_:-]+",
        "_",
        str(value or "").strip().lower(),
    ).strip("_:-")
    return normalized[:80]


def _required_text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise DictionaryPolicyConfigError(f"{field} must be a non-empty string")
    return value.strip()


def _bool_value(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() not in {"0", "false", "no", "off", "disabled"}
    return bool(value)
