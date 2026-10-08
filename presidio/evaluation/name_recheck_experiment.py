"""Offline casing experiment; never imported by the production Analyzer."""

from __future__ import annotations

import argparse
import json
import re
import statistics
import time
from dataclasses import dataclass
from pathlib import Path

from .corpus import DEFAULT_CORPUS_PATH, load_corpus
from .metrics import evaluate_predictions

_UPPERCASE_NAME_RE = re.compile(r"[А-ЯЁ]+(?:[-\s][А-ЯЁ]+){1,2}")
NAME_TYPES = frozenset({"PERSON", "ORGANIZATION"})
MAX_CANDIDATE_CHARS = 96
CONTEXT_CHARS = 48


@dataclass(frozen=True)
class RecheckWindow:
    text: str
    offset: int
    candidate_start: int
    candidate_end: int


def prepare_window(
    text: str, start: int, end: int, *, recase: bool
) -> RecheckWindow | None:
    """Keep offsets stable and reject partial words and expanding conversions."""
    if not 0 <= start < end <= len(text) or end - start > MAX_CANDIDATE_CHARS:
        return None
    value = text[start:end]
    if not _UPPERCASE_NAME_RE.fullmatch(value):
        return None
    if (start and text[start - 1].isalnum()) or (
        end < len(text) and text[end].isalnum()
    ):
        return None
    left, right = max(0, start - CONTEXT_CHARS), min(len(text), end + CONTEXT_CHARS)
    while left < start and left and text[left - 1].isalnum() and text[left].isalnum():
        left += 1
    while (
        right > end
        and right < len(text)
        and text[right - 1].isalnum()
        and text[right].isalnum()
    ):
        right -= 1
    replacement = value.title() if recase else value
    if len(replacement) != len(value):
        return None
    return RecheckWindow(
        text[left:start] + replacement + text[end:right], left, start - left, end - left
    )


def run_experiment(*, repetitions: int = 5) -> dict:
    # Import only on explicit execution; no production callback or setting.
    from ..ner.huggingface_recognizer import HuggingFaceNERRecognizer, MODEL_REVISION

    recognizer = HuggingFaceNERRecognizer()
    recognizer.load_model()
    selected = (
        case
        for filename in (
            "name_context_regressions.jsonl",
            "person_organization_regressions.jsonl",
        )
        for case in load_corpus(
            DEFAULT_CORPUS_PATH.with_name(filename), required_entity_types=NAME_TYPES
        )
        if "uppercase" in case.tags
        or "experiment" in case.tags
        or "person_field" in case.tags
    )
    by_text = {}
    for case in selected:
        by_text.setdefault(case.text, case)
    cases = tuple(by_text.values())
    baseline = {}
    candidates = {}
    rows = []
    for case in cases:
        original = recognizer.analyze(case.text, score_threshold=case.score_threshold)
        baseline[case.case_id] = [r.to_dict() for r in original]
        candidates[case.case_id] = list(baseline[case.case_id])
        for index, result in enumerate(original):
            if result.entity_type not in NAME_TYPES:
                continue
            window = prepare_window(case.text, result.start, result.end, recase=True)
            if window is None:
                continue
            row = {
                "case_id": case.case_id,
                "holdout": "holdout" in case.tags,
                "original_type": result.entity_type,
                "original_score": result.score,
                "context_chars": len(window.text),
                "variants": {},
            }
            for recase, variant in (
                (False, "bounded_original"),
                (True, "bounded_titlecase"),
            ):
                prepared = prepare_window(
                    case.text, result.start, result.end, recase=recase
                )
                durations = []
                for _ in range(repetitions):
                    started = time.perf_counter()
                    repeated = recognizer.analyze(
                        prepared.text, score_threshold=case.score_threshold
                    )
                    durations.append(time.perf_counter() - started)
                exact = [
                    r
                    for r in repeated
                    if r.entity_type in NAME_TYPES
                    and r.start == prepared.candidate_start
                    and r.end == prepared.candidate_end
                ]
                row["variants"][variant] = {
                    "matching_types": [r.entity_type for r in exact],
                    "median_ms": round(statistics.median(durations) * 1000, 3),
                    "score": exact[0].score if len(exact) == 1 else None,
                }
                if recase and len(exact) == 1:
                    updated = dict(candidates[case.case_id][index])
                    updated["entity_type"] = exact[0].entity_type
                    updated["score"] = min(result.score, exact[0].score)
                    candidates[case.case_id][index] = updated
            rows.append(row)
    return {
        "production_enabled": False,
        "revision": MODEL_REVISION,
        "repetitions": repetitions,
        "cases": len(cases),
        "rechecks": rows,
        "baseline_metrics": evaluate_predictions(cases, baseline),
        "simulated_metrics": evaluate_predictions(cases, candidates),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    report = run_experiment()
    args.report.write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(
        json.dumps(
            {
                "production_enabled": False,
                "cases": report["cases"],
                "rechecks": len(report["rechecks"]),
            }
        )
    )


if __name__ == "__main__":
    main()
