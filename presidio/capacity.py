"""Bounded capacity primitives for the Presidio Analyzer service."""

from __future__ import annotations

import asyncio
import math
import os
import time
from collections import deque
from dataclasses import dataclass
from typing import Any


DEFAULT_CONCURRENCY_LIMIT = 1
DEFAULT_QUEUE_LIMIT = 8
DEFAULT_QUEUE_TIMEOUT_SECONDS = 1.0
DEFAULT_RETRY_AFTER_SECONDS = 1
SERVICE_TIME_EWMA_ALPHA = 0.2


class CapacityRejected(Exception):
    """Raised when analyzer capacity is exhausted before work can start."""

    def __init__(
        self,
        reason: str,
        message: str,
        status_code: int = 503,
        retry_after_seconds: int = DEFAULT_RETRY_AFTER_SECONDS,
    ):
        super().__init__(message)
        self.reason = reason
        self.status_code = status_code
        self.retry_after_seconds = max(1, int(retry_after_seconds))


@dataclass
class CapacitySettings:
    """Runtime capacity settings for one analyzer process."""

    concurrency_limit: int = DEFAULT_CONCURRENCY_LIMIT
    queue_limit: int = DEFAULT_QUEUE_LIMIT
    queue_timeout_seconds: float = DEFAULT_QUEUE_TIMEOUT_SECONDS


def _get_int_env(name: str, default: int, minimum: int) -> int:
    raw_value = os.getenv(name)
    if raw_value is None:
        return default
    try:
        value = int(raw_value)
    except ValueError:
        return default
    return value if value >= minimum else default


def _get_float_env(name: str, default: float, minimum: float) -> float:
    raw_value = os.getenv(name)
    if raw_value is None:
        return default
    try:
        value = float(raw_value)
    except ValueError:
        return default
    return value if value >= minimum else default


def load_capacity_settings() -> CapacitySettings:
    """Load analyzer capacity settings from environment variables."""
    return CapacitySettings(
        concurrency_limit=_get_int_env(
            "PRESIDIO_ANALYZER_CONCURRENCY_LIMIT",
            DEFAULT_CONCURRENCY_LIMIT,
            minimum=1,
        ),
        queue_limit=_get_int_env(
            "PRESIDIO_ANALYZER_QUEUE_LIMIT",
            DEFAULT_QUEUE_LIMIT,
            minimum=0,
        ),
        queue_timeout_seconds=_get_float_env(
            "PRESIDIO_ANALYZER_QUEUE_TIMEOUT_SECONDS",
            DEFAULT_QUEUE_TIMEOUT_SECONDS,
            minimum=0,
        ),
    )


class AnalyzerCapacitySlot:
    """Acquired analyzer capacity slot."""

    def __init__(self, limiter: "AnalyzerCapacityLimiter"):
        self._limiter = limiter
        self._released = False
        self._acquired_at = time.monotonic()

    async def __aenter__(self) -> "AnalyzerCapacitySlot":
        return self

    async def __aexit__(self, *_exc_info: Any) -> None:
        await self.release()

    async def release(self) -> None:
        if self._released:
            return
        self._released = True
        await self._limiter.release(
            service_seconds=max(0.0, time.monotonic() - self._acquired_at)
        )


class AnalyzerCapacityLimiter:
    """Bound per-process analyzer concurrency and waiting queue length."""

    def __init__(
        self,
        concurrency_limit: int = DEFAULT_CONCURRENCY_LIMIT,
        queue_limit: int = DEFAULT_QUEUE_LIMIT,
        queue_timeout_seconds: float = DEFAULT_QUEUE_TIMEOUT_SECONDS,
    ):
        self.concurrency_limit = max(1, int(concurrency_limit))
        self.queue_limit = max(0, int(queue_limit))
        self.queue_timeout_seconds = max(0.0, float(queue_timeout_seconds))
        self._condition = asyncio.Condition()
        self._active = 0
        self._waiters: deque[asyncio.Future[None]] = deque()
        self._service_time_ewma_seconds: float | None = None

    @property
    def retry_after_seconds(self) -> int:
        """Return a bounded delay based on configured wait and observed work."""
        estimate = max(
            float(DEFAULT_RETRY_AFTER_SECONDS),
            self.queue_timeout_seconds,
            self._service_time_ewma_seconds or 0.0,
        )
        return min(3_600, max(1, math.ceil(estimate)))

    async def acquire(self) -> AnalyzerCapacitySlot:
        """Acquire a capacity slot or reject when queue policy is exceeded."""
        waiter: asyncio.Future[None] | None = None
        async with self._condition:
            if self._active < self.concurrency_limit and not self._waiters:
                self._active += 1
                return AnalyzerCapacitySlot(self)

            if len(self._waiters) >= self.queue_limit:
                raise CapacityRejected(
                    "queue_full",
                    "Presidio Analyzer capacity queue is full.",
                    retry_after_seconds=self.retry_after_seconds,
                )

            waiter = asyncio.get_running_loop().create_future()
            self._waiters.append(waiter)

        try:
            await asyncio.wait_for(waiter, timeout=self.queue_timeout_seconds)
            return AnalyzerCapacitySlot(self)
        except (TimeoutError, asyncio.TimeoutError) as exc:
            await self._cancel_waiter_or_release_reserved_slot(waiter)
            raise CapacityRejected(
                "queue_timeout",
                "Timed out waiting for Presidio Analyzer capacity.",
                retry_after_seconds=self.retry_after_seconds,
            ) from exc
        except asyncio.CancelledError:
            await self._cancel_waiter_or_release_reserved_slot(waiter)
            raise

    async def release(self, *, service_seconds: float | None = None) -> None:
        """Release one active capacity slot and wake one queued request."""
        async with self._condition:
            if service_seconds is not None:
                service_seconds = max(0.0, float(service_seconds))
                previous = self._service_time_ewma_seconds
                self._service_time_ewma_seconds = (
                    service_seconds
                    if previous is None
                    else (
                        SERVICE_TIME_EWMA_ALPHA * service_seconds
                        + (1 - SERVICE_TIME_EWMA_ALPHA) * previous
                    )
                )
            if self._active > 0:
                self._active -= 1
                self._wake_next_waiter_unlocked()

    async def _cancel_waiter_or_release_reserved_slot(
        self,
        waiter: asyncio.Future[None],
    ) -> None:
        async with self._condition:
            try:
                self._waiters.remove(waiter)
                waiter.cancel()
                return
            except ValueError:
                pass

            if waiter.done() and not waiter.cancelled() and self._active > 0:
                self._active -= 1
                self._wake_next_waiter_unlocked()

    def _wake_next_waiter_unlocked(self) -> None:
        while self._waiters and self._active < self.concurrency_limit:
            waiter = self._waiters.popleft()
            if waiter.done():
                continue
            self._active += 1
            waiter.set_result(None)
            return

    def snapshot(self) -> dict[str, int | float]:
        """Return current limiter state for health and tests."""
        return {
            "concurrency_limit": self.concurrency_limit,
            "queue_limit": self.queue_limit,
            "queue_timeout_seconds": self.queue_timeout_seconds,
            "active": self._active,
            "waiting": len(self._waiters),
            "retry_after_seconds": self.retry_after_seconds,
            "service_time_ewma_seconds": (
                round(self._service_time_ewma_seconds, 6)
                if self._service_time_ewma_seconds is not None
                else None
            ),
        }


def build_limiter_from_env() -> AnalyzerCapacityLimiter:
    """Build a limiter from current environment variables."""
    settings = load_capacity_settings()
    return AnalyzerCapacityLimiter(
        concurrency_limit=settings.concurrency_limit,
        queue_limit=settings.queue_limit,
        queue_timeout_seconds=settings.queue_timeout_seconds,
    )
