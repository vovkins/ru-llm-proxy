"""Await bounded cleanup even when the caller receives repeated cancellation."""

import asyncio
from typing import Any, Coroutine

import anyio


async def finish_stream_cleanup(cleanup: Coroutine[Any, Any, None]) -> None:
    task = asyncio.create_task(cleanup, name="ru-pii-stream-cleanup")
    interruption = None
    with anyio.CancelScope(shield=True):
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError as error:
                if task.cancelled():
                    raise
                interruption = error
            except Exception:
                break
        try:
            task.result()
        finally:
            if interruption is not None:
                raise interruption
