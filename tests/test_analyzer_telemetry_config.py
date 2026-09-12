"""Static checks for Presidio Analyzer request telemetry."""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_analyzer_server_exposes_safe_request_telemetry():
    source = (ROOT / "presidio" / "analyzer_server.py").read_text()

    assert "presidio_analyzer_request" in source
    assert "ru_presidio_analyzer_requests" in source
    assert "ru_presidio_analyzer_latency_seconds" in source
    assert "ru_presidio_analyzer_entities_detected" in source
    assert "ru_presidio_analyzer_capacity_rejections" in source
    assert "ru_presidio_analyzer_failures" in source
    assert "ru_presidio_analyzer_ner_failures" in source
    assert "ru_presidio_analyzer_ner_inference" in source
    assert "ru_presidio_analyzer_ner_inference_duration_seconds" in source
    assert "ru_presidio_analyzer_ner_input_tokens" in source
    assert "ru_presidio_analyzer_ner_windows_processed" in source
    assert "ru_presidio_analyzer_queue_wait_seconds" in source
    assert "ru_presidio_analyzer_phase_duration_seconds" in source
    assert "ru_presidio_analyzer_input_characters" in source
    assert "ru_presidio_analyzer_text_chunks" in source
    assert "ru_presidio_analyzer_text_chunk_characters" in source
    assert "ru_presidio_analyzer_merge_decisions" in source
    assert "presidio_ner_inference" in source
    assert "presidio_analyzer_phase" in source
    assert "presidio_ner_startup_begin" in source
    assert "presidio_ner_startup_ready" in source
    assert "presidio_ner_startup_failed" in source
    assert '@app.get("/metrics")' in source
    assert "generate_latest()" in source

    telemetry_block = source[source.index("def _emit_analyzer_telemetry") :]
    assert '"entity_counts"' in telemetry_block
    assert '"capacity"' in telemetry_block
    assert '"text": request.text' not in telemetry_block
    assert "request.text[" not in telemetry_block
    assert 'entity.get("text")' not in telemetry_block
    assert 'entity.get("start")' not in telemetry_block
    assert 'entity.get("end")' not in telemetry_block
    assert "failure_reason=type(e).__name__" not in source


def test_static_tests_include_analyzer_telemetry_gate():
    makefile = (ROOT / "Makefile").read_text()
    workflow = (ROOT / ".github" / "workflows" / "baseline.yml").read_text()

    assert "tests/test_analyzer_telemetry_config.py" in makefile
    assert "make test-static" in workflow


def test_docs_describe_analyzer_telemetry_contract():
    readme = (ROOT / "README.md").read_text()
    monitoring = (ROOT / "docs" / "monitoring.md").read_text()
    compliance = (ROOT / "docs" / "compliance.md").read_text()

    assert "docs/monitoring.md" in readme

    for text in (monitoring, compliance):
        assert "presidio_analyzer_request" in text
        assert "ru_presidio_analyzer_requests_total" in text
        assert "ru_presidio_analyzer_latency_seconds" in text
        assert "ru_presidio_analyzer_ner_failures_total" in text
        assert "ru_presidio_analyzer_ner_inference_total" in text
        assert "ru_presidio_analyzer_ner_inference_duration_seconds" in text
        assert "ru_presidio_analyzer_ner_input_tokens" in text
        assert "ru_presidio_analyzer_ner_windows_processed" in text
        assert "ru_presidio_analyzer_queue_wait_seconds" in text
        assert "ru_presidio_analyzer_phase_duration_seconds" in text
        assert "ru_presidio_analyzer_input_characters" in text
        assert "ru_presidio_analyzer_text_chunks" in text
        assert "ru_presidio_analyzer_text_chunk_characters" in text
        assert "ru_presidio_analyzer_merge_decisions_total" in text
        assert "presidio_ner_inference" in text
        assert "presidio_analyzer_phase" in text
        assert "исходн" in text
        assert "значен" in text

    assert "Per-request telemetry Presidio Analyzer будет реализована в #31" not in (
        compliance
    )


def test_analysis_cache_contract_is_observable_and_documented():
    guardrail = (ROOT / "litellm_guardrails" / "pii_guardrail.py").read_text()
    metrics = (ROOT / "litellm_guardrails" / "metrics.py").read_text()
    architecture = (ROOT / "docs" / "architecture.md").read_text()
    monitoring = (ROOT / "docs" / "monitoring.md").read_text()
    configuration = (ROOT / "docs" / "configuration.md").read_text()

    assert "LITELLM_SALT_KEY" in guardrail
    assert "pii_analysis_cache:v1:" in guardrail
    assert "analysis_signature" in guardrail
    assert "ru_pii_guardrail_analysis_cache_requests" in metrics
    assert "ru_pii_guardrail_analysis_cache_latency_seconds" in metrics
    for text in (architecture, monitoring, configuration):
        assert "HMAC" in text
        assert "исходн" in text
