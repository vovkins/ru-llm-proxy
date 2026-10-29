"""Forward ASGI events unchanged and cancel only our pre-provider work."""

import asyncio
from collections import deque
from contextvars import ContextVar
from typing import Awaitable, Callable, TypeVar

from litellm_guardrails.stream_cleanup import finish_stream_cleanup

_DISCONNECTED: ContextVar[asyncio.Event | None] = ContextVar(
    "ru_pii_http_disconnected", default=None,
)
_T = TypeVar("_T")


class ClientDisconnected(Exception):
    """The HTTP client left before our pre-provider work completed."""


async def _stop_tasks(*tasks: asyncio.Task) -> None:
    for task in tasks:
        if not task.done():
            task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


async def until_client_disconnect(work: Callable[[], Awaitable[_T]]) -> _T:
    disconnected = _DISCONNECTED.get()
    if disconnected is None:
        return await work()
    if disconnected.is_set():
        raise ClientDisconnected
    operation = asyncio.create_task(work(), name="ru-pii-preprocessing")
    monitor = asyncio.create_task(disconnected.wait(), name="ru-pii-disconnect-wait")
    try:
        await asyncio.wait((operation, monitor), return_when=asyncio.FIRST_COMPLETED)
        # Disconnection wins when both tasks finish in the same iteration.
        if disconnected.is_set():
            raise ClientDisconnected
        return operation.result()
    finally:
        await finish_stream_cleanup(_stop_tasks(operation, monitor))


class _ReceiveRelay:
    def __init__(self, receive, disconnected: asyncio.Event):
        self._receive = receive
        self._disconnected = disconnected
        self._messages = deque()
        self._condition = asyncio.Condition()
        self._failure: Exception | None = None

    async def pump(self) -> None:
        try:
            while True:
                message = await self._receive()
                async with self._condition:
                    if message["type"] == "http.disconnect":
                        self._disconnected.set()
                        self._condition.notify_all()
                        return
                    # One queued ASGI chunk, plus the chunk currently held by
                    # the reader. Never aggregate the complete request body.
                    await self._condition.wait_for(lambda: not self._messages)
                    self._messages.append(message)
                    self._condition.notify_all()
        except Exception as error:
            async with self._condition:
                self._failure = error
                self._condition.notify_all()

    async def receive(self):
        async with self._condition:
            await self._condition.wait_for(
                lambda: self._messages or self._disconnected.is_set() or self._failure is not None
            )
            if self._messages:
                message = self._messages.popleft()
                self._condition.notify_all()
                return message
            if self._failure is not None:
                raise self._failure
            # Multiple native listeners may observe the same terminal event.
            return {"type": "http.disconnect"}


class RequestCancellationMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope.get("method") != "POST":
            return await self.app(scope, receive, send)
        disconnected = asyncio.Event()
        token = _DISCONNECTED.set(disconnected)
        relay = _ReceiveRelay(receive, disconnected)
        reader = asyncio.create_task(relay.pump(), name="ru-pii-asgi-receive")
        try:
            return await self.app(scope, relay.receive, send)
        finally:
            try:
                await finish_stream_cleanup(_stop_tasks(reader))
            finally:
                _DISCONNECTED.reset(token)
