"""Research-only paired comparison; never changes production acceptance gates.

Inputs carry an explicit annotation scope. HTTP/model failures are unavailable
results, not successful empty predictions. Reports omit texts and secret values.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import re
from typing import Iterable, Mapping

from .corpus import CorpusCase, EntitySpan
from .metrics import evaluate_predictions


@dataclass(frozen=True)
class ComparisonCase:
    case: CorpusCase
    corpus: str
    scope: frozenset[str]
    complete_annotation: bool = False
    origin: str = "diagnostic"

    def __post_init__(self):
        if not self.corpus or not self.scope or not self.origin:
            raise ValueError("Corpus, scope and origin are required")
        for span in self.case.expected:
            if span.entity_type not in self.scope:
                raise ValueError("Gold class is outside annotation scope")
            if not 0 <= span.start < span.end <= len(self.case.text):
                raise ValueError("Gold interval is outside source text")


def merged_intervals(spans: Iterable[tuple[int, int]]) -> list[tuple[int, int]]:
    result: list[tuple[int, int]] = []
    for start, end in sorted(spans):
        if start >= end:
            raise ValueError("Empty or inverted interval")
        if result and start <= result[-1][1]:
            result[-1] = result[-1][0], max(end, result[-1][1])
        else:
            result.append((start, end))
    return result


def intersection_length(left, right):
    left, right = merged_intervals(left), merged_intervals(right)
    i = j = total = 0
    while i < len(left) and j < len(right):
        a, b = left[i]
        c, d = right[j]
        total += max(0, min(b, d) - max(a, c))
        if b <= d:
            i += 1
        else:
            j += 1
    return total


def interval_length(spans):
    return sum(end - start for start, end in merged_intervals(spans))


def coverage(case: CorpusCase, predictions):
    masks = [(p["start"], p["end"]) for p in predictions]
    gold = [(p.start, p.end) for p in case.expected]
    values = []
    for span in case.expected:
        overlap = intersection_length([(span.start, span.end)], masks)
        same_type = [(p["start"], p["end"]) for p in predictions
                     if p["entity_type"] == span.entity_type]
        significant = [(span.start + m.start(), span.start + m.end())
                       for m in re.finditer(r"\S+", case.text[span.start:span.end])]
        significant_size = interval_length(significant)
        significant_covered = intersection_length(significant, masks)
        values.append({
            "entity_type": span.entity_type, "start": span.start, "end": span.end,
            "characters": span.end - span.start, "covered_characters": overlap,
            "fully_covered": overlap == span.end - span.start,
            "partially_covered": 0 < overlap < span.end - span.start,
            "typed_fully_covered": intersection_length([(span.start, span.end)], same_type) == span.end - span.start,
            "significant_characters": significant_size,
            "covered_significant_characters": significant_covered,
            "significant_fully_covered": significant_covered == significant_size,
            "significant_partially_covered": 0 < significant_covered < significant_size,
            "typed_significant_fully_covered": intersection_length(significant, same_type) == significant_size,
        })
    return {
        "values": values,
        "gold_characters": interval_length(gold),
        "covered_gold_characters": intersection_length(gold, masks),
        "masked_characters": interval_length(masks),
        "outside_gold_characters": interval_length(masks) - intersection_length(gold, masks),
    }


def _validate_predictions(case, predictions):
    if not isinstance(predictions, list):
        raise ValueError("Predictions must be an array")
    validated = []
    for raw in predictions:
        if not isinstance(raw, Mapping):
            raise ValueError("Prediction must be an object")
        kind, start, end = raw.get("entity_type"), raw.get("start"), raw.get("end")
        if not isinstance(kind, str) or not kind or type(start) is not int or type(end) is not int:
            raise ValueError("Invalid prediction class/coordinates")
        if not 0 <= start < end <= len(case.text):
            raise ValueError("Prediction interval is outside text")
        validated.append({"entity_type": kind, "start": start, "end": end})
    return validated


def score_results(cases: Iterable[ComparisonCase], results: Mapping[str, dict]):
    cases = tuple(cases)
    ids = [item.case.case_id for item in cases]
    if len(ids) != len(set(ids)) or set(results) - set(ids):
        raise ValueError("Duplicate cases or unknown result IDs")
    totals = Counter()
    classes: dict[str, Counter] = {}
    details = []
    for item in cases:
        case = item.case
        for kind in item.scope:
            classes.setdefault(kind, Counter())
        totals["cases"] += 1
        totals["gold_values_all_requests"] += len(case.expected)
        for span in case.expected:
            classes[span.entity_type]["gold_values_all_requests"] += 1
        result = results.get(case.case_id)
        if result is None or result.get("status") != "ok":
            totals["unavailable_cases"] += 1
            totals["gold_values_in_unavailable_cases"] += len(case.expected)
            for span in case.expected:
                classes[span.entity_type]["unavailable_gold_values"] += 1
            details.append({"case_id": case.case_id, "status": "unavailable",
                            "http_status": result.get("http_status") if result else None,
                            "error_type": result.get("error_type") if result else "MissingResult"})
            continue
        predictions = _validate_predictions(case, result.get("entities"))
        scoped = [p for p in predictions if p["entity_type"] in item.scope]
        exact = evaluate_predictions([case], {case.case_id: scoped}, entity_types=item.scope)
        for kind in item.scope:
            row = exact["per_entity"][kind]
            classes[kind].update({name: row[name] for name in
                                 ("expected", "predicted", "true_positives", "false_positives", "false_negatives")})
        covered = coverage(case, predictions)
        totals["successful_cases"] += 1
        totals["negative_cases"] += not case.expected
        totals["negative_cases_with_in_scope_mask"] += not case.expected and bool(scoped)
        totals["negative_cases_with_any_mask"] += not case.expected and bool(predictions)
        totals["cases_with_any_mask"] += bool(predictions)
        totals["gold_characters"] += covered["gold_characters"]
        totals["covered_gold_characters"] += covered["covered_gold_characters"]
        scoped_coverage = coverage(case, scoped)
        totals["in_scope_extra_masked_characters"] += scoped_coverage["outside_gold_characters"]
        totals["in_scope_clean_characters"] += len(case.text) - covered["gold_characters"]
        for value in covered["values"]:
            kind = classes[value["entity_type"]]
            for key in ("fully_covered", "partially_covered", "typed_fully_covered", "significant_fully_covered",
                        "significant_partially_covered", "typed_significant_fully_covered"):
                kind[key] += value[key]
                totals[key] += value[key]
            kind["gold_characters"] += value["characters"]
            kind["covered_gold_characters"] += value["covered_characters"]
            for key in ("significant_characters", "covered_significant_characters"):
                kind[key] += value[key]
                totals[key] += value[key]
        if item.complete_annotation:
            totals["complete_annotation_cases"] += 1
            totals["extra_masked_characters"] += covered["outside_gold_characters"]
            totals["clean_characters"] += len(case.text) - covered["gold_characters"]
            totals["false_block_cases"] += not case.expected and bool(predictions)
        else:
            totals["partial_annotation_cases"] += 1
        gold_set = {(p.entity_type, p.start, p.end) for p in case.expected}
        actual_set = {(p["entity_type"], p["start"], p["end"]) for p in scoped}
        details.append({"case_id": case.case_id, "status": "ok", "exact_match": gold_set == actual_set,
                        "coverage": covered, "predictions": predictions,
                        "complete_annotation": item.complete_annotation})
    rows = {}
    for kind, counts in sorted(classes.items()):
        row = dict(counts)
        expected, predicted, tp = (counts[n] for n in ("expected", "predicted", "true_positives"))
        row.update(precision=tp / predicted if predicted else None,
                   recall=tp / expected if expected else None,
                   f1=2 * tp / (expected + predicted) if expected + predicted else None)
        rows[kind] = row
    expected = sum(c["expected"] for c in classes.values())
    predicted = sum(c["predicted"] for c in classes.values())
    tp = sum(c["true_positives"] for c in classes.values())
    totals.update(expected=expected, predicted=predicted, true_positives=tp,
                  false_positives=predicted - tp, false_negatives=expected - tp)
    return {"counts": dict(totals), "per_entity": rows, "cases": details,
            "note": "P/R/F1 condition on successful requests; all-request gold and failures are separate. "
                    "Extra masking/false blocking is evaluated only on complete annotations. "
                    "No acceptance thresholds or changes to strict regression assertions."}


def manifest(cases: Iterable[ComparisonCase]):
    """Stable identities without publishing text or credential values."""
    result = []
    for item in cases:
        case = item.case
        result.append({"case_id": case.case_id, "corpus": item.corpus, "origin": item.origin,
                       "text_sha256": hashlib.sha256(case.text.encode()).hexdigest(),
                       "characters": len(case.text), "score_threshold": case.score_threshold,
                       "scope": sorted(item.scope), "complete_annotation": item.complete_annotation,
                       "expected": [{"entity_type": e.entity_type, "start": e.start, "end": e.end}
                                    for e in case.expected]})
    return result


def write_comparison(path: Path, cases, old, new, metadata):
    cases = tuple(cases)
    groups = sorted({item.corpus for item in cases})
    report = {"metadata": metadata, "manifest": manifest(cases), "groups": {}}
    for name in groups:
        group = [item for item in cases if item.corpus == name]
        ids = {item.case.case_id for item in group}
        report["groups"][name] = {
            "reference": score_results(group, {k: v for k, v in old.items() if k in ids}),
            "candidate": score_results(group, {k: v for k, v in new.items() if k in ids}),
        }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    return report
