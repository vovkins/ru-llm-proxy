"""Opt-in bundle-based late fallback with bounded request-local evidence."""

from contextvars import ContextVar
from collections import Counter
from threading import Lock
from .boundary_options import partition, close_cluster
from .text_processing import has_complete_word_edges

MAX_LEDGER = 4096
MAX_FALLBACK_SPAN_CHARACTERS = 64
METADATA_KEY = "boundary_required_spans_v1"
_LOCK = Lock()
_COUNTS = Counter()
_RULES = frozenset(
    [
        "delimiter",
        "directional_names",
        "mixed_token",
        "mixed_token_refused",
        "closure_capped",
        "invariant_failure",
        "ledger_overflow",
        "legacy_resolved",
        "disabled",
        "large_cluster_refused",
        "incomplete_word_refused",
    ]
)


def count(kind, rule):
    key = (
        (
            kind
            if kind
            in [
                "PERSON",
                "LOCATION",
                "ORGANIZATION",
                "LOGIN",
                "PASSWORD",
                "AUTH_TOKEN",
                "SECRET_KEY",
                "CONTRACT_NUMBER",
            ]
            else "OTHER"
        ),
        rule if rule in _RULES else "OTHER",
    )
    with _LOCK:
        _COUNTS[key] += 1


def metrics_snapshot():
    with _LOCK:
        return dict(_COUNTS)


def install(cls):
    from . import triton_recognizer as nr

    init0 = cls.__init__
    analyze0 = cls._analyze_required
    infer0 = cls._infer_window_batch
    resolve0 = cls._resolve_boundary_values
    nr.NER_FAILURE_CLASSES = nr.NER_FAILURE_CLASSES | {"boundary_positive_coverage_failed"}
    nr.NER_REQUEST_SCOPED_FAILURE_CLASSES = nr.NER_REQUEST_SCOPED_FAILURE_CLASSES | {
        "boundary_positive_coverage_failed"
    }

    def fail(rule):
        count("OTHER", rule)
        raise nr.NERProcessingError(
            phase="postprocessing", failure_class="boundary_positive_coverage_failed"
        )

    def init(self, *args, **kwargs):
        self._boundary_state = ContextVar("boundary_evidence", default=None)
        init0(self, *args, **kwargs)
        self.boundary_mode = getattr(self._profile, "boundary_fallback_mode", "off")
        if self.boundary_mode not in ["off", "guarded"]:
            raise ValueError("Unknown boundary processing bundle")

    def infer(self, *args, **kwargs):
        result = infer0(self, *args, **kwargs)
        state = self._boundary_state.get()
        if state is None:
            return result
        for tokens in result:
            for e in nr.decode_bio_predictions(tokens):
                if (
                    e.score < state["threshold"]
                    or state["requested"]
                    and e.entity_type not in state["requested"]
                ):
                    continue
                key = (e.start, e.end, e.entity_type)
                if key not in state["positive"] and len(state["positive"]) >= MAX_LEDGER:
                    state["overflow"] = True
                    continue
                state["positive"][key] = max(e.score, state["positive"].get(key, 0.0))
        return result

    def resolve(
        self, normalized, predictions, recovery, context, *, checked_person_cluster=None, **kwargs
    ):
        try:
            return resolve0(self, normalized, predictions, recovery, context, **kwargs)
        except nr.NERWindowBoundaryError:
            state = self._boundary_state.get()
            if (
                state is None
                or not kwargs.get("allow_typed_fields")
                or checked_person_cluster is None
            ):
                raise
            checked_predictions = frozenset(checked_person_cluster.predictions)
            if state["overflow"]:
                fail("ledger_overflow")
            state["norm"] = normalized
            all_predictions = list(dict.fromkeys([*predictions, *recovery]))
            resolved = []
            bounds = []
            remaining = []
            for items in partition(all_predictions):
                nr._raise_if_cancelled(kwargs.get("cancellation_event"))
                try:
                    legacy_entities, legacy_bounds = resolve0(self, normalized, items, [], context, **kwargs)
                    resolved.extend(legacy_entities)
                    bounds.extend(legacy_bounds)
                    continue
                except nr.NERWindowBoundaryError:
                    pass
                c = nr.UnresolvedWindowEntityCluster(items[0].entity.entity_type, tuple(items))
                # Only the failed PERSON cluster observed by a complete control
                # window qualifies. The legacy resolver above still handles other clusters.
                if c.entity_type != "PERSON" or not checked_predictions.issuperset(items):
                    remaining.append(c)
                    continue
                closed, event = close_cluster(
                    normalized.text,
                    c,
                    lambda: nr._raise_if_cancelled(kwargs.get("cancellation_event")),
                )
                count(c.entity_type, event["rule"])
                if event.get("closure_capped"):
                    count(c.entity_type, "closure_capped")
                if closed is None:
                    remaining.append(c)
                    continue
                start, end = normalized.to_original_span(closed.entity.start, closed.entity.end)
                if end - start > MAX_FALLBACK_SPAN_CHARACTERS:
                    count(c.entity_type, "large_cluster_refused")
                    remaining.append(c)
                    continue
                if not has_complete_word_edges(
                    context.values.text, context.offset + start, context.offset + end
                ):
                    count(c.entity_type, "incomplete_word_refused")
                    remaining.append(c)
                    continue
                state["clusters"].append(c)
                if event["rule"] == "mixed_token":
                    state["policy_required"].append(
                        (
                            closed.entity.start,
                            closed.entity.end,
                            closed.entity.entity_type,
                            closed.entity.score,
                        )
                    )
                resolved.extend(nr.merge_window_predictions(items + [closed]))
                # Direct component adapters can return WEP without invoking token infer.
                # Keep their original positive envelopes in the coverage contract as well.
                for p in items:
                    e = p.entity
                    if e.score >= state["threshold"] and (
                        not state["requested"] or e.entity_type in state["requested"]
                    ):
                        key = (e.start, e.end, e.entity_type)
                        if key not in state["positive"] and len(state["positive"]) >= MAX_LEDGER:
                            state["overflow"] = True
                        else:
                            state["positive"][key] = max(e.score, state["positive"].get(key, 0.0))
            if remaining:
                raise nr.NERWindowBoundaryError(tuple(remaining), tuple(all_predictions))
            return sorted(resolved, key=lambda e: (e.start, e.end, e.entity_type)), bounds

    def analyze(self, text, **kwargs):
        if self.boundary_mode == "off":
            return analyze0(self, text, **kwargs)
        context = kwargs.get("boundary_context")
        requested = getattr(
            context, "requested_entities", None
        ) or nr._normalize_requested_entities(kwargs.get("entities"))
        state = dict(
            positive={},
            clusters=[],
            policy_required=[],
            overflow=False,
            norm=None,
            threshold=kwargs["score_threshold"],
            requested=requested,
        )
        token = self._boundary_state.set(state)
        try:
            result = analyze0(self, text, **kwargs)
            if not state["clusters"]:
                return result
            if state["overflow"]:
                fail("ledger_overflow")
            required = []
            norm = state["norm"]
            for (a, b, k), score in sorted(state["positive"].items()):
                if not any(
                    any(a < p.entity.end and p.entity.start < b for p in c.predictions)
                    for c in state["clusters"]
                ):
                    continue
                x, y = norm.to_original_span(a, b)
                required.append((x, y, k, score))
            for a, b, k, score in state["policy_required"]:
                x, y = norm.to_original_span(a, b)
                required.append((x, y, k, score))
            cover = sorted((r.start, r.end) for r in result[0])
            merged = []
            for a, b in cover:
                if merged and a <= merged[-1][1]:
                    merged[-1] = (merged[-1][0], max(b, merged[-1][1]))
                else:
                    merged.append((a, b))
            if any(not any(x <= a < b <= y for x, y in merged) for a, b, k, s in required):
                fail("invariant_failure")
            if required:
                if not result[0]:
                    fail("invariant_failure")
                carrier = result[0][0]
                carrier.recognition_metadata = dict(
                    carrier.recognition_metadata or {}, **{METADATA_KEY: tuple(required)}
                )
            return result
        finally:
            self._boundary_state.reset(token)

    cls.__init__ = init
    cls._infer_window_batch = infer
    cls._resolve_boundary_values = resolve
    cls._analyze_required = analyze
