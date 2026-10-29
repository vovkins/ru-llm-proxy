"""Finite owner-accepted test outcomes, separate from ground-truth quality."""

from __future__ import annotations

from functools import lru_cache
import hashlib
import json
from pathlib import Path

from presidio.evaluation.corpus import CorpusCase, EntitySpan
from presidio.evaluation.tiny2_comparison import coverage
from presidio.model_contract import MODEL_REVISION

LIMITATIONS_PATH = Path(__file__).resolve().parents[1] / "fixtures/ner-model-limitations.json"


@lru_cache(maxsize=1)
def accepted_registry():
    data = json.loads(LIMITATIONS_PATH.read_text(encoding="utf-8"))
    assert data["model_revision"] == MODEL_REVISION, "accepted limits belong to another model"
    return data["groups"]


def span_signature(results):
    spans = tuple(sorted((r["entity_type"], r["start"], r["end"]) for r in results))
    assert len(spans) == len(set(spans)), "duplicate Analyzer intervals"
    return spans


def expected_signature(case):
    return tuple(sorted((e.entity_type, e.start, e.end) for e in case.expected))


def accepted_entry(case, corpus):
    entry = accepted_registry().get(corpus, {}).get(case.case_id)
    if entry is not None:
        assert (
            hashlib.sha256(case.text.encode()).hexdigest() == entry["text_sha256"]
            and expected_signature(case) == tuple(tuple(s) for s in entry["expected"])
            and case.score_threshold == entry["score_threshold"]
            and case.language == entry["language"]
            and case.critical == entry["critical"]
            and case.tags == tuple(entry["tags"])
        ), f"{case.case_id}: accepted limitation no longer matches source annotation"
    return entry


def check_case_result(case, results, corpus):
    """Accept gold or a source-bound known result, never arbitrary predictions."""
    entry = accepted_entry(case, corpus)
    actual = span_signature(results)
    assert all(isinstance(kind, str) and type(start) is int and type(end) is int
               and 0 <= start < end <= len(case.text) for kind, start, end in actual), (
        f"{case.case_id}: invalid Analyzer interval"
    )
    gold = set(expected_signature(case))
    accepted = tuple(tuple(s) for s in entry["accepted"]) if entry else ()
    # Keeping gold and dropping known extras is an improvement, not a failure.
    extras = {s for s in accepted if all(s[2] <= e.start or s[1] >= e.end for e in case.expected)}
    correct = gold <= set(actual) <= gold | extras
    assert correct or (entry is not None and actual == accepted), (
        f"{case.case_id}: unaccepted Analyzer span/type change"
    )
    values = coverage(case, results)["values"]
    if corpus != "accepted_coverage":
        assert all(v["significant_fully_covered"] for v in values), (
            f"{case.case_id}: no complete sensitive span union"
        )
    return {
        "assessment": "matches_gold" if set(actual) == gold else "accepted_limitation",
        "expected_masked": bool(actual),
        "coverage": values,
    }


def check_login_filter(case, results, filtered):
    """Preserve the selective-filter contract plus exact approved punctuation."""
    check_case_result(case, results, "instruction_regressions")
    gold_login = {s for s in expected_signature(case) if s[0] == "LOGIN"}
    actual = span_signature(filtered)
    allowed = {(e.start, e.end) for e in case.expected}
    entry = accepted_entry(case, "instruction_regressions")
    if entry:
        allowed.update((start, end) for kind, start, end in entry["accepted"] if kind == "LOGIN")
    assert gold_login <= set(actual) and all(
        kind == "LOGIN" and type(start) is int and type(end) is int
        and 0 <= start < end <= len(case.text)
        and all(any(a <= i < b for a, b in allowed) for i in range(start, end))
        for kind, start, end in actual
    ), f"{case.case_id}: LOGIN filter changed command or credential"


def accepted_coverage_cases():
    return tuple(
        CorpusCase(case_id, row["text"], tuple(EntitySpan(*s) for s in row["expected"]),
                   row["critical"], tuple(row["tags"]), row["language"], row["score_threshold"])
        for case_id, row in accepted_registry()["accepted_coverage"].items()
    )
