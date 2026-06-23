"""Synthetic concurrency checks for Analyzer endpoint backpressure."""

import asyncio
import sys
import time
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("presidio_analyzer")

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi import HTTPException

from presidio import analyzer_server
from presidio.capacity import AnalyzerCapacityLimiter


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
