"""One monotonic budget includes admission and the still-running worker."""
import asyncio
import threading
import time

import pytest
from fastapi import HTTPException

from presidio import analyzer_server as server


@pytest.mark.parametrize("value", ["nan", "inf", "0", "-1", "86401", "not-a-number"])
def test_invalid_internal_budget_is_rejected(value):
    async def run():
        with pytest.raises(HTTPException) as error:
            await server.analyze(server.AnalyzeRequest(text="synthetic"), analysis_budget_header=value)
        assert error.value.status_code == 422
    asyncio.run(run())


def test_budget_expiry_in_queue_removes_waiter_without_starting_worker(monkeypatch):
    async def run():
        from capacity import AnalyzerCapacityLimiter
        limiter = AnalyzerCapacityLimiter(concurrency_limit=1, queue_limit=2, queue_timeout_seconds=1)
        monkeypatch.setattr(server, "capacity_limiter", limiter)
        slot = await limiter.acquire()
        calls = []
        monkeypatch.setattr(server, "_analyze_sync", lambda _r: calls.append(True))
        try:
            with pytest.raises(HTTPException) as error:
                await server.analyze(server.AnalyzeRequest(text="synthetic"), analysis_budget_header="0.02")
            assert error.value.status_code == 503
            assert error.value.detail["failure_class"] == "analysis_deadline_exceeded"
            assert error.value.detail["retryable"] is False
            assert not error.value.headers
            assert calls == []
            assert limiter.snapshot()["waiting"] == 0
            assert limiter.snapshot()["active"] == 1
        finally:
            await slot.release()
    asyncio.run(run())


def test_expired_worker_keeps_slot_until_cooperative_checkpoint(monkeypatch):
    async def run():
        from capacity import AnalyzerCapacityLimiter
        limiter = AnalyzerCapacityLimiter(concurrency_limit=1, queue_limit=0)
        monkeypatch.setattr(server, "capacity_limiter", limiter)
        entered, release = threading.Event(), threading.Event()
        def worker(_request):
            entered.set()
            assert release.wait(2)
            server.check_work()
            pytest.fail("expired work continued")
        monkeypatch.setattr(server, "_analyze_sync", worker)
        task = asyncio.create_task(server.analyze(server.AnalyzeRequest(text="synthetic"), analysis_budget_header="0.02"))
        try:
            await asyncio.to_thread(entered.wait, 1)
            await asyncio.sleep(0.04)
            assert limiter.snapshot()["active"] == 1
            assert not task.done()
            release.set()
            with pytest.raises(HTTPException) as error:
                await task
            assert error.value.detail["failure_class"] == "analysis_deadline_exceeded"
            assert not error.value.headers
            assert limiter.snapshot()["active"] == 0
        finally:
            release.set()
            await asyncio.gather(task, return_exceptions=True)
    asyncio.run(run())


def test_worker_and_all_phases_share_one_budget(monkeypatch):
    async def run():
        captures = []
        def worker(request):
            captures.append(server.remaining_seconds())
            time.sleep(0.01)
            captures.append(server.remaining_seconds())
            return server.AnalyzeResponse(text=request.text, entities=[])
        monkeypatch.setattr(server, "_analyze_sync", worker)
        await server.analyze(server.AnalyzeRequest(text="synthetic"), analysis_budget_header="0.5")
        assert 0 < captures[1] < captures[0] <= 0.5
        assert server.CURRENT_BUDGET.get() is None
    asyncio.run(run())
