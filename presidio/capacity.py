"""Bounded capacity primitives for the Presidio Analyzer service."""

from __future__ import annotations

import asyncio
import os
from collections import deque
from dataclasses import dataclass
from typing import Any


DEFAULT_CONCURRENCY_LIMIT = 1
DEFAULT_QUEUE_LIMIT = 8
DEFAULT_QUEUE_TIMEOUT_SECONDS = 0.25


class CapacityRejected(Exception):
    """Raised when analyzer capacity is exhausted before work can start."""

    def __init__(self, reason: str, message: str, status_code: int = 503):
        super().__init__(message)
        self.reason = reason
        self.status_code = status_code


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

    async def __aenter__(self) -> "AnalyzerCapacitySlot":
        return self

    async def __aexit__(self, *_exc_info: Any) -> None:
        await self.release()

    async def release(self) -> None:
        if self._released:
            return
        self._released = True
        await self._limiter.release()


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
                )

            waiter = asyncio.get_running_loop().create_future()
            self._waiters.append(waiter)

        try:
            await asyncio.wait_for(waiter, timeout=self.queue_timeout_seconds)
            return AnalyzerCapacitySlot(self)
        except TimeoutError as exc:
            await self._cancel_waiter_or_release_reserved_slot(waiter)
            raise CapacityRejected(
                "queue_timeout",
                "Timed out waiting for Presidio Analyzer capacity.",
            ) from exc
        except asyncio.CancelledError:
            await self._cancel_waiter_or_release_reserved_slot(waiter)
            raise

    async def release(self) -> None:
        """Release one active capacity slot and wake one queued request."""
        async with self._condition:
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
        }


def build_limiter_from_env() -> AnalyzerCapacityLimiter:
    """Build a limiter from current environment variables."""
    settings = load_capacity_settings()
    return AnalyzerCapacityLimiter(
        concurrency_limit=settings.concurrency_limit,
        queue_limit=settings.queue_limit,
        queue_timeout_seconds=settings.queue_timeout_seconds,
    )
