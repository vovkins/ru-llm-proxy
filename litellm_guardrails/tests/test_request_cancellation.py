"""ASGI event forwarding and request-local cancellation, without platform patches."""

import asyncio

import pytest

from litellm_guardrails.request_cancellation import (
    ClientDisconnected, RequestCancellationMiddleware, _DISCONNECTED,
    until_client_disconnect,
)

SCOPE = {"type": "http", "method": "POST", "path": "/v1/responses"}


@pytest.mark.asyncio
async def test_without_http_context_keeps_direct_hook_contract():
    async def work():
        return 42

    assert await until_client_disconnect(work) == 42


@pytest.mark.asyncio
@pytest.mark.parametrize("scope", [{"type": "lifespan"}, {"type": "websocket"},
                                      {"type": "http", "method": "GET"}])
async def test_other_protocols_and_read_routes_pass_through(scope):
    receive, send = object(), object()

    async def app(actual, actual_receive, actual_send):
        assert actual is scope
        assert actual_receive is receive and actual_send is send
        assert _DISCONNECTED.get() is None

    await RequestCancellationMiddleware(app)(scope, receive, send)


@pytest.mark.asyncio
async def test_body_is_forwarded_without_aggregation_and_send_without_buffering():
    incoming = asyncio.Queue()
    messages = [{"type": "http.request", "body": b"a" * 65536, "more_body": True},
                {"type": "http.request", "body": b"b", "more_body": False}]
    for message in messages:
        incoming.put_nowait(message)
    sent = []

    async def send(message):
        sent.append(message)

    async def app(scope, receive, send):
        assert await receive() is messages[0]
        assert await receive() is messages[1]
        for event in ({"type": "http.response.start", "status": 200},
                      {"type": "http.response.body", "body": b"data: first\n\n", "more_body": True},
                      {"type": "http.response.body", "body": b"data: last\n\n"}):
            await send(event)
            assert sent[-1] is event

    await RequestCancellationMiddleware(app)(SCOPE, incoming.get, send)
    assert len(sent) == 3
    assert _DISCONNECTED.get() is None


@pytest.mark.asyncio
async def test_slow_body_consumer_applies_backpressure():
    calls = 0
    began = asyncio.Event()

    async def receive():
        nonlocal calls
        calls += 1
        return {"type": "http.request", "body": b"large chunk", "more_body": True}

    async def app(*_args):
        began.set()
        await asyncio.Event().wait()

    task = asyncio.create_task(RequestCancellationMiddleware(app)(SCOPE, receive, None))
    await began.wait()
    for _ in range(10):
        await asyncio.sleep(0)
    assert calls <= 2
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_disconnect_is_repeated_for_multiple_native_listeners():
    incoming = asyncio.Queue()
    incoming.put_nowait({"type": "http.request", "body": b"", "more_body": False})

    async def app(scope, receive, send):
        assert (await receive())["type"] == "http.request"
        listeners = [asyncio.create_task(receive()) for _ in range(3)]
        incoming.put_nowait({"type": "http.disconnect"})
        assert await asyncio.gather(*listeners) == [{"type": "http.disconnect"}] * 3
        assert (await receive())["type"] == "http.disconnect"

    await RequestCancellationMiddleware(app)(SCOPE, incoming.get, None)


@pytest.mark.asyncio
async def test_disconnect_before_work_does_not_create_a_coroutine():
    called = False
    incoming = asyncio.Queue()
    incoming.put_nowait({"type": "http.disconnect"})

    def work():
        nonlocal called
        called = True
        raise AssertionError("must not start")

    async def app(scope, receive, send):
        await receive()
        with pytest.raises(ClientDisconnected):
            await until_client_disconnect(work)

    await RequestCancellationMiddleware(app)(SCOPE, incoming.get, None)
    assert not called


@pytest.mark.asyncio
async def test_disconnect_stops_only_preprocessing_and_drains_cleanup():
    incoming = asyncio.Queue()
    began, cleaned, platform_finished = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def work():
        began.set()
        try:
            await asyncio.Event().wait()
        finally:
            await asyncio.sleep(0)
            cleaned.set()

    async def app(*_args):
        with pytest.raises(ClientDisconnected):
            await until_client_disconnect(work)
        assert cleaned.is_set()
        platform_finished.set()

    task = asyncio.create_task(RequestCancellationMiddleware(app)(SCOPE, incoming.get, None))
    await began.wait()
    incoming.put_nowait({"type": "http.disconnect"})
    await asyncio.wait_for(task, 1)
    assert platform_finished.is_set() and not task.cancelled()


@pytest.mark.asyncio
async def test_disconnect_wins_over_a_simultaneously_finished_operation():
    incoming = asyncio.Queue()

    async def work():
        _DISCONNECTED.get().set()
        return "must not be forwarded"

    async def app(*_args):
        with pytest.raises(ClientDisconnected):
            await until_client_disconnect(work)

    await RequestCancellationMiddleware(app)(SCOPE, incoming.get, None)


@pytest.mark.asyncio
async def test_request_context_is_isolated_and_outer_cancel_drains_work():
    began, stopped = [asyncio.Event(), asyncio.Event()], [asyncio.Event(), asyncio.Event()]
    incoming = [asyncio.Queue(), asyncio.Queue()]

    def application(index):
        async def work():
            began[index].set()
            try:
                await asyncio.Event().wait()
            finally:
                stopped[index].set()

        async def app(*_args):
            await until_client_disconnect(work)

        return RequestCancellationMiddleware(app)

    tasks = [asyncio.create_task(application(i)(SCOPE, incoming[i].get, None)) for i in range(2)]
    for event in began:
        await event.wait()
    incoming[0].put_nowait({"type": "http.disconnect"})
    with pytest.raises(ClientDisconnected):
        await tasks[0]
    assert stopped[0].is_set() and not stopped[1].is_set()
    tasks[1].cancel()
    with pytest.raises(asyncio.CancelledError):
        await tasks[1]
    assert stopped[1].is_set()


@pytest.mark.asyncio
async def test_receive_errors_are_not_relabelled_as_client_disconnect():
    async def receive():
        raise RuntimeError("test receive failure")

    async def app(scope, receive, send):
        await receive()

    with pytest.raises(RuntimeError, match="test receive failure"):
        await RequestCancellationMiddleware(app)(SCOPE, receive, None)
