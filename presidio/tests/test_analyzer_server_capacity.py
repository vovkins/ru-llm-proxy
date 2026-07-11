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
    monkeypatch.setattr(analyzer_server.dp_recognizer, "is_loaded", lambda: True)

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
    monkeypatch.setattr(analyzer_server.dp_recognizer, "is_loaded", lambda: False)

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
    assert event["failure_reason"] == "RuntimeError"
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
    response = await analyzer_server.metrics()
    body = response.body.decode()

    assert response.status_code == 200
    assert "ru_presidio_analyzer_requests_total" in body
    assert 'outcome="success"' in body
    assert "ru_presidio_analyzer_latency_seconds_bucket" in body
    assert "ru_presidio_analyzer_entities_detected_total" in body
    assert 'entity_type="EMAIL_ADDRESS"' in body
    assert "test@example.com" not in body


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

    slot = await limiter.acquire()
    try:
        response = await analyzer_server.health()
    finally:
        await slot.release()

    assert response["status"] == "ok"
    assert response["capacity"]["active"] == 1
    assert response["capacity"]["queue_limit"] == 0


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
