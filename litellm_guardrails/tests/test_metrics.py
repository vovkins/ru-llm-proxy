"""Regression tests for process-wide PII guardrail Prometheus metrics."""

from pathlib import Path

import pytest
from litellm.proxy.types_utils.utils import get_instance_fn
from prometheus_client import CollectorRegistry, Counter, REGISTRY

from litellm_guardrails.metrics import _build_metric


ROOT = Path(__file__).resolve().parents[2]
GUARDRAIL_CLASS = "litellm_guardrails.pii_guardrail.RuPIIGuardrail"


def _load_guardrail_like_litellm():
    return get_instance_fn(
        GUARDRAIL_CLASS,
        config_file_path=str(ROOT / "litellm-config.yaml"),
    )


def _sample_value(name, labels=None):
    return REGISTRY.get_sample_value(name, labels or {}) or 0.0


def test_dynamic_guardrail_loads_share_registered_metrics(caplog):
    first_class = _load_guardrail_like_litellm()
    second_class = _load_guardrail_like_litellm()
    first_globals = first_class.__init__.__globals__
    second_globals = second_class.__init__.__globals__

    metric_names = (
        "PII_PRE_CALLS",
        "PII_POST_CALLS",
        "PII_ANALYZER_LATENCY",
        "PII_ANALYSIS_CACHE_REQUESTS",
        "PII_ANALYSIS_CACHE_LATENCY",
    )
    for name in metric_names:
        assert first_globals[name] is second_globals[name]
        assert first_globals[name].__class__.__name__ != "_NoopMetric"

    pre_labels = {"result": "clean"}
    post_labels = {"result": "restored"}
    pre_before = _sample_value("ru_pii_guardrail_pre_calls_total", pre_labels)
    post_before = _sample_value("ru_pii_guardrail_post_calls_total", post_labels)
    latency_before = _sample_value(
        "ru_pii_guardrail_analyzer_latency_seconds_count"
    )
    cache_hit_labels = {"result": "hit"}
    cache_latency_labels = {"operation": "load"}
    cache_hit_before = _sample_value(
        "ru_pii_guardrail_analysis_cache_requests_total",
        cache_hit_labels,
    )
    cache_latency_before = _sample_value(
        "ru_pii_guardrail_analysis_cache_latency_seconds_count",
        cache_latency_labels,
    )

    first_globals["PII_PRE_CALLS"].labels(**pre_labels).inc()
    second_globals["PII_POST_CALLS"].labels(**post_labels).inc()
    first_globals["PII_ANALYZER_LATENCY"].observe(0.01)
    first_globals["PII_ANALYSIS_CACHE_REQUESTS"].labels(
        **cache_hit_labels
    ).inc()
    second_globals["PII_ANALYSIS_CACHE_LATENCY"].labels(
        **cache_latency_labels
    ).observe(0.001)

    assert _sample_value(
        "ru_pii_guardrail_pre_calls_total", pre_labels
    ) == pytest.approx(pre_before + 1)
    assert _sample_value(
        "ru_pii_guardrail_post_calls_total", post_labels
    ) == pytest.approx(post_before + 1)
    assert _sample_value(
        "ru_pii_guardrail_analyzer_latency_seconds_count"
    ) == pytest.approx(latency_before + 1)
    assert _sample_value(
        "ru_pii_guardrail_analysis_cache_requests_total",
        cache_hit_labels,
    ) == pytest.approx(cache_hit_before + 1)
    assert _sample_value(
        "ru_pii_guardrail_analysis_cache_latency_seconds_count",
        cache_latency_labels,
    ) == pytest.approx(cache_latency_before + 1)
    assert "already registered, using no-op" not in caplog.text


def test_metric_registration_conflict_is_not_silenced(caplog):
    registry = CollectorRegistry()
    _build_metric(
        Counter,
        "ru_pii_guardrail_registration_test",
        "Registration conflict test.",
        registry=registry,
    )

    with pytest.raises(ValueError, match="Duplicated timeseries"):
        _build_metric(
            Counter,
            "ru_pii_guardrail_registration_test",
            "Registration conflict test.",
            registry=registry,
        )

    assert "Prometheus metric registration conflict" in caplog.text


def test_package_level_guardrail_export_remains_available():
    from litellm_guardrails import RuPIIGuardrail

    assert RuPIIGuardrail.__name__ == "RuPIIGuardrail"
