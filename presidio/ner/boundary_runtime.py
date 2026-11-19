"""Evidence captured only for the guarded, request-local PERSON fallback."""

from collections import Counter
from contextvars import ContextVar
from dataclasses import dataclass, field
from threading import Lock
from typing import TYPE_CHECKING

from .boundary_options import close_cluster, partition
from .text_processing import NormalizedText, has_complete_word_edges

if TYPE_CHECKING:
    from .triton_recognizer import EntityPrediction, UnresolvedWindowEntityCluster

MAX_LEDGER = 4096
MAX_FALLBACK_SPAN_CHARACTERS = 64
METADATA_KEY = "boundary_required_spans_v1"

_COUNTS = Counter()
_LOCK = Lock()


def count(kind, rule):
    with _LOCK:
        _COUNTS[kind, rule] += 1


def metrics_snapshot():
    with _LOCK:
        return dict(_COUNTS)


def fail(rule):
    from .triton_recognizer import NERProcessingError

    count("OTHER", rule)
    raise NERProcessingError(
        phase="postprocessing", failure_class="boundary_positive_coverage_failed"
    )


@dataclass
class BoundaryEvidence:
    owner: object
    threshold: float
    requested: frozenset[str] | None
    positive: dict[tuple[int, int, str], float] = field(default_factory=dict)
    clusters: list["UnresolvedWindowEntityCluster"] = field(default_factory=list)
    policy_required: list["EntityPrediction"] = field(default_factory=list)
    normalized: NormalizedText | None = None
    overflowed: bool = False

    def record(self, finding):
        if (finding.score < self.threshold or
                self.requested and finding.entity_type not in self.requested):
            return
        key = (finding.start, finding.end, finding.entity_type)
        if key not in self.positive and len(self.positive) >= MAX_LEDGER:
            self.overflowed = True
        else:
            self.positive[key] = max(finding.score, self.positive.get(key, 0.0))

    def attach_proof(self, results):
        if not self.clusters:
            return
        if self.overflowed:
            fail("ledger_overflow")
        normalized = self.normalized
        if normalized is None:
            fail("invariant_failure")
        required = []
        for (left, right, kind), score in sorted(self.positive.items()):
            if any(
                left < p.entity.end and p.entity.start < right
                for cluster in self.clusters for p in cluster.predictions
            ):
                start, end = normalized.to_original_span(left, right)
                required.append((start, end, kind, score))
        for finding in self.policy_required:
            start, end = normalized.to_original_span(finding.start, finding.end)
            required.append((start, end, finding.entity_type, finding.score))

        cover = []
        for start, end in sorted((r.start, r.end) for r in results):
            if cover and start <= cover[-1][1]:
                cover[-1] = (cover[-1][0], max(end, cover[-1][1]))
            else:
                cover.append((start, end))
        if any(not any(a <= left < right <= b for a, b in cover)
               for left, right, _, _ in required):
            fail("invariant_failure")
        if required:
            carrier = results[0]
            carrier.recognition_metadata = {
                **(carrier.recognition_metadata or {}),
                METADATA_KEY: tuple(required),
            }


ACTIVE_EVIDENCE: ContextVar[BoundaryEvidence | None] = ContextVar("boundary_evidence", default=None)


def evidence_for(owner):
    evidence = ACTIVE_EVIDENCE.get()
    if evidence is not None and evidence.owner is owner:
        return evidence
    return None


def resolve_checked_cluster(evidence, legacy_resolve, normalized, predictions, recovery,
                            context, checked_person_cluster, **kwargs):
    # Local import avoids a cycle with the recognizer's prediction types.
    from . import triton_recognizer as nr

    if evidence.overflowed:
        fail("ledger_overflow")
    evidence.normalized = normalized
    checked_predictions = frozenset(checked_person_cluster.predictions)
    all_predictions = list(dict.fromkeys([*predictions, *recovery]))
    resolved, bounds, remaining = [], [], []

    def check():
        nr._raise_if_cancelled(kwargs.get("cancellation_event"))

    for items in partition(all_predictions):
        check()
        try:
            legacy_entities, legacy_bounds = legacy_resolve(normalized, items, [], context, **kwargs)
            resolved.extend(legacy_entities)
            bounds.extend(legacy_bounds)
            continue
        except nr.NERWindowBoundaryError:
            pass
        cluster = nr.UnresolvedWindowEntityCluster(items[0].entity.entity_type, tuple(items))
        if cluster.entity_type != "PERSON" or not checked_predictions.issuperset(items):
            remaining.append(cluster)
            continue
        closed, event = close_cluster(normalized.text, cluster, check)
        count(cluster.entity_type, event["rule"])
        if event.get("closure_capped"):
            count(cluster.entity_type, "closure_capped")
        if closed is None:
            remaining.append(cluster)
            continue
        start, end = normalized.to_original_span(closed.entity.start, closed.entity.end)
        if end - start > MAX_FALLBACK_SPAN_CHARACTERS:
            count(cluster.entity_type, "large_cluster_refused")
            remaining.append(cluster)
            continue
        if not has_complete_word_edges(context.values.text, context.offset + start, context.offset + end):
            count(cluster.entity_type, "incomplete_word_refused")
            remaining.append(cluster)
            continue
        evidence.clusters.append(cluster)
        if event["rule"] == "mixed_token":
            evidence.policy_required.append(closed.entity)
        resolved.extend(nr.merge_window_predictions(items + [closed]))
        for prediction in items:
            evidence.record(prediction.entity)
    if remaining:
        raise nr.NERWindowBoundaryError(tuple(remaining), tuple(all_predictions))
    return sorted(resolved, key=lambda e: (e.start, e.end, e.entity_type)), bounds
