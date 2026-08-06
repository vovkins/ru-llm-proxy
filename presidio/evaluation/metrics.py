"""Exact-span and masking-coverage metrics for Analyzer evaluation."""

from __future__ import annotations

from collections import Counter
from typing import Any, Iterable, Mapping

from .corpus import TARGET_ENTITY_TYPES, CorpusCase, EntitySpan


def _ratio(numerator: int, denominator: int) -> float | None:
    if denominator == 0:
        return None
    return round(numerator / denominator, 6)


def _f1(precision: float | None, recall: float | None) -> float | None:
    if precision is None or recall is None:
        return None if precision is None and recall is None else 0.0
    if precision + recall == 0:
        return 0.0
    return round(2 * precision * recall / (precision + recall), 6)


def _normalize_prediction(raw: Mapping[str, Any], *, text_length: int) -> EntitySpan:
    entity_type = raw.get("entity_type")
    start = raw.get("start")
    end = raw.get("end")
    if not isinstance(entity_type, str) or not entity_type:
        raise ValueError("prediction entity_type must be a non-empty string")
    if isinstance(start, bool) or not isinstance(start, int):
        raise ValueError("prediction start must be an integer")
    if isinstance(end, bool) or not isinstance(end, int):
        raise ValueError("prediction end must be an integer")
    if not 0 <= start < end <= text_length:
        raise ValueError("prediction span is outside the source text")
    return EntitySpan(entity_type=entity_type, start=start, end=end)


def _exact_matches(
    expected: Iterable[EntitySpan],
    predicted: Iterable[EntitySpan],
) -> tuple[int, list[EntitySpan], list[EntitySpan]]:
    remaining_expected = list(expected)
    unexpected: list[EntitySpan] = []
    true_positives = 0

    for prediction in predicted:
        try:
            match_index = remaining_expected.index(prediction)
        except ValueError:
            unexpected.append(prediction)
        else:
            true_positives += 1
            remaining_expected.pop(match_index)
    return true_positives, remaining_expected, unexpected


def _fully_covers(prediction: EntitySpan, expected: EntitySpan) -> bool:
    return prediction.start <= expected.start and prediction.end >= expected.end


def _metric_row(*, expected: int, predicted: int, true_positives: int) -> dict[str, Any]:
    precision = _ratio(true_positives, predicted)
    recall = _ratio(true_positives, expected)
    return {
        "expected": expected,
        "predicted": predicted,
        "true_positives": true_positives,
        "false_positives": predicted - true_positives,
        "false_negatives": expected - true_positives,
        "precision": precision,
        "recall": recall,
        "f1": _f1(precision, recall),
    }


def evaluate_predictions(
    cases: Iterable[CorpusCase],
    predictions_by_case: Mapping[str, Iterable[Mapping[str, Any]]],
) -> dict[str, Any]:
    """Evaluate Analyzer responses without retaining raw entity values."""
    normalized_cases = tuple(cases)
    known_case_ids = {case.case_id for case in normalized_cases}
    unknown_case_ids = set(predictions_by_case) - known_case_ids
    if unknown_case_ids:
        unknown = ", ".join(sorted(unknown_case_ids))
        raise ValueError(f"predictions contain unknown case ids: {unknown}")

    totals = Counter()
    per_entity = {entity_type: Counter() for entity_type in TARGET_ENTITY_TYPES}
    prediction_type_counts: Counter[str] = Counter()
    case_results: list[dict[str, Any]] = []
    critical_misses: list[dict[str, str]] = []

    for case in normalized_cases:
        raw_predictions = predictions_by_case.get(case.case_id, ())
        predictions = [
            _normalize_prediction(raw, text_length=len(case.text))
            for raw in raw_predictions
        ]
        prediction_type_counts.update(prediction.entity_type for prediction in predictions)
        target_predictions = [
            prediction
            for prediction in predictions
            if prediction.entity_type in TARGET_ENTITY_TYPES
        ]

        true_positives, missing, unexpected = _exact_matches(
            case.expected,
            target_predictions,
        )
        typed_covered = [
            expected
            for expected in case.expected
            if any(
                prediction.entity_type == expected.entity_type
                and _fully_covers(prediction, expected)
                for prediction in predictions
            )
        ]
        masking_covered = [
            expected
            for expected in case.expected
            if any(_fully_covers(prediction, expected) for prediction in predictions)
        ]

        totals.update(
            expected=len(case.expected),
            predicted=len(target_predictions),
            true_positives=true_positives,
            typed_covered=len(typed_covered),
            masking_covered=len(masking_covered),
        )

        for entity_type in TARGET_ENTITY_TYPES:
            expected_for_type = [
                entity for entity in case.expected if entity.entity_type == entity_type
            ]
            predicted_for_type = [
                entity for entity in target_predictions if entity.entity_type == entity_type
            ]
            exact_for_type, _, _ = _exact_matches(expected_for_type, predicted_for_type)
            typed_covered_for_type = sum(
                entity in typed_covered for entity in expected_for_type
            )
            masking_covered_for_type = sum(
                entity in masking_covered for entity in expected_for_type
            )
            per_entity[entity_type].update(
                expected=len(expected_for_type),
                predicted=len(predicted_for_type),
                true_positives=exact_for_type,
                typed_covered=typed_covered_for_type,
                masking_covered=masking_covered_for_type,
            )

        if case.critical:
            for expected in case.expected:
                totals["critical_expected"] += 1
                if expected in masking_covered:
                    totals["critical_covered"] += 1
                else:
                    critical_misses.append(
                        {
                            "case_id": case.case_id,
                            "entity_type": expected.entity_type,
                        }
                    )

        case_results.append(
            {
                "case_id": case.case_id,
                "tags": list(case.tags),
                "critical": case.critical,
                "expected": len(case.expected),
                "target_predictions": len(target_predictions),
                "all_predictions": len(predictions),
                "exact_matches": true_positives,
                "typed_covered": len(typed_covered),
                "masking_covered": len(masking_covered),
                "missing_entity_types": sorted(entity.entity_type for entity in missing),
                "unexpected_target_types": sorted(
                    entity.entity_type for entity in unexpected
                ),
                "all_prediction_types": sorted(
                    {prediction.entity_type for prediction in predictions}
                ),
            }
        )

    aggregate = _metric_row(
        expected=totals["expected"],
        predicted=totals["predicted"],
        true_positives=totals["true_positives"],
    )
    aggregate.update(
        {
            "typed_covered": totals["typed_covered"],
            "typed_coverage_recall": _ratio(
                totals["typed_covered"], totals["expected"]
            ),
            "masking_covered": totals["masking_covered"],
            "masking_coverage_recall": _ratio(
                totals["masking_covered"], totals["expected"]
            ),
            "critical_expected": totals["critical_expected"],
            "critical_covered": totals["critical_covered"],
            "critical_coverage_recall": _ratio(
                totals["critical_covered"], totals["critical_expected"]
            ),
        }
    )

    entity_metrics: dict[str, Any] = {}
    for entity_type in sorted(TARGET_ENTITY_TYPES):
        counts = per_entity[entity_type]
        row = _metric_row(
            expected=counts["expected"],
            predicted=counts["predicted"],
            true_positives=counts["true_positives"],
        )
        row.update(
            {
                "typed_covered": counts["typed_covered"],
                "typed_coverage_recall": _ratio(
                    counts["typed_covered"], counts["expected"]
                ),
                "masking_covered": counts["masking_covered"],
                "masking_coverage_recall": _ratio(
                    counts["masking_covered"], counts["expected"]
                ),
            }
        )
        entity_metrics[entity_type] = row

    return {
        "aggregate": aggregate,
        "per_entity": entity_metrics,
        "prediction_type_counts": dict(sorted(prediction_type_counts.items())),
        "critical_misses": critical_misses,
        "cases": case_results,
    }
