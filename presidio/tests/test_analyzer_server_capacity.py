"""Synthetic concurrency checks for Analyzer endpoint backpressure."""

import asyncio
import json
import logging
import sys
import threading
import time
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("presidio_analyzer")

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi import HTTPException

from capacity import AnalyzerCapacityLimiter
from presidio import analyzer_server


def _set_ner_state(
    monkeypatch,
    *,
    state: str,
    loaded: bool,
    warmed_up: bool,
    failure_phase: str | None = None,
    failure_class: str | None = None,
):
    monkeypatch.setattr(analyzer_server.ner_recognizer, "state", lambda: state)
    monkeypatch.setattr(
        analyzer_server.ner_recognizer,
        "is_loaded",
        lambda: loaded,
    )
    monkeypatch.setattr(
        analyzer_server.ner_recognizer,
        "is_warmed_up",
        lambda: warmed_up,
    )
    monkeypatch.setattr(
        analyzer_server.ner_recognizer,
        "failure_phase",
        lambda: failure_phase,
    )
    monkeypatch.setattr(
        analyzer_server.ner_recognizer,
        "failure_class",
        lambda: failure_class,
    )


def _json_log_events(caplog, event):
    events = []
    for record in caplog.records:
        try:
            payload = json.loads(record.getMessage())
        except json.JSONDecodeError:
            continue
        if payload.get("event") == event:
            events.append(payload)
    return events


class _RecordingMetric:
    def __init__(self):
        self.label_values = []
        self.increments = []
        self.observations = []

    def labels(self, **labels):
        self.label_values.append(labels)
        return self

    def inc(self, amount=1):
        self.increments.append(amount)

    def observe(self, amount):
        self.observations.append(amount)


class _BrokenMetric:
    def labels(self, **_labels):
        raise RuntimeError("metrics backend unavailable")


def test_analyze_rejects_concurrent_request_when_queue_is_full(monkeypatch):
    asyncio.run(_analyze_rejects_concurrent_request_when_queue_is_full(monkeypatch))


async def _analyze_rejects_concurrent_request_when_queue_is_full(monkeypatch):
    limiter = AnalyzerCapacityLimiter(
        concurrency_limit=1,
        queue_limit=0,
        queue_timeout_seconds=0.01,
    )
    monkeypatch.setattr(analyzer_server, "capacity_limiter", limiter)

    def slow_analyze_sync(request):
        time.sleep(0.05)
        return analyzer_server.AnalyzeResponse(text=request.text, entities=[])

    monkeypatch.setattr(analyzer_server, "_analyze_sync", slow_analyze_sync)

    request = analyzer_server.AnalyzeRequest(text="Иван Иванов")
    first = asyncio.create_task(analyzer_server.analyze(request))
    try:
        for _ in range(100):
            if limiter.snapshot()["active"] == 1:
                break
            await asyncio.sleep(0.001)

        with pytest.raises(HTTPException) as exc_info:
            await analyzer_server.analyze(request)

        assert exc_info.value.status_code == 503
        assert exc_info.value.detail["code"] == "analyzer_overloaded"
        assert exc_info.value.detail["reason"] == "queue_full"

        response = await first
        assert response.entities == []
    finally:
        if not first.done():
            first.cancel()


def test_analyze_emits_safe_success_telemetry(monkeypatch, caplog):
    asyncio.run(_analyze_emits_safe_success_telemetry(monkeypatch, caplog))


async def _analyze_emits_safe_success_telemetry(monkeypatch, caplog):
    limiter = AnalyzerCapacityLimiter(
        concurrency_limit=1,
        queue_limit=0,
        queue_timeout_seconds=0.01,
    )
    monkeypatch.setattr(analyzer_server, "capacity_limiter", limiter)
    monkeypatch.setattr(analyzer_server.ner_recognizer, "is_loaded", lambda: True)
    monkeypatch.setattr(analyzer_server.ner_recognizer, "is_warmed_up", lambda: True)

    async def analyze_with_phone(request):
        return analyzer_server.AnalyzeResponse(
            text=request.text,
            entities=[
                {
                    "entity_type": "PHONE_NUMBER",
                    "start": 9,
                    "end": 21,
                    "score": 1.0,
                    "text": "+79031234567",
                }
            ],
        )

    monkeypatch.setattr(analyzer_server, "_run_blocking_analyze", analyze_with_phone)
    request = analyzer_server.AnalyzeRequest(text="Телефон: +79031234567")

    with caplog.at_level(logging.INFO, logger="presidio.analyzer_server"):
        response = await analyzer_server.analyze(request)

    assert response.entities[0]["text"] == "+79031234567"
    events = _json_log_events(caplog, "presidio_analyzer_request")
    assert len(events) == 1
    event = events[0]
    assert event["outcome"] == "success"
    assert event["entity_count"] == 1
    assert event["entity_counts"] == {"PHONE_NUMBER": 1}
    assert event["language"] == "ru"
    assert event["score_threshold"] == 0.35
    assert event["ner"] == "loaded"
    assert event["latency_ms"] >= 0
    assert event["capacity"]["concurrency_limit"] == 1
    assert "event_id" in event

    logs = "\n".join(record.getMessage() for record in caplog.records)
    assert "+79031234567" not in logs
    assert "Телефон" not in logs
    assert "start" not in logs
    assert "end" not in logs


def test_analyze_emits_safe_no_entities_telemetry(monkeypatch, caplog):
    asyncio.run(_analyze_emits_safe_no_entities_telemetry(monkeypatch, caplog))


async def _analyze_emits_safe_no_entities_telemetry(monkeypatch, caplog):
    limiter = AnalyzerCapacityLimiter(
        concurrency_limit=1,
        queue_limit=0,
        queue_timeout_seconds=0.01,
    )
    monkeypatch.setattr(analyzer_server, "capacity_limiter", limiter)
    monkeypatch.setattr(analyzer_server.ner_recognizer, "is_loaded", lambda: False)

    async def analyze_clean(request):
        return analyzer_server.AnalyzeResponse(text=request.text, entities=[])

    monkeypatch.setattr(analyzer_server, "_run_blocking_analyze", analyze_clean)

    with caplog.at_level(logging.INFO, logger="presidio.analyzer_server"):
        response = await analyzer_server.analyze(
            analyzer_server.AnalyzeRequest(text="Обычный текст"),
        )

    assert response.entities == []
    events = _json_log_events(caplog, "presidio_analyzer_request")
    assert len(events) == 1
    event = events[0]
    assert event["outcome"] == "no_entities"
    assert event["entity_count"] == 0
    assert event["entity_counts"] == {}
    assert event["ner"] == "not_loaded"
    assert "failure_reason" not in event
    assert "Обычный текст" not in "\n".join(
        record.getMessage() for record in caplog.records
    )


def test_analyze_emits_safe_overload_telemetry(monkeypatch, caplog):
    asyncio.run(_analyze_emits_safe_overload_telemetry(monkeypatch, caplog))


async def _analyze_emits_safe_overload_telemetry(monkeypatch, caplog):
    limiter = AnalyzerCapacityLimiter(
        concurrency_limit=1,
        queue_limit=0,
        queue_timeout_seconds=0.01,
    )
    monkeypatch.setattr(analyzer_server, "capacity_limiter", limiter)
    slot = await limiter.acquire()
    try:
        with caplog.at_level(logging.INFO, logger="presidio.analyzer_server"):
            with pytest.raises(HTTPException) as exc_info:
                await analyzer_server.analyze(
                    analyzer_server.AnalyzeRequest(text="Телефон: +79031234567"),
                )
    finally:
        await slot.release()

    assert exc_info.value.status_code == 503
    events = _json_log_events(caplog, "presidio_analyzer_request")
    assert len(events) == 1
    event = events[0]
    assert event["outcome"] == "overload"
    assert event["failure_reason"] == "queue_full"
    assert event["entity_count"] == 0
    assert event["capacity"]["active"] == 1
    assert "+79031234567" not in "\n".join(
        record.getMessage() for record in caplog.records
    )


def test_analyze_emits_safe_error_telemetry(monkeypatch, caplog):
    asyncio.run(_analyze_emits_safe_error_telemetry(monkeypatch, caplog))


async def _analyze_emits_safe_error_telemetry(monkeypatch, caplog):
    limiter = AnalyzerCapacityLimiter(
        concurrency_limit=1,
        queue_limit=0,
        queue_timeout_seconds=0.01,
    )
    monkeypatch.setattr(analyzer_server, "capacity_limiter", limiter)

    async def analyze_error(_request):
        raise RuntimeError("raw +79031234567 should not be logged")

    monkeypatch.setattr(analyzer_server, "_run_blocking_analyze", analyze_error)

    with caplog.at_level(logging.INFO, logger="presidio.analyzer_server"):
        with pytest.raises(RuntimeError):
            await analyzer_server.analyze(
                analyzer_server.AnalyzeRequest(text="Телефон: +79031234567"),
            )

    events = _json_log_events(caplog, "presidio_analyzer_request")
    assert len(events) == 1
    event = events[0]
    assert event["outcome"] == "analyzer_error"
    assert event["failure_reason"] == "internal_error"
    assert event["entity_count"] == 0
    logs = "\n".join(record.getMessage() for record in caplog.records)
    assert "+79031234567" not in logs
    assert "raw" not in logs


def test_metrics_endpoint_exposes_analyzer_metrics(monkeypatch):
    asyncio.run(_metrics_endpoint_exposes_analyzer_metrics(monkeypatch))


async def _metrics_endpoint_exposes_analyzer_metrics(monkeypatch):
    limiter = AnalyzerCapacityLimiter(
        concurrency_limit=1,
        queue_limit=0,
        queue_timeout_seconds=0.01,
    )
    monkeypatch.setattr(analyzer_server, "capacity_limiter", limiter)

    async def analyze_with_email(request):
        return analyzer_server.AnalyzeResponse(
            text=request.text,
            entities=[
                {
                    "entity_type": "EMAIL_ADDRESS",
                    "start": 0,
                    "end": 16,
                    "score": 1.0,
                    "text": "test@example.com",
                }
            ],
        )

    monkeypatch.setattr(analyzer_server, "_run_blocking_analyze", analyze_with_email)

    await analyzer_server.analyze(
        analyzer_server.AnalyzeRequest(text="test@example.com"),
    )
    analyzer_server._record_merge_metrics(
        (
            analyzer_server.MergeDecision(
                reason="overlap_preferred_source",
                winner_source="native_credential",
                loser_source="structural",
            ),
        )
    )
    analyzer_server._record_ner_inference(
        analyzer_server.NERInferenceTelemetry(
            outcome="success",
            duration_seconds=0.01,
            windows_processed=1,
        )
    )
    response = await analyzer_server.metrics()
    body = response.body.decode()

    assert response.status_code == 200
    assert "ru_presidio_analyzer_requests_total" in body
    assert 'outcome="success"' in body
    assert "ru_presidio_analyzer_latency_seconds_bucket" in body
    assert "ru_presidio_analyzer_entities_detected_total" in body
    assert "ru_presidio_analyzer_merge_decisions_total" in body
    assert "ru_presidio_analyzer_ner_inference_total" in body
    assert "ru_presidio_analyzer_ner_inference_duration_seconds_bucket" in body
    assert "ru_presidio_analyzer_ner_windows_processed_bucket" in body
    assert 'reason="overlap_preferred_source"' in body
    assert 'winner_source="native_credential"' in body
    assert 'entity_type="EMAIL_ADDRESS"' in body
    assert "test@example.com" not in body


def test_record_ner_inference_emits_bounded_metrics_and_safe_log(
    monkeypatch,
    caplog,
):
    inference = _RecordingMetric()
    latency = _RecordingMetric()
    windows = _RecordingMetric()
    failures = _RecordingMetric()
    monkeypatch.setattr(analyzer_server, "ANALYZER_NER_INFERENCE", inference)
    monkeypatch.setattr(
        analyzer_server,
        "ANALYZER_NER_INFERENCE_LATENCY",
        latency,
    )
    monkeypatch.setattr(analyzer_server, "ANALYZER_NER_WINDOWS", windows)
    monkeypatch.setattr(analyzer_server, "ANALYZER_NER_FAILURES", failures)

    telemetry = analyzer_server.NERInferenceTelemetry(
        outcome="failure",
        duration_seconds=0.125,
        windows_processed=2,
        failure_phase="inference",
        failure_class="forward_pass_failed",
    )
    with caplog.at_level(logging.INFO, logger="presidio.analyzer_server"):
        analyzer_server._record_ner_inference(telemetry)

    assert inference.label_values == [{"outcome": "failure"}]
    assert inference.increments == [1]
    assert latency.label_values == [{"outcome": "failure"}]
    assert latency.observations == [0.125]
    assert windows.label_values == [{"outcome": "failure"}]
    assert windows.observations == [2]
    assert failures.label_values == [
        {
            "phase": "inference",
            "failure_class": "forward_pass_failed",
        }
    ]
    events = _json_log_events(caplog, "presidio_ner_inference")
    assert events == [
        {
            "duration_ms": 125.0,
            "event": "presidio_ner_inference",
            "failure_class": "forward_pass_failed",
            "failure_phase": "inference",
            "outcome": "failure",
            "windows_processed": 2,
        }
    ]


def test_ner_telemetry_failure_does_not_affect_request_path(monkeypatch, caplog):
    monkeypatch.setattr(analyzer_server, "ANALYZER_NER_INFERENCE", _BrokenMetric())

    with caplog.at_level(logging.INFO, logger="presidio.analyzer_server"):
        analyzer_server._record_ner_inference(
            analyzer_server.NERInferenceTelemetry(
                outcome="success",
                duration_seconds=0.01,
                windows_processed=1,
            )
        )

    assert "error_type=RuntimeError" in caplog.text
    assert "metrics backend unavailable" not in caplog.text
    events = _json_log_events(caplog, "presidio_ner_inference")
    assert len(events) == 1
    assert events[0]["outcome"] == "success"


def test_blocking_analyze_cancelled_error_log_uses_error_type(monkeypatch, caplog):
    asyncio.run(_blocking_analyze_cancelled_error_log_uses_error_type(monkeypatch, caplog))


async def _blocking_analyze_cancelled_error_log_uses_error_type(monkeypatch, caplog):
    started = threading.Event()
    finish = threading.Event()

    def blocking_analyze_sync(_request):
        started.set()
        finish.wait(timeout=1)
        raise RuntimeError("raw +79031234567 should not be logged")

    monkeypatch.setattr(analyzer_server, "_analyze_sync", blocking_analyze_sync)

    request = analyzer_server.AnalyzeRequest(text="Телефон: +79031234567")
    with caplog.at_level(logging.ERROR, logger="presidio.analyzer_server"):
        task = asyncio.create_task(analyzer_server._run_blocking_analyze(request))
        try:
            await asyncio.wait_for(asyncio.to_thread(started.wait, 1), timeout=1)
            task.cancel()
            await asyncio.sleep(0)
            finish.set()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, timeout=1)
        finally:
            finish.set()
            if not task.done():
                task.cancel()

    logs = "\n".join(record.getMessage() for record in caplog.records)
    assert "error_type=RuntimeError" in logs
    assert "+79031234567" not in logs
    assert "raw" not in logs


def test_health_reports_capacity_without_entering_limiter(monkeypatch):
    asyncio.run(_health_reports_capacity_without_entering_limiter(monkeypatch))


async def _health_reports_capacity_without_entering_limiter(monkeypatch):
    limiter = AnalyzerCapacityLimiter(
        concurrency_limit=1,
        queue_limit=0,
        queue_timeout_seconds=0.01,
    )
    monkeypatch.setattr(analyzer_server, "capacity_limiter", limiter)
    _set_ner_state(
        monkeypatch,
        state="ready",
        loaded=True,
        warmed_up=True,
    )

    slot = await limiter.acquire()
    try:
        response = await analyzer_server.health()
    finally:
        await slot.release()

    assert response["status"] == "ok"
    assert response["ner"] == "loaded"
    assert response["ner_warmed_up"] is True
    assert response["ner_required"] is True
    assert response["capacity"]["active"] == 1
    assert response["capacity"]["queue_limit"] == 0


def test_health_is_unhealthy_when_required_ner_is_not_loaded(monkeypatch):
    asyncio.run(_health_is_unhealthy_when_required_ner_is_not_loaded(monkeypatch))


async def _health_is_unhealthy_when_required_ner_is_not_loaded(monkeypatch):
    _set_ner_state(
        monkeypatch,
        state="failed",
        loaded=False,
        warmed_up=False,
        failure_phase="artifact_verification",
        failure_class="artifact_invalid",
    )

    response = await analyzer_server.health()
    body = json.loads(response.body.decode())

    assert response.status_code == 503
    assert body["status"] == "unhealthy"
    assert body["ner"] == "not_loaded"
    assert body["ner_state"] == "failed"
    assert body["ner_warmed_up"] is False
    assert body["ner_required"] is True
    assert body["ner_failure_phase"] == "artifact_verification"
    assert body["ner_failure_class"] == "artifact_invalid"
    assert body["ner_backend"] == "huggingface_transformers"
    assert body["ner_model"] == analyzer_server.MODEL_ID
    assert body["ner_revision"] == analyzer_server.MODEL_REVISION


def test_health_is_unhealthy_when_loaded_ner_is_not_warmed_up(monkeypatch):
    asyncio.run(_health_is_unhealthy_when_loaded_ner_is_not_warmed_up(monkeypatch))


async def _health_is_unhealthy_when_loaded_ner_is_not_warmed_up(monkeypatch):
    _set_ner_state(
        monkeypatch,
        state="failed",
        loaded=True,
        warmed_up=False,
        failure_phase="inference",
        failure_class="forward_pass_failed",
    )

    response = await analyzer_server.health()
    body = json.loads(response.body.decode())

    assert response.status_code == 503
    assert body["status"] == "unhealthy"
    assert body["ner"] == "loaded"
    assert body["ner_state"] == "failed"
    assert body["ner_warmed_up"] is False
    assert body["ner_failure_phase"] == "inference"
    assert body["ner_failure_class"] == "forward_pass_failed"


def test_health_reports_fixed_model_identity(monkeypatch):
    asyncio.run(_health_reports_fixed_model_identity(monkeypatch))


async def _health_reports_fixed_model_identity(monkeypatch):
    _set_ner_state(
        monkeypatch,
        state="ready",
        loaded=True,
        warmed_up=True,
    )

    response = await analyzer_server.health()

    assert response["status"] == "ok"
    assert response["ner"] == "loaded"
    assert response["ner_state"] == "ready"
    assert response["ner_warmed_up"] is True
    assert response["ner_required"] is True
    assert response["ner_backend"] == "huggingface_transformers"
    assert response["ner_model"] == analyzer_server.MODEL_ID
    assert response["ner_revision"] == analyzer_server.MODEL_REVISION


def test_lifespan_refuses_to_start_when_required_ner_fails(monkeypatch, caplog):
    asyncio.run(_lifespan_refuses_to_start_when_required_ner_fails(monkeypatch, caplog))


async def _lifespan_refuses_to_start_when_required_ner_fails(monkeypatch, caplog):
    def fail_load():
        raise RuntimeError("checkpoint mismatch")

    monkeypatch.setattr(analyzer_server.ner_recognizer, "load_model", fail_load)
    monkeypatch.setattr(
        analyzer_server.ner_recognizer,
        "failure_phase",
        lambda: "model_validation",
    )
    monkeypatch.setattr(
        analyzer_server.ner_recognizer,
        "failure_class",
        lambda: "runtime_contract_invalid",
    )

    with caplog.at_level(logging.ERROR, logger="presidio.analyzer_server"):
        with pytest.raises(RuntimeError, match="Hugging Face NER is required"):
            async with analyzer_server.lifespan(None):
                pass

    events = _json_log_events(caplog, "presidio_ner_startup_failed")
    assert events == [
        {
            "event": "presidio_ner_startup_failed",
            "failure_class": "runtime_contract_invalid",
            "model": analyzer_server.MODEL_ID,
            "phase": "model_validation",
            "revision": analyzer_server.MODEL_REVISION,
        }
    ]
    logs = "\n".join(record.getMessage() for record in caplog.records)
    assert "checkpoint mismatch" not in logs


def test_lifespan_emits_safe_ner_startup_events(monkeypatch, caplog):
    asyncio.run(_lifespan_emits_safe_ner_startup_events(monkeypatch, caplog))


async def _lifespan_emits_safe_ner_startup_events(monkeypatch, caplog):
    monkeypatch.setattr(analyzer_server.ner_recognizer, "load_model", lambda: None)
    monkeypatch.setattr(analyzer_server.ner_recognizer, "state", lambda: "ready")
    monkeypatch.setattr(
        analyzer_server.ner_recognizer,
        "is_warmed_up",
        lambda: True,
    )

    with caplog.at_level(logging.INFO, logger="presidio.analyzer_server"):
        async with analyzer_server.lifespan(None):
            pass

    assert _json_log_events(caplog, "presidio_ner_startup_begin") == [
        {
            "event": "presidio_ner_startup_begin",
            "model": analyzer_server.MODEL_ID,
            "revision": analyzer_server.MODEL_REVISION,
        }
    ]
    assert _json_log_events(caplog, "presidio_ner_startup_ready") == [
        {
            "event": "presidio_ner_startup_ready",
            "model": analyzer_server.MODEL_ID,
            "revision": analyzer_server.MODEL_REVISION,
            "state": "ready",
            "warmed_up": True,
        }
    ]


def test_analyze_sync_does_not_return_partial_results_on_ner_failure(monkeypatch):
    regex_called = False

    def regex_results(**_kwargs):
        nonlocal regex_called
        regex_called = True
        return [object()]

    def fail_ner(*_args, **_kwargs):
        raise analyzer_server.NERBackendError(
            phase="inference",
            failure_class="forward_pass_failed",
        )

    monkeypatch.setattr(analyzer_server.analyzer, "analyze", regex_results)
    monkeypatch.setattr(analyzer_server.ner_recognizer, "require_ready", lambda: None)
    monkeypatch.setattr(analyzer_server.ner_recognizer, "analyze", fail_ner)

    with pytest.raises(analyzer_server.NERBackendError):
        analyzer_server._analyze_sync(
            analyzer_server.AnalyzeRequest(text="Телефон: +79031234567")
        )

    assert regex_called is True


def test_analyze_sync_rejects_degraded_regex_only_mode(monkeypatch):
    regex_called = False

    def regex_results(**_kwargs):
        nonlocal regex_called
        regex_called = True
        return []

    def reject_unready():
        raise analyzer_server.NERBackendError(
            phase="inference",
            failure_class="forward_pass_failed",
        )

    monkeypatch.setattr(analyzer_server.analyzer, "analyze", regex_results)
    monkeypatch.setattr(analyzer_server.ner_recognizer, "require_ready", reject_unready)

    with pytest.raises(analyzer_server.NERBackendError):
        analyzer_server._analyze_sync(
            analyzer_server.AnalyzeRequest(
                text="ИНН 7707083893",
                entities=["RU_INN"],
            )
        )

    assert regex_called is False


def test_analyze_sync_rechecks_readiness_before_return(monkeypatch):
    readiness_checks = 0

    def readiness_changes_during_request():
        nonlocal readiness_checks
        readiness_checks += 1
        if readiness_checks == 2:
            raise analyzer_server.NERBackendError(
                phase="inference",
                failure_class="forward_pass_failed",
            )

    monkeypatch.setattr(
        analyzer_server.ner_recognizer,
        "require_ready",
        readiness_changes_during_request,
    )
    monkeypatch.setattr(analyzer_server.analyzer, "analyze", lambda **_kwargs: [])

    with pytest.raises(analyzer_server.NERBackendError):
        analyzer_server._analyze_sync(
            analyzer_server.AnalyzeRequest(
                text="ИНН 7707083893",
                entities=["RU_INN"],
            )
        )

    assert readiness_checks == 2


def test_runtime_ner_failure_returns_safe_503(monkeypatch, caplog):
    asyncio.run(_runtime_ner_failure_returns_safe_503(monkeypatch, caplog))


async def _runtime_ner_failure_returns_safe_503(monkeypatch, caplog):
    limiter = AnalyzerCapacityLimiter(
        concurrency_limit=1,
        queue_limit=0,
        queue_timeout_seconds=0.01,
    )
    monkeypatch.setattr(analyzer_server, "capacity_limiter", limiter)

    async def fail_required_ner(_request):
        raise analyzer_server.NERBackendError(
            phase="inference",
            failure_class="forward_pass_failed",
        )

    monkeypatch.setattr(analyzer_server, "_run_blocking_analyze", fail_required_ner)
    secret = "raw-secret-value-must-not-appear"

    with caplog.at_level(logging.INFO, logger="presidio.analyzer_server"):
        with pytest.raises(HTTPException) as exc_info:
            await analyzer_server.analyze(
                analyzer_server.AnalyzeRequest(text=f"PASSWORD={secret}"),
            )

    assert exc_info.value.status_code == 503
    assert exc_info.value.detail == {
        "code": "required_ner_unavailable",
        "phase": "inference",
        "failure_class": "forward_pass_failed",
    }
    events = _json_log_events(caplog, "presidio_analyzer_request")
    assert len(events) == 1
    assert events[0]["outcome"] == "analyzer_error"
    assert events[0]["failure_reason"] == "required_ner_unavailable"
    assert events[0]["ner_failure_phase"] == "inference"
    assert events[0]["ner_failure_class"] == "forward_pass_failed"
    assert secret not in "\n".join(record.getMessage() for record in caplog.records)


def test_request_scoped_ner_failure_keeps_health_ready(monkeypatch, caplog):
    asyncio.run(_request_scoped_ner_failure_keeps_health_ready(monkeypatch, caplog))


async def _request_scoped_ner_failure_keeps_health_ready(monkeypatch, caplog):
    limiter = AnalyzerCapacityLimiter(
        concurrency_limit=1,
        queue_limit=0,
        queue_timeout_seconds=0.01,
    )
    monkeypatch.setattr(analyzer_server, "capacity_limiter", limiter)
    _set_ner_state(
        monkeypatch,
        state="ready",
        loaded=True,
        warmed_up=True,
    )

    async def fail_request_boundary(_request):
        raise analyzer_server.NERProcessingError(
            phase="windowing",
            failure_class="window_boundary_unresolved",
            windows_processed=3,
        )

    monkeypatch.setattr(
        analyzer_server,
        "_run_blocking_analyze",
        fail_request_boundary,
    )
    secret = "boundary-secret-must-not-appear"

    with caplog.at_level(logging.INFO, logger="presidio.analyzer_server"):
        with pytest.raises(HTTPException) as exc_info:
            await analyzer_server.analyze(
                analyzer_server.AnalyzeRequest(text=f"SECRET_KEY={secret}"),
            )

    assert exc_info.value.status_code == 503
    assert exc_info.value.detail == {
        "code": "required_ner_unavailable",
        "phase": "windowing",
        "failure_class": "window_boundary_unresolved",
    }
    health = await analyzer_server.health()
    assert health["status"] == "ok"
    assert health["ner_state"] == "ready"
    assert secret not in "\n".join(record.getMessage() for record in caplog.records)


def test_blocking_analyze_keeps_task_alive_until_thread_finishes_after_double_cancel(
    monkeypatch,
):
    asyncio.run(
        _blocking_analyze_keeps_task_alive_until_thread_finishes_after_double_cancel(
            monkeypatch,
        )
    )


async def _blocking_analyze_keeps_task_alive_until_thread_finishes_after_double_cancel(
    monkeypatch,
):
    started = threading.Event()
    finish = threading.Event()

    def blocking_analyze_sync(request):
        started.set()
        finish.wait(timeout=1)
        return analyzer_server.AnalyzeResponse(text=request.text, entities=[])

    monkeypatch.setattr(analyzer_server, "_analyze_sync", blocking_analyze_sync)

    request = analyzer_server.AnalyzeRequest(text="Иван Иванов")
    task = asyncio.create_task(analyzer_server._run_blocking_analyze(request))

    try:
        await asyncio.wait_for(asyncio.to_thread(started.wait, 1), timeout=1)
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        await asyncio.sleep(0.02)

        assert not task.done()

        finish.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=1)
    finally:
        finish.set()
        if not task.done():
            task.cancel()
