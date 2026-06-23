"""Tests for analyzer capacity and backpressure primitives."""

import asyncio

import pytest

from presidio.capacity import AnalyzerCapacityLimiter, CapacityRejected


def test_rejects_when_active_and_waiting_limits_are_full():
    asyncio.run(_rejects_when_active_and_waiting_limits_are_full())


async def _rejects_when_active_and_waiting_limits_are_full():
    limiter = AnalyzerCapacityLimiter(
        concurrency_limit=1,
        queue_limit=1,
        queue_timeout_seconds=1,
    )
    first_slot = await limiter.acquire()

    waiter = asyncio.create_task(limiter.acquire())
    try:
        for _ in range(100):
            if limiter.snapshot()["waiting"] == 1:
                break
            await asyncio.sleep(0.001)

        assert limiter.snapshot()["active"] == 1
        assert limiter.snapshot()["waiting"] == 1

        with pytest.raises(CapacityRejected) as exc_info:
            await limiter.acquire()

        assert exc_info.value.reason == "queue_full"
        assert exc_info.value.status_code == 503
        assert limiter.snapshot()["waiting"] == 1
    finally:
        await first_slot.release()
        waiter_slot = await waiter
        await waiter_slot.release()


def test_rejects_when_queue_wait_times_out():
    asyncio.run(_rejects_when_queue_wait_times_out())


async def _rejects_when_queue_wait_times_out():
    limiter = AnalyzerCapacityLimiter(
        concurrency_limit=1,
        queue_limit=1,
        queue_timeout_seconds=0.01,
    )
    first_slot = await limiter.acquire()

    try:
        with pytest.raises(CapacityRejected) as exc_info:
            await limiter.acquire()

        assert exc_info.value.reason == "queue_timeout"
        assert exc_info.value.status_code == 503
    finally:
        await first_slot.release()

    assert limiter.snapshot()["active"] == 0
    assert limiter.snapshot()["waiting"] == 0


def test_waiting_request_acquires_slot_after_release():
    asyncio.run(_waiting_request_acquires_slot_after_release())


async def _waiting_request_acquires_slot_after_release():
    limiter = AnalyzerCapacityLimiter(
        concurrency_limit=1,
        queue_limit=1,
        queue_timeout_seconds=1,
    )
    first_slot = await limiter.acquire()
    waiter = asyncio.create_task(limiter.acquire())

    for _ in range(100):
        if limiter.snapshot()["waiting"] == 1:
            break
        await asyncio.sleep(0.001)

    await first_slot.release()
    waiter_slot = await waiter

    assert limiter.snapshot()["active"] == 1
    await waiter_slot.release()
    assert limiter.snapshot()["active"] == 0


def test_waiting_request_is_not_bypassed_by_later_arrival():
    asyncio.run(_waiting_request_is_not_bypassed_by_later_arrival())


async def _waiting_request_is_not_bypassed_by_later_arrival():
    limiter = AnalyzerCapacityLimiter(
        concurrency_limit=1,
        queue_limit=2,
        queue_timeout_seconds=0.05,
    )

    async with limiter._condition:
        existing_waiter = asyncio.get_running_loop().create_future()
        limiter._waiters.append(existing_waiter)

    later = asyncio.create_task(limiter.acquire())
    try:
        await asyncio.sleep(0)

        assert not later.done()
        assert limiter.snapshot()["active"] == 0
    finally:
        existing_waiter.cancel()
        async with limiter._condition:
            if existing_waiter in limiter._waiters:
                limiter._waiters.remove(existing_waiter)
        if later.done():
            slot = later.result()
            await slot.release()
        else:
            later.cancel()
            with pytest.raises(asyncio.CancelledError):
                await later
