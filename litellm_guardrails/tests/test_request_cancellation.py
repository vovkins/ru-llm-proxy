"""ASGI event forwarding and request-local cancellation, without platform patches."""

import asyncio

import pytest

from litellm_guardrails.request_cancellation import (
    ClientDisconnected, RequestCancellationMiddleware, _AUDIT_RESPONSE, _DISCONNECTED,
    bind_guardrail_audit_id, mark_guardrail_http_refusal, until_client_disconnect,
)

SCOPE = {"type": "http", "method": "POST", "path": "/v1/responses"}
MESSAGES_SCOPE = {**SCOPE, "path": "/v1/messages"}


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
@pytest.mark.parametrize("status,expected", [(422, True), (503, False), (200, False)])
async def test_only_matching_guardrail_refusal_owns_audit_header(status, expected):
    sent = []
    audit_header = b"x-ru-llm-proxy-request-id"
    headers = [(b"retry-after", b"5"), (b"X-Ru-LLM-Proxy-Request-Id", b"untrusted")]
    body = {"type": "http.response.body", "body": b"unchanged"}

    async def app(scope, receive, send):
        bind_guardrail_audit_id("server-id")

        async def work():
            mark_guardrail_http_refusal("server-id", 422)
            mark_guardrail_http_refusal("foreign-id", 503)

        await until_client_disconnect(work)
        await send({"type": "http.response.start", "status": status, "headers": headers})
        await send(body)

    async def send(message):
        sent.append(message)

    await RequestCancellationMiddleware(app)(SCOPE, asyncio.Queue().get, send)
    assert sent[0]["headers"] == [(b"retry-after", b"5")] + (
        [(audit_header, b"server-id")] if expected else []
    )
    assert sent[1] is body
    assert headers[1][1] == b"untrusted"
    assert _AUDIT_RESPONSE.get() is None


@pytest.mark.asyncio
@pytest.mark.parametrize("path,refusal_status,status,existing,expected", [
    ("/v1/messages", 503, 503, None, (b"retry-after", b"7")),
    ("/v1/messages", 503, 503, (b"Retry-After", b"13"), (b"Retry-After", b"13")),
    ("/v1/chat/completions", 503, 503, None, None),
    ("/v1/responses", 503, 503, None, None),
    ("/v1/messages/count_tokens", 503, 503, None, None),
    ("/v1/messages", 422, 422, None, None),
    ("/v1/messages", 503, 429, None, None),
    ("/v1/messages", None, 503, None, None),
    ("/v1/messages", None, 429, (b"retry-after", b"9"), (b"retry-after", b"9")),
])
async def test_retry_after_only_fills_missing_local_messages_503(path, refusal_status, status, existing, expected):
    headers = [existing] if existing else []
    sent = []

    async def app(scope, receive, send):
        bind_guardrail_audit_id("server-id")
        if refusal_status is not None:
            mark_guardrail_http_refusal("server-id", refusal_status, retry_after_seconds=7)
        mark_guardrail_http_refusal("foreign-id", 503, retry_after_seconds=31)
        await send({"type": "http.response.start", "status": status, "headers": headers})

    async def send(message):
        sent.append(message)

    await RequestCancellationMiddleware(app)({**SCOPE, "path": path}, asyncio.Queue().get, send)
    assert [header for header in sent[0]["headers"] if header[0].lower() == b"retry-after"] == (
        [expected] if expected else []
    )
    assert headers == ([existing] if existing else [])


@pytest.mark.asyncio
async def test_audit_refusal_after_sse_start_does_not_modify_sent_events():
    start = {"type": "http.response.start", "status": 200, "headers": [
        (b"content-type", b"text/event-stream"),
    ]}
    body = {"type": "http.response.body", "body": b"data: error\n\n"}
    sent = []

    async def app(scope, receive, send):
        bind_guardrail_audit_id("server-id")
        await send(start)
        mark_guardrail_http_refusal("server-id", 503, retry_after_seconds=7)
        await send(body)

    async def send(message):
        sent.append(message)

    await RequestCancellationMiddleware(app)(MESSAGES_SCOPE, asyncio.Queue().get, send)
    assert sent[0] is start and sent[1] is body


@pytest.mark.asyncio
async def test_parallel_requests_do_not_share_audit_refusals():
    both_bound = asyncio.Event()
    bound = 0

    async def run(request_id, refused, delay=None):
        sent = []

        async def app(scope, receive, send):
            nonlocal bound
            bind_guardrail_audit_id(request_id)
            bound += 1
            if bound == 4:
                both_bound.set()
            await both_bound.wait()
            if refused:
                mark_guardrail_http_refusal(request_id, 503, retry_after_seconds=delay)
            await send({"type": "http.response.start", "status": 503, "headers": []})

        async def send(message):
            sent.append(message)

        await RequestCancellationMiddleware(app)(MESSAGES_SCOPE, asyncio.Queue().get, send)
        return sent[0]["headers"]

    assert await asyncio.gather(
        run("first", True, 2), run("second", True, 7), run("third", True), run("fourth", False),
    ) == [
        [(b"x-ru-llm-proxy-request-id", b"first"), (b"retry-after", b"2")],
        [(b"x-ru-llm-proxy-request-id", b"second"), (b"retry-after", b"7")],
        [(b"x-ru-llm-proxy-request-id", b"third")], [],
    ]


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
