"""Cancel pre-provider work and preserve headers on our HTTP refusals."""

import asyncio
import json
import logging

import anyio
from collections import deque
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Awaitable, Callable, TypeVar

from litellm_guardrails.stream_cleanup import finish_stream_cleanup

_DISCONNECTED: ContextVar[asyncio.Event | None] = ContextVar(
    "ru_pii_http_disconnected", default=None,
)
_T = TypeVar("_T")
_REQUEST_MAPPING_CLEANUPS: ContextVar[dict | None] = ContextVar(
    "ru_pii_request_mapping_cleanups", default=None,
)
_AUDIT_ID_HEADER = b"x-ru-llm-proxy-request-id"
_RETRY_AFTER_HEADER = b"retry-after"


@dataclass
class _GuardrailAuditResponse:
    request_id: str | None = None
    refusal_status: int | None = None
    retry_after_seconds: int | None = None


_AUDIT_RESPONSE: ContextVar[_GuardrailAuditResponse | None] = ContextVar(
    "ru_pii_audit_response", default=None,
)


def bind_guardrail_audit_id(request_id: str) -> None:
    state = _AUDIT_RESPONSE.get()
    if state is not None:
        state.request_id = request_id
        state.refusal_status = None
        state.retry_after_seconds = None


def mark_guardrail_http_refusal(
    request_id: str | None, status: int, *, retry_after_seconds: int | None = None,
) -> None:
    state = _AUDIT_RESPONSE.get()
    if state is not None and request_id is not None and state.request_id == request_id:
        state.refusal_status = status
        state.retry_after_seconds = retry_after_seconds


def register_request_cleanup(request_id: str, callback) -> None:
    cleanups = _REQUEST_MAPPING_CLEANUPS.get()
    if cleanups is not None:
        cleanups[request_id] = callback


def unregister_request_cleanup(request_id: str) -> None:
    cleanups = _REQUEST_MAPPING_CLEANUPS.get()
    if cleanups is not None:
        cleanups.pop(request_id, None)


async def _finish_mapping_cleanups(cleanups: dict) -> None:
    pending = tuple(cleanups.items())
    cleanups.clear()
    for request_id, callback in pending:
        try:
            with anyio.fail_after(2, shield=True):
                await callback()
        except Exception as error:
            logging.getLogger(__name__).warning(json.dumps({
                "event": "pii_request_cleanup_failed", "request_id": request_id.rsplit(":", 1)[-1],
                "operation": request_id.split(":", 1)[0],
                "error_type": type(error).__name__,
            }))



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
        cleanups = {}
        cleanup_token = _REQUEST_MAPPING_CLEANUPS.set(cleanups)
        audit_response = _GuardrailAuditResponse()
        audit_token = _AUDIT_RESPONSE.set(audit_response)
        is_messages = scope.get("path") == "/v1/messages"
        relay = _ReceiveRelay(receive, disconnected)
        reader = asyncio.create_task(relay.pump(), name="ru-pii-asgi-receive")

        async def send_with_guardrail_headers(message):
            if message["type"] == "http.response.start":
                headers = message.get("headers", ())
                request_id = audit_response.request_id
                # Reserve the header for locally audited refusals, not the provider.
                publish = (message["status"] in (422, 503)
                           and message["status"] == audit_response.refusal_status
                           and request_id is not None)
                if publish or any(name.lower() == _AUDIT_ID_HEADER for name, _ in headers):
                    headers = [(name, value) for name, value in headers
                               if name.lower() != _AUDIT_ID_HEADER]
                    if publish:
                        headers.append((_AUDIT_ID_HEADER, request_id.encode("ascii")))
                        retry_after = audit_response.retry_after_seconds
                        # LiteLLM's Messages endpoint can discard the exception headers.
                        if (
                            is_messages and message["status"] == 503 and retry_after is not None
                            and not any(name.lower() == _RETRY_AFTER_HEADER for name, _ in headers)
                        ):
                            headers.append((_RETRY_AFTER_HEADER, str(retry_after).encode("ascii")))
                    message = {**message, "headers": headers}
            await send(message)

        try:
            return await self.app(scope, relay.receive, send_with_guardrail_headers)
        finally:
            try:
                async def cleanup():
                    try:
                        await _stop_tasks(reader)
                    finally:
                        await _finish_mapping_cleanups(cleanups)
                await finish_stream_cleanup(cleanup())
            finally:
                _AUDIT_RESPONSE.reset(audit_token)
                _REQUEST_MAPPING_CLEANUPS.reset(cleanup_token)
                _DISCONNECTED.reset(token)
