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
    assert '@app.get("/metrics")' in source
    assert "generate_latest()" in source

    telemetry_block = source[source.index("def _emit_analyzer_telemetry") :]
    assert '"entity_counts"' in telemetry_block
    assert '"capacity"' in telemetry_block
    assert "request.text" not in telemetry_block
    assert 'entity.get("text")' not in telemetry_block
    assert 'entity.get("start")' not in telemetry_block
    assert 'entity.get("end")' not in telemetry_block


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
        assert "raw" in text
        assert "entity values" in text or "raw values" in text

    assert "Per-request telemetry Presidio Analyzer будет реализована в #31" not in (
        compliance
    )
