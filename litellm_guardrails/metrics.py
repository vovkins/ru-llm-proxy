"""Process-wide Prometheus metrics shared by all PII guardrail instances."""

import logging


logger = logging.getLogger(__name__)

try:
    from prometheus_client import Counter, Histogram
except Exception:  # pragma: no cover - lightweight environments may omit metrics.
    Counter = None
    Histogram = None


class _NoopMetric:
    """Fallback metric used only when prometheus_client is unavailable."""

    def labels(self, *args, **kwargs):
        return self

    def inc(self, amount: float = 1):
        return None

    def observe(self, amount: float):
        return None


def _build_metric(factory, *args, **kwargs):
    """Register a metric once or fail instead of silently losing telemetry."""
    if factory is None:
        logger.warning(
            "Prometheus client unavailable, metric disabled: metric=%s",
            args[0],
        )
        return _NoopMetric()
    try:
        return factory(*args, **kwargs)
    except ValueError:
        logger.critical(
            "Prometheus metric registration conflict: metric=%s",
            args[0],
        )
        raise


PII_PRE_CALLS = _build_metric(
    Counter,
    "ru_pii_guardrail_pre_calls",
    "PII guardrail pre-call requests.",
    ["result"],
)
PII_POST_CALLS = _build_metric(
    Counter,
    "ru_pii_guardrail_post_calls",
    "PII guardrail post-call requests.",
    ["result"],
)
PII_ENTITIES_DETECTED = _build_metric(
    Counter,
    "ru_pii_guardrail_entities_detected",
    "PII entities masked by entity type.",
    ["entity_type"],
)
PII_FAIL_OPEN = _build_metric(
    Counter,
    "ru_pii_guardrail_fail_open",
    "PII guardrail failures handled in fail-open mode.",
    ["operation"],
)
PII_FAIL_CLOSED = _build_metric(
    Counter,
    "ru_pii_guardrail_fail_closed",
    "PII guardrail failures handled in fail-closed mode.",
    ["operation"],
)
PII_ANALYZER_LATENCY = _build_metric(
    Histogram,
    "ru_pii_guardrail_analyzer_latency_seconds",
    "Latency of Presidio Analyzer calls made by the PII guardrail.",
    buckets=(0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30),
)
PII_REDIS_LATENCY = _build_metric(
    Histogram,
    "ru_pii_guardrail_redis_latency_seconds",
    "Latency of Redis operations made by the PII guardrail.",
    ["operation"],
    buckets=(0.001, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5),
)
PII_MAPPING_SIZE = _build_metric(
    Histogram,
    "ru_pii_guardrail_mapping_size",
    "Number of placeholder mappings saved for a masked request.",
    buckets=(1, 2, 5, 10, 25, 50, 100, 250),
)
PII_BLOCKED_ENTITIES = _build_metric(
    Counter,
    "ru_pii_guardrail_blocked",
    "PII entities blocked by entity type.",
    ["entity_type"],
)
PRE_EGRESS_POLICY_BLOCKED = _build_metric(
    Counter,
    "ru_pre_egress_policy_blocked",
    "Pre-egress policy blocks by category.",
    ["category"],
)
FINAL_PAYLOAD_LEAK_CHECK_BLOCKED = _build_metric(
    Counter,
    "ru_final_payload_leak_check_blocked",
    "Final provider-bound payload leak-check blocks by rule id.",
    ["rule_id"],
)
REGULATED_TOPIC_POLICY_BLOCKED = _build_metric(
    Counter,
    "ru_regulated_topic_policy_blocked",
    "Regulated-topic policy blocks by bounded category and rule id.",
    ["category", "rule_id"],
)
SYNTHETIC_PII_ALLOWLIST_HITS = _build_metric(
    Counter,
    "ru_synthetic_pii_allowlist_hits",
    "Synthetic/test PII allowlist hits by bounded rule id and entity type.",
    ["rule_id", "entity_type"],
)
DICTIONARY_SUBSTITUTIONS_APPLIED = _build_metric(
    Counter,
    "ru_dictionary_substitution_applied",
    "Dictionary substitutions applied by bounded rule id.",
    ["rule_id"],
)
DICTIONARY_SUBSTITUTION_MAPPING_SIZE = _build_metric(
    Histogram,
    "ru_dictionary_substitution_mapping_size",
    "Number of reversible dictionary mappings saved for a substituted request.",
    buckets=(1, 2, 5, 10, 25, 50, 100, 250),
)


__all__ = [
    "DICTIONARY_SUBSTITUTION_MAPPING_SIZE",
    "DICTIONARY_SUBSTITUTIONS_APPLIED",
    "FINAL_PAYLOAD_LEAK_CHECK_BLOCKED",
    "PII_ANALYZER_LATENCY",
    "PII_BLOCKED_ENTITIES",
    "PII_ENTITIES_DETECTED",
    "PII_FAIL_CLOSED",
    "PII_FAIL_OPEN",
    "PII_MAPPING_SIZE",
    "PII_POST_CALLS",
    "PII_PRE_CALLS",
    "PII_REDIS_LATENCY",
    "PRE_EGRESS_POLICY_BLOCKED",
    "REGULATED_TOPIC_POLICY_BLOCKED",
    "SYNTHETIC_PII_ALLOWLIST_HITS",
]
