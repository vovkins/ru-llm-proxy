#!/usr/bin/env python3
"""Compare pinned model repository rules with active native recognizers."""

from __future__ import annotations

import argparse
import hashlib
import importlib
import json
import os
import re
import subprocess
import sys
import time
import warnings
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

try:
    import tomllib
except ImportError:  # pragma: no cover - Python 3.9 in the local development venv
    import tomli as tomllib


EVALUATION_DIR = Path(__file__).resolve().parent
PRESIDIO_DIR = EVALUATION_DIR.parent
REPOSITORY_ROOT = PRESIDIO_DIR.parent
DEFAULT_MANIFEST_PATH = EVALUATION_DIR / "deterministic_rule_source_manifest.json"
DEFAULT_CORPUS_PATH = EVALUATION_DIR / "data" / "deterministic_rules_review.jsonl"
DEFAULT_REPORT_PATH = EVALUATION_DIR / "reports" / "deterministic-rule-review.json"
DEFAULT_INVENTORY_PATH = EVALUATION_DIR / "reports" / "deterministic-rule-inventory.json"

ANNOTATION_RE = re.compile(r"\{\{(?P<entity>[A-Z][A-Z0-9_]*):(?P<value>.*?)\}\}", re.DOTALL)
CASE_ID_RE = re.compile(r"^[a-z][a-z0-9_]*$")
TARGET_TYPES = frozenset(
    {"LOGIN", "PASSWORD", "AUTH_TOKEN", "SECRET_KEY", "CONTRACT_NUMBER"}
)
CURRENT_ENTITY_MAP = {
    "API_KEY": "SECRET_KEY",
    "AUTH_TOKEN": "AUTH_TOKEN",
    "BEARER_TOKEN": "AUTH_TOKEN",
    "DB_URL": "SECRET_KEY",
    "JWT": "AUTH_TOKEN",
    "LOGIN": "LOGIN",
    "PASSWORD": "PASSWORD",
    "PRIVATE_KEY": "SECRET_KEY",
    "SECRET_KEY": "SECRET_KEY",
}

# These decisions preserve the reviewed source inventory; accepted adaptations are
# registered separately in presidio/recognizers/credential_rules.py.
ALREADY_COVERED_RULES = frozenset(
    {
        "anthropic-admin-api-key",
        "anthropic-api-key",
        "gcp-api-key",
        "github-pat",
        "gitlab-pat",
        "jwt",
        "openai-api-key",
        "private-key",
        "slack-bot-token",
    }
)
ADAPT_RULES = frozenset(
    {
        "aws-access-token",
        "aws-amazon-bedrock-api-key-long-lived",
        "aws-amazon-bedrock-api-key-short-lived",
        "curl-auth-header",
        "curl-auth-user",
        "github-app-token",
        "github-fine-grained-pat",
        "github-oauth",
        "github-refresh-token",
        "huggingface-access-token",
        "huggingface-organization-api-token",
        "slack-app-token",
        "slack-config-access-token",
        "slack-config-refresh-token",
        "slack-legacy-bot-token",
        "slack-legacy-token",
        "slack-legacy-workspace-token",
        "slack-user-token",
        "vault-batch-token",
        "vault-service-token",
        "yandex-access-token",
        "yandex-api-key",
        "yandex-aws-access-token",
    }
)
REJECT_RULES = frozenset(
    {
        "adobe-client-id",
        "asana-client-id",
        "bitbucket-client-id",
        "discord-client-id",
        "flutterwave-public-key",
        "generic-api-key",
        "linkedin-client-id",
        "lob-pub-api-key",
        "looker-client-id",
        "mailgun-pub-key",
        "messagebird-client-id",
        "new-relic-user-api-id",
        "pkcs12-file",
        "plaid-client-id",
        "sendbird-access-id",
        "sumologic-access-id",
    }
)


@dataclass(frozen=True, order=True)
class Span:
    entity_type: str
    start: int
    end: int
    detector: str = "expected"


@dataclass(frozen=True)
class ReviewCase:
    case_id: str
    family: str
    text: str
    expected: tuple[Span, ...]
    tags: tuple[str, ...]


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source_file:
        for chunk in iter(lambda: source_file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_and_verify_source(source_dir: Path, manifest_path: Path) -> dict[str, Any]:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    failures = []
    for file_record in manifest["model_repository"]["files"]:
        source_path = source_dir / file_record["path"]
        if not source_path.is_file():
            failures.append(f"missing {file_record['path']}")
            continue
        actual_size = source_path.stat().st_size
        actual_sha256 = sha256_file(source_path)
        if actual_size != file_record["size_bytes"]:
            failures.append(f"size mismatch for {file_record['path']}")
        if actual_sha256 != file_record["sha256"]:
            failures.append(f"checksum mismatch for {file_record['path']}")
    if failures:
        raise ValueError("source verification failed: " + "; ".join(failures))
    return manifest


def _expand_repeat(record: dict[str, Any], field: str) -> str:
    repeat = record.get(field)
    if repeat is None:
        return ""
    if not isinstance(repeat, dict) or set(repeat) != {"text", "count"}:
        raise ValueError(f"{field} must contain text and count")
    text = repeat["text"]
    count = repeat["count"]
    if not isinstance(text, str) or not text:
        raise ValueError(f"{field}.text must be a non-empty string")
    if isinstance(count, bool) or not isinstance(count, int) or not 1 <= count <= 1000:
        raise ValueError(f"{field}.count must be from 1 to 1000")
    return text * count


def parse_annotated_text(annotated_text: str) -> tuple[str, tuple[Span, ...]]:
    plain_parts: list[str] = []
    expected: list[Span] = []
    source_cursor = 0
    plain_length = 0
    for match in ANNOTATION_RE.finditer(annotated_text):
        prefix = annotated_text[source_cursor : match.start()]
        if "{{" in prefix or "}}" in prefix:
            raise ValueError("malformed annotation")
        plain_parts.append(prefix)
        plain_length += len(prefix)
        entity_type = match.group("entity")
        value = match.group("value")
        if entity_type not in TARGET_TYPES or not value:
            raise ValueError(f"invalid annotation type or value: {entity_type}")
        start = plain_length
        plain_parts.append(value)
        plain_length += len(value)
        expected.append(Span(entity_type, start, plain_length))
        source_cursor = match.end()
    suffix = annotated_text[source_cursor:]
    if "{{" in suffix or "}}" in suffix:
        raise ValueError("malformed annotation")
    plain_parts.append(suffix)
    return "".join(plain_parts), tuple(expected)


def load_corpus(path: Path) -> tuple[ReviewCase, ...]:
    cases = []
    for line_number, raw_line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not raw_line.strip():
            continue
        record = json.loads(raw_line)
        case_id = record.get("id")
        family = record.get("family")
        tags = record.get("tags")
        if not isinstance(case_id, str) or not CASE_ID_RE.fullmatch(case_id):
            raise ValueError(f"line {line_number}: invalid case id")
        if family not in {"kv", "cli", "opaque", "gitleaks", "contract", "all"}:
            raise ValueError(f"line {line_number}: invalid family")
        if not isinstance(tags, list) or set(tags).isdisjoint({"positive", "negative"}):
            raise ValueError(f"line {line_number}: tags need polarity")
        annotated = (
            _expand_repeat(record, "prefix_repeat")
            + record["annotated_text"]
            + _expand_repeat(record, "suffix_repeat")
        )
        text, expected = parse_annotated_text(annotated)
        if ("positive" in tags) != bool(expected):
            raise ValueError(f"line {line_number}: polarity does not match annotations")
        cases.append(ReviewCase(case_id, family, text, expected, tuple(tags)))
    if len({case.case_id for case in cases}) != len(cases):
        raise ValueError("case ids must be unique")
    if not cases:
        raise ValueError("corpus must not be empty")
    return tuple(cases)


def import_verified_scrubber(source_dir: Path):
    for module_name in tuple(sys.modules):
        if module_name == "core" or module_name.startswith("core."):
            del sys.modules[module_name]
    sys.path.insert(0, str(source_dir))
    try:
        return importlib.import_module("core.scrubber")
    finally:
        sys.path.pop(0)


def isolated_gitleaks_predictions(scrubber, text: str, rules: list) -> list[Span]:
    """Apply only the source loader's Gitleaks rules, without its entropy fallback."""
    spans = []
    lowered = text.lower()
    for pattern, label, keywords, group, entropy in rules:
        if keywords and not any(keyword in lowered for keyword in keywords):
            continue
        for match in pattern.finditer(text):
            start, end = match.span(group)
            if start < 0 or end <= start:
                start, end = match.span()
            if entropy and scrubber._entropy(text[start:end]) < entropy:
                continue
            start, end = scrubber.detector_core(text, start, end)
            if start < end:
                spans.append(Span(label or "SECRET", start, end, "gitleaks"))
    return spans


def source_predictions(scrubber, text: str, rules: list) -> tuple[Span, ...]:
    spans = [
        Span(site.label, site.start, site.end, site.detector)
        for site in scrubber.credential_sites(text, [])
    ]
    spans.extend(isolated_gitleaks_predictions(scrubber, text, rules))
    for match in scrubber.CONTRACT_RE.finditer(text):
        spans.append(Span("CONTRACT_NUMBER", match.start(1), match.end(1), "contract"))
    return tuple(sorted(set(spans)))


def current_predictions(text: str) -> tuple[Span, ...]:
    if str(PRESIDIO_DIR) not in sys.path:
        sys.path.insert(0, str(PRESIDIO_DIR))
    from recognizers import ALL_RECOGNIZERS

    spans = []
    for factory in ALL_RECOGNIZERS:
        recognizer = factory()
        supported = set(getattr(recognizer, "supported_entities", ()) or ())
        if supported.isdisjoint(CURRENT_ENTITY_MAP):
            continue
        for result in recognizer.analyze(text, entities=None, nlp_artifacts=None):
            mapped_type = CURRENT_ENTITY_MAP.get(result.entity_type)
            if mapped_type:
                spans.append(Span(mapped_type, result.start, result.end, type(recognizer).__name__))
    return tuple(sorted(set(spans)))


def _overlaps(left: Span, right: Span) -> bool:
    return left.start < right.end and right.start < left.end


def calculate_metrics(cases: Iterable[ReviewCase], predictions: dict[str, tuple[Span, ...]]) -> dict[str, Any]:
    expected_count = predicted_count = exact_tp = typed_covered = masking_covered = 0
    false_positive_predictions = 0
    findings = []
    for case in cases:
        expected = set(case.expected)
        predicted = {
            Span(item.entity_type, item.start, item.end)
            for item in predictions.get(case.case_id, ())
        }
        exact = expected.intersection(predicted)
        expected_count += len(expected)
        predicted_count += len(predicted)
        exact_tp += len(exact)
        typed_covered += sum(
            1
            for wanted in expected
            if any(wanted.entity_type == item.entity_type and _overlaps(wanted, item) for item in predicted)
        )
        masking_covered += sum(
            1 for wanted in expected if any(_overlaps(wanted, item) for item in predicted)
        )
        false_positive_predictions += sum(
            1 for item in predicted if not any(_overlaps(item, wanted) for wanted in expected)
        )
        if exact != expected or any(not any(_overlaps(item, wanted) for wanted in expected) for item in predicted):
            findings.append(
                {
                    "case_id": case.case_id,
                    "family": case.family,
                    "expected_types": sorted(item.entity_type for item in expected),
                    "predicted_types": sorted(item.entity_type for item in predicted),
                }
            )
    exact_fp = predicted_count - exact_tp
    exact_fn = expected_count - exact_tp
    precision = exact_tp / predicted_count if predicted_count else 0.0
    recall = exact_tp / expected_count if expected_count else 0.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    return {
        "case_count": len(tuple(cases)) if not isinstance(cases, tuple) else len(cases),
        "expected_count": expected_count,
        "predicted_count": predicted_count,
        "exact_true_positives": exact_tp,
        "exact_false_positives": exact_fp,
        "exact_false_negatives": exact_fn,
        "exact_precision": round(precision, 6),
        "exact_recall": round(recall, 6),
        "exact_f1": round(f1, 6),
        "typed_overlap_recall": round(typed_covered / expected_count, 6) if expected_count else 0.0,
        "masking_overlap_recall": round(masking_covered / expected_count, 6) if expected_count else 0.0,
        "false_positive_predictions": false_positive_predictions,
        "findings": findings,
    }


def _python_regex_status(rule: dict[str, Any]) -> tuple[bool | None, str | None]:
    regex = rule.get("regex")
    if not isinstance(regex, str):
        return None, "missing_regex"
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", DeprecationWarning)
            warnings.simplefilter("error", FutureWarning)
            re.compile(regex)
        return True, None
    except (re.error, DeprecationWarning, FutureWarning) as exc:
        return False, type(exc).__name__


def _loader_label(identifier: str) -> str | None:
    lowered = identifier.lower()
    if "token" in lowered:
        return "AUTH_TOKEN"
    if "key" in lowered or "secret" in lowered:
        return "SECRET_KEY"
    return None


def _rule_decision(identifier: str) -> tuple[str, str]:
    if identifier in ALREADY_COVERED_RULES:
        return "already_covered", "existing_structured_recognizer"
    if identifier in ADAPT_RULES:
        return "adapt", "high_signal_format_requires_native_presidio_rule"
    if identifier in REJECT_RULES:
        return "reject", "public_identifier_generic_rule_or_file_metadata"
    return "requires_evidence", "provider_specific_rule_not_in_active_default_scope"


def build_inventory(gitleaks_path: Path, manifest: dict[str, Any]) -> dict[str, Any]:
    raw_rules = tomllib.loads(gitleaks_path.read_text(encoding="utf-8")).get("rules", [])
    records = []
    for rule in raw_rules:
        identifier = str(rule.get("id", ""))
        label = _loader_label(identifier)
        compatible, incompatibility = _python_regex_status(rule)
        decision, reason = _rule_decision(identifier)
        risk_flags = []
        if rule.get("allowlists"):
            risk_flags.append("rule_allowlist_ignored_by_model_loader")
        if rule.get("path"):
            risk_flags.append("path_constraint_ignored_by_model_loader")
        if rule.get("entropy"):
            risk_flags.append("entropy_threshold_present")
        if compatible is False:
            risk_flags.append("not_python_re_compatible")
        if label is None:
            risk_flags.append("excluded_by_identifier_label_mapping")
        records.append(
            {
                "source_id": identifier,
                "source_loader_label": label,
                "source_python_compatible": compatible,
                "source_loader_would_load": bool(label and compatible),
                "source_has_entropy_threshold": bool(rule.get("entropy")),
                "source_has_keywords": bool(rule.get("keywords")),
                "source_has_rule_allowlist": bool(rule.get("allowlists")),
                "source_has_path_constraint": bool(rule.get("path")),
                "current_coverage": (
                    "full" if identifier in ALREADY_COVERED_RULES else
                    "adapted" if identifier in ADAPT_RULES else
                    "not_demonstrated"
                ),
                "recommendation": decision,
                "reason_code": reason,
                "risk_flags": risk_flags,
                "compatibility_error_type": incompatibility,
            }
        )
    decision_counts = Counter(item["recommendation"] for item in records)
    loader_counts = Counter(
        "loaded" if item["source_loader_would_load"] else
        "incompatible" if item["source_loader_label"] and item["source_python_compatible"] is False else
        "missing_regex" if item["source_loader_label"] and item["source_python_compatible"] is None else
        "excluded"
        for item in records
    )
    return {
        "schema_version": 1,
        "source": {
            "repository": manifest["model_repository"]["repository"],
            "revision": manifest["model_repository"]["revision"],
            "gitleaks_sha256": manifest["gitleaks_upstream"]["sha256"],
            "upstream_git_blob_sha1": manifest["gitleaks_upstream"]["git_blob_sha1"],
        },
        "policy": {
            "active_runtime_rules_changed": True,
            "selected_formats_adapted": True,
            "regex_and_descriptions_copied": False,
            "approval_required_before_integration": False,
        },
        "counts": {
            "source_rules": len(records),
            "recommendations": dict(sorted(decision_counts.items())),
            "model_loader": dict(sorted(loader_counts.items())),
        },
        "rules": records,
    }


def git_metadata() -> dict[str, Any]:
    try:
        revision = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=REPOSITORY_ROOT,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        dirty = bool(
            subprocess.run(
                ["git", "status", "--porcelain"],
                cwd=REPOSITORY_ROOT,
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
        )
    except (FileNotFoundError, subprocess.CalledProcessError):
        revision = os.getenv("GIT_REVISION")
        raw_dirty = os.getenv("GIT_WORKTREE_DIRTY", "unknown").lower()
        dirty = (
            True if raw_dirty == "true" else
            False if raw_dirty == "false" else
            "unknown"
        )
    return {"revision": revision, "worktree_dirty": dirty}


def run(args: argparse.Namespace) -> tuple[dict[str, Any], dict[str, Any]]:
    manifest = load_and_verify_source(args.source_dir, args.manifest)
    cases = load_corpus(args.corpus)
    scrubber = import_verified_scrubber(args.source_dir)
    gitleaks_rules, loader_skipped = scrubber.load_gitleaks_rules(args.source_dir / "gitleaks.toml")

    source_results: dict[str, tuple[Span, ...]] = {}
    current_results: dict[str, tuple[Span, ...]] = {}
    source_durations: dict[str, float] = {}
    for case in cases:
        started = time.perf_counter()
        source_results[case.case_id] = source_predictions(scrubber, case.text, gitleaks_rules)
        source_durations[case.case_id] = time.perf_counter() - started
        current_results[case.case_id] = current_predictions(case.text)

    source_metrics = calculate_metrics(cases, source_results)
    current_metrics = calculate_metrics(cases, current_results)
    per_family = {}
    for family in sorted({case.family for case in cases if case.family != "all"}):
        family_cases = tuple(case for case in cases if case.family == family)
        isolated_source_results = {
            case.case_id: tuple(
                span for span in source_results[case.case_id] if span.detector == family
            )
            for case in family_cases
        }
        per_family[family] = {
            "model_repository_family_only": calculate_metrics(
                family_cases, isolated_source_results
            ),
            "model_repository_rules": calculate_metrics(family_cases, source_results),
            "current_project_rules": calculate_metrics(family_cases, current_results),
        }

    family_detector_counts = Counter(
        span.detector
        for case_spans in source_results.values()
        for span in case_spans
    )
    report = {
        "schema_version": 1,
        "metadata": {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "source_repository": manifest["model_repository"]["repository"],
            "source_revision": manifest["model_repository"]["revision"],
            "source_verified": True,
            "corpus_path": str(args.corpus.relative_to(REPOSITORY_ROOT)),
            "corpus_sha256": sha256_file(args.corpus),
            "case_count": len(cases),
            "git": git_metadata(),
            "active_runtime_rules_changed": True,
        },
        "source_loader": {
            "loaded_gitleaks_rules": len(gitleaks_rules),
            "skipped_selected_rules": loader_skipped,
            "global_allowlist_applied": False,
            "rule_allowlists_applied": False,
            "path_constraints_applied": False,
            "label_mapping": "identifier substring: token, then key or secret",
        },
        "detector_prediction_counts": dict(sorted(family_detector_counts.items())),
        "performance": {
            "source_total_seconds": round(sum(source_durations.values()), 6),
            "source_max_case_seconds": round(max(source_durations.values()), 6),
            "source_slowest_case_id": max(source_durations, key=source_durations.get),
        },
        "aggregate": {
            "model_repository_rules": source_metrics,
            "current_project_rules": current_metrics,
        },
        "per_family": per_family,
    }
    inventory = build_inventory(args.source_dir / "gitleaks.toml", manifest)
    return report, inventory


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-dir", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST_PATH)
    parser.add_argument("--corpus", type=Path, default=DEFAULT_CORPUS_PATH)
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT_PATH)
    parser.add_argument("--inventory", type=Path, default=DEFAULT_INVENTORY_PATH)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    report, inventory = run(args)
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.inventory.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    args.inventory.write_text(json.dumps(inventory, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"report": str(args.report), "inventory": str(args.inventory)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
