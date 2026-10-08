"""Public SDK integration: terminal evidence and request-owned HTTP resources."""

import asyncio
import gzip
import json
import ssl
import tracemalloc
import zlib
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import anyio
import httpx
import litellm
import pytest
import pytest_asyncio
from openai import AsyncOpenAI
from litellm.llms.custom_httpx.http_handler import AsyncHTTPHandler
from litellm.proxy.types_utils.utils import get_instance_fn

from litellm_guardrails import upstream_stream as upstream
from litellm_guardrails.pii_guardrail import RuPIIGuardrail
from litellm_guardrails.sse_integrity import IncompleteUpstreamStream, SSEIntegrity, verified_response
from litellm_guardrails.stream_cleanup import finish_stream_cleanup


def test_config_loader_returns_the_same_adapter_as_guardrail_imports():
    config = str(Path(__file__).resolve().parents[2] / "litellm-config.yaml")
    for _ in range(2):
        assert get_instance_fn("litellm_guardrails.upstream_stream_adapter", config_file_path=config) is upstream.adapter
        guard = get_instance_fn("litellm_guardrails.pii_guardrail.RuPIIGuardrail", config_file_path=config)
        assert guard.async_pre_call_hook.__globals__["close_request_streams"] is upstream.close_request_streams


def frame(value, newline=b"\n"):
    return b"data: " + (value.encode() if isinstance(value, str) else json.dumps(value).encode()) + newline * 2


def chat(text="", finish=None, index=0):
    return {"id": "chat-test", "object": "chat.completion.chunk", "created": 1,
            "model": "mock-chat", "choices": [{"index": index, "delta": {"content": text}, "finish_reason": finish}]}


def frames(api, mode="complete"):
    if api == "chat":
        first = frame(chat("safe <PHONE_"))
        terminal = frame(chat("NUMBER_1>", "length" if mode == "length" else "stop"))
    else:
        snapshot = {"id": "resp_test", "object": "response", "created_at": 1, "model": "mock-chat", "output": []}
        first = frame({"type": "response.created", "response": {**snapshot, "status": "in_progress"}})
        kind = "incomplete" if mode == "length" else "completed"
        terminal = frame({"type": f"response.{kind}", "response": {**snapshot, "status": kind}})
    if mode == "eof":
        return [first]
    if mode == "partial":
        return [first, b'data: {"broken":']
    if mode == "done-only":
        return [first, frame("[DONE]")]
    if mode == "error":
        error = {"message": "Synthetic provider failure", "type": "server_error"}
        return [first, frame({"error": error} if api == "chat" else
                             {"type": "error", "code": "server_error", "message": error["message"]})]
    return [first, terminal] + ([frame("[DONE]")] if api == "chat" else [])


class Wire(httpx.AsyncByteStream):
    def __init__(self, chunks, close_wait=None):
        self.chunks = chunks
        self.closed = 0
        self.close_wait = close_wait

    async def __aiter__(self):
        for chunk in self.chunks:
            yield chunk

    async def aclose(self):
        if self.close_wait:
            await self.close_wait.wait()
        self.closed += 1


@pytest.mark.parametrize("api", ["chat", "responses"])
@pytest.mark.parametrize("mode", ["complete", "length", "error", "eof", "partial", "done-only"])
@pytest.mark.parametrize("newline", [b"\n", b"\r", b"\r\n"])
def test_terminal_evidence_at_every_byte_boundary(api, mode, newline):
    payload = b"".join(frames(api, mode)).replace(b"\n", newline)
    parser = SSEIntegrity(api)
    invalid = mode in {"eof", "partial", "done-only"}
    try:
        for byte in payload:
            parser.feed(bytes([byte]))
        parser.finish()
    except IncompleteUpstreamStream:
        assert invalid
    else:
        assert not invalid
        assert parser.terminal == ("provider_error" if mode == "error" else
                                   "provider_incomplete" if mode == "length" and api == "responses" else "complete")


def test_all_choices_require_their_own_real_finish():
    parser = SSEIntegrity("chat", choices=2)
    parser.feed(frame(chat(finish="stop", index=0)))
    with pytest.raises(IncompleteUpstreamStream, match="missing_terminal"):
        parser.finish()
    parser.feed(frame(chat(finish="tool_calls", index=1)))
    parser.feed(frame({"choices": [], "usage": {"total_tokens": 4}}))
    parser.feed(frame("[DONE]"))
    parser.finish()


@pytest.mark.parametrize("payload,reason", [
    (b"data: [\n\n", "invalid_event_json"),
    (frame([]), "invalid_event_shape"),
    (frame({"choices": [{"index": 10}]}), "invalid_choice_index"),
    (frame(chat(finish="")), "invalid_finish_reason"),
    (frame(chat(finish="stop")) + frame("[DONE]") + frame(chat()), "data_after_done"),
])
def test_malformed_events_are_safe_protocol_errors(payload, reason):
    with pytest.raises(IncompleteUpstreamStream, match=reason):
        SSEIntegrity("chat").feed(payload)


def test_comments_multiline_utf8_and_per_event_memory_bound():
    parser = SSEIntegrity("responses", max_event_bytes=96)
    parser.feed(b": heartbeat\r\n\r\n")
    parser.feed('data: {"type":\ndata: "response.completed", "text":"Тверь"}\n\n'.encode())
    parser.finish()
    for _ in range(1000):
        parser.feed(b": heartbeat\n\n")
    assert not parser.line and not parser.data and parser.event_bytes == 0
    with pytest.raises(IncompleteUpstreamStream, match="event_too_large"):
        parser.feed(b"data: " + b"x" * 97)
    assert len(parser.line) <= 96


@pytest.mark.asyncio
@pytest.mark.parametrize("encoding", [None, "gzip", "deflate"])
async def test_httpx_decodes_compression_without_changing_events(encoding):
    data = frame({"type": "response.completed", "encrypted_content": "opaque <PHONE_NUMBER_1>",
                  "response": {"output": ["x" * (2 * 1024 * 1024)]}})
    encoded = gzip.compress(data) if encoding == "gzip" else zlib.compress(data) if encoding == "deflate" else data
    wire = Wire([encoded[i:i + 37] for i in range(0, len(encoded), 37)])
    observations = []
    raw = httpx.Response(200, headers={"content-encoding": encoding} if encoding else {},
                         stream=wire, request=httpx.Request("POST", "http://test.invalid/v1/responses"))
    response = verified_response(raw, "responses", 1, lambda *args: observations.append(args))
    assert await response.aread() == data
    await response.aclose()
    assert wire.closed == 1
    assert observations == [("complete", None)]
    assert "content-encoding" not in response.headers


@pytest_asyncio.fixture
async def manager(monkeypatch):
    wires, requests = [], []

    async def provider(request):
        requests.append(request)
        api = "responses" if request.url.path.endswith("/responses") else "chat"
        wire = Wire(getattr(value, "test_frames", frames(api, getattr(value, "test_mode", "complete"))))
        wires.append(wire)
        return httpx.Response(200, stream=wire, headers={"content-type": "text/event-stream", "set-cookie": "session=other-account"})

    value = upstream.StreamManager(httpx.MockTransport(provider))
    monkeypatch.setattr(upstream, "_manager", lambda create=True: value)
    yield value, wires, requests
    await value.aclose()
    assert not value.requests and not value.closing and not value.pools


@pytest.mark.asyncio
@pytest.mark.parametrize("api", ["chat", "responses"])
@pytest.mark.parametrize("mode", ["complete", "length", "eof", "partial", "done-only", "error", "cancel"])
async def test_real_stock_router_uses_public_injection_and_closes_owned_source(manager, api, mode):
    value, wires, requests = manager
    value.test_mode = mode
    before = litellm.callbacks
    litellm.callbacks = [upstream.adapter]
    router = litellm.Router(model_list=[{"model_name": "test", "litellm_params": {
        "model": "openai/mock-chat", "api_base": "http://test.invalid/v1",
        "api_key": "synthetic-test", "max_retries": 0}}], num_retries=0, timeout=3)
    events, error = [], None
    kwargs = {"model": "test", "stream": True, "litellm_metadata": {upstream.TRANSPORT_ID: "server-id"}}
    try:
        stream = await (router.acompletion(messages=[{"role": "user", "content": "safe"}], **kwargs)
                        if api == "chat" else router.aresponses(input="safe", **kwargs))
        if mode != "cancel":
            async for event in stream:
                events.append(event.model_dump() if hasattr(event, "model_dump") else event)
        await stream.aclose()
    except Exception as exc:
        error = exc
    finally:
        await value.close_request("server-id")
        litellm.callbacks = before
    assert requests and all(w.closed == 1 for w in wires)
    assert not value.requests
    assert upstream.TRANSPORT_ID not in requests[0].content.decode()
    if mode in {"eof", "partial", "done-only", "error"}:
        assert error
        assert not any(c.get("finish_reason") for e in events for c in e.get("choices", []))
        assert "response.completed" not in [e.get("type") for e in events]
    else:
        assert not error


@pytest.mark.asyncio
async def test_pool_keeps_neighbor_headers_timeouts_and_cookies_isolated(manager):
    value, wires, requests = manager
    clients = [await value.client({"timeout": 7}, key, "chat") for key in ("a", "b")]
    assert clients[0].pool is clients[1].pool
    for index, client in enumerate(clients):
        client.headers["Authorization"] = f"Bearer synthetic-{index}"
        await client.send(client.build_request("POST", "http://test.invalid/v1/chat/completions"), stream=True)
    await value.close_request("a")
    assert wires[0].closed == 1 and not wires[1].closed
    assert not clients[1].pool.client.is_closed
    response = await clients[1].send(clients[1].build_request("POST", "http://test.invalid/v1/chat/completions"), stream=True)
    await response.aread()
    assert all(r.extensions["timeout"]["read"] == 7 for r in requests)
    assert [r.headers["Authorization"] for r in requests] == ["Bearer synthetic-0", "Bearer synthetic-1", "Bearer synthetic-1"]
    assert all("cookie" not in r.headers for r in requests)
    await value.close_request("b")
    assert all(w.closed == 1 for w in wires)


@pytest.mark.asyncio
async def test_cleanup_is_idempotent_with_repeated_cancellation(manager):
    value, _, _ = manager
    client = await value.client({}, "a", "chat")
    gate = asyncio.Event()
    wire = Wire([], close_wait=gate)
    client.responses.append(httpx.Response(200, stream=wire))
    task = asyncio.create_task(finish_stream_cleanup(value.close_request("a")))
    await asyncio.sleep(0.01)
    task.cancel()
    task.cancel()
    other = asyncio.create_task(value.close_request("a"))
    gate.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    await other
    await value.close_request("a")
    assert wire.closed == 1 and client.released and client.pool.borrowers == 0


@pytest.mark.asyncio
async def test_preconfigured_responses_client_is_copied_not_mutated(manager):
    value, _, requests = manager
    previous = AsyncHTTPHandler(timeout=11)
    previous.client.headers["X-Account"] = "synthetic-account"
    try:
        result = await upstream.adapter.async_pre_call_deployment_hook({
            "model": "openai/mock-chat", "stream": True, "client": previous,
            "litellm_metadata": {upstream.TRANSPORT_ID: "a"}}, "aresponses")
        sdk = result["client"]
        assert sdk is not previous and sdk.client is not previous.client
        assert not previous.client.is_closed
        response = await sdk.client.send(sdk.client.build_request("POST", "http://test.invalid/v1/responses"), stream=True)
        await response.aread()
        assert requests[0].headers["X-Account"] == "synthetic-account"
        assert requests[0].extensions["timeout"]["read"] == 11
        await value.close_request("a")
        assert not previous.client.is_closed
    finally:
        await previous.client.aclose()


@pytest.mark.asyncio
async def test_nonstream_and_other_providers_do_not_get_custom_clients(manager):
    value, _, _ = manager
    for kwargs, kind in [({"model": "openai/test", "stream": False}, "acompletion"),
                         ({"model": "anthropic/test", "stream": True}, "acompletion"),
                         ({"model": "openai/test", "stream": True}, "embedding")]:
        assert await upstream.adapter.async_pre_call_deployment_hook(kwargs, kind) is kwargs
        assert "client" not in kwargs
    assert not value.pools


def test_tls_and_proxy_configuration_is_part_of_pool_identity(monkeypatch):
    monkeypatch.setenv("NO_PROXY", "test.invalid")
    context = ssl.create_default_context()
    first = upstream._ssl_settings({"ssl_verify": context})
    assert first[1] is context
    monkeypatch.setenv("NO_PROXY", "other.invalid")
    assert first[0] != upstream._ssl_settings({"ssl_verify": context})[0]
    assert upstream._ssl_settings({"ssl_verify": False})[1] is False


@pytest.mark.asyncio
async def test_clean_stream_gets_separate_correlation_without_mapping(monkeypatch):
    guard = RuPIIGuardrail()
    guard._redis = AsyncMock()
    guard._analyze_text = AsyncMock(return_value=[])
    data = {"model": "mock-chat", "stream": True, "messages": [{"role": "user", "content": "safe"}],
            "litellm_metadata": {upstream.TRANSPORT_ID: "caller-forged"}}
    result = await guard.async_pre_call_hook(MagicMock(), None, data, "acompletion")
    assert upstream.transport_request_id(result) not in {None, "caller-forged"}
    assert "pii_request_id" not in result.get("metadata", {})
    guard._redis.setex.assert_not_awaited()


@pytest.mark.asyncio
async def test_hung_close_is_bounded_and_other_responses_still_close(manager):
    value, _, _ = manager
    client = await value.client({}, "a", "chat")
    hung, normal = Wire([], asyncio.Event()), Wire([])
    client.responses.extend([httpx.Response(200, stream=wire) for wire in (hung, normal)])
    with anyio.fail_after(3):
        await value.close_request("a")
    assert normal.closed == 1 and not value.requests and client.pool.borrowers == 0


@pytest.mark.asyncio
async def test_many_records_in_one_chunk_do_not_hit_the_per_event_limit():
    data = frame(chat("x")) * 1000 + frame(chat(finish="stop"))
    parser = SSEIntegrity("chat", max_event_bytes=256)
    parser.feed(data)
    parser.finish()


@pytest.mark.asyncio
@pytest.mark.parametrize("api", ["chat", "responses"])
@pytest.mark.parametrize("mode", ["retry", "fallback"])
async def test_router_retries_and_fallbacks_keep_all_attempts_owned(monkeypatch, api, mode):
    requests, wires = [], []

    async def provider(request):
        requests.append(request)
        if (mode == "retry" and len(requests) == 1) or (mode == "fallback" and request.url.host == "primary.invalid"):
            return httpx.Response(429, json={"error": {"type": "rate_limit_error", "message": "Synthetic retry"}})
        wire = Wire(frames(api))
        wires.append(wire)
        return httpx.Response(200, stream=wire, headers={"content-type": "text/event-stream"})

    value = upstream.StreamManager(httpx.MockTransport(provider))
    monkeypatch.setattr(upstream, "_manager", lambda create=True: value)
    before = litellm.callbacks
    litellm.callbacks = [upstream.adapter]
    router = litellm.Router(model_list=[{"model_name": name, "litellm_params": {
        "model": "openai/mock-chat", "api_base": f"http://{name}.invalid/v1", "api_key": "synthetic-test", "max_retries": 0}}
        for name in ("primary", "backup")], num_retries=1 if mode == "retry" else 0, retry_after=0,
        fallbacks=[{"primary": ["backup"]}] if mode == "fallback" else None)
    kwargs = {"model": "primary", "stream": True, "litellm_metadata": {upstream.TRANSPORT_ID: "a"}}
    try:
        stream = await (router.acompletion(messages=[{"role": "user", "content": "safe"}], **kwargs)
                        if api == "chat" else router.aresponses(input="safe", **kwargs))
        assert [event async for event in stream]
        await stream.aclose()
        assert len(requests) == 2
        if mode == "fallback":
            assert requests[-1].url.host == "backup.invalid"
        await value.close_request("a")
        assert not value.requests and all(w.closed == 1 for w in wires)
    finally:
        litellm.callbacks = before
        await value.aclose()


@pytest.mark.asyncio
async def test_pools_are_bounded_and_idle_ones_can_be_replaced(manager, monkeypatch):
    value, _, _ = manager
    monkeypatch.setattr(upstream, "MAX_HTTP_POOLS", 2)
    for key in ("a", "b"):
        monkeypatch.setenv("NO_PROXY", key)
        await value.client({}, key, "chat")
    monkeypatch.setenv("NO_PROXY", "c")
    with pytest.raises(httpx.PoolTimeout):
        await value.client({}, "c", "chat")
    await value.close_request("a")
    await value.client({}, "c", "chat")
    assert len(value.pools) == 2


@pytest.mark.asyncio
async def test_auth_redirects_and_event_hooks_follow_preconfigured_client(manager):
    value, _, requests = manager
    previous = AsyncHTTPHandler(timeout=9)
    previous.client.auth = httpx.BasicAuth("synthetic", "test")
    previous.client.follow_redirects = False
    hook = AsyncMock()
    previous.client.event_hooks = {"request": [hook], "response": []}
    try:
        kwargs = await upstream.adapter.async_pre_call_deployment_hook({
            "model": "openai/mock-chat", "stream": True, "client": previous,
            "litellm_metadata": {upstream.TRANSPORT_ID: "a"}}, "aresponses")
        response = await kwargs["client"].client.get("http://test.invalid/v1/responses")
        assert response.status_code == 200
        assert requests[0].headers["authorization"].startswith("Basic ")
        hook.assert_awaited_once()
    finally:
        await value.close_request("a")
        await previous.client.aclose()


@pytest.mark.asyncio
async def test_complete_incomplete_and_failed_response_bytes_are_not_rewritten():
    for kind, outcome in [("completed", "complete"), ("incomplete", "provider_incomplete"), ("failed", "provider_error")]:
        data = frame({"type": f"response.{kind}", "response": {"status": kind, "output": [],
                     "encrypted_content": "opaque", "error": {"code": "test"} if kind == "failed" else None}})
        observations = []
        raw = httpx.Response(200, stream=Wire([data]), request=httpx.Request("POST", "http://test.invalid/v1/responses"))
        response = verified_response(raw, "responses", 1, lambda *args: observations.append(args))
        assert await response.aread() == data
        assert observations == [(outcome, None)]


@pytest.mark.asyncio
async def test_first_event_is_yielded_without_waiting_for_the_terminal():
    gate = asyncio.Event()

    class DelayedWire(Wire):
        async def __aiter__(self):
            yield frame(chat("first"))
            await gate.wait()
            yield frame(chat(finish="stop"))

    response = verified_response(httpx.Response(200, stream=DelayedWire([]),
        request=httpx.Request("POST", "http://test.invalid/v1/chat/completions")), "chat", 1, lambda *args: None)
    iterator = response.aiter_bytes()
    with anyio.fail_after(0.5):
        assert b"first" in await anext(iterator)
    await response.aclose()


@pytest.mark.asyncio
async def test_metrics_and_logs_use_only_bounded_transport_metadata(manager, caplog):
    value, _, _ = manager
    value.test_mode = "partial"
    client = await value.client({}, "server-id", "chat")
    response = await client.send(client.build_request("POST", "http://test.invalid/v1/chat/completions",
                                headers={"Authorization": "Bearer SECRET_TEST_KEY"}), stream=True)
    with pytest.raises(IncompleteUpstreamStream):
        await response.aread()
    await value.close_request("server-id")
    assert '"outcome": "incomplete"' in caplog.text
    assert '"reason": "truncated_record"' in caplog.text
    assert "SECRET_TEST_KEY" not in caplog.text and "PHONE_" not in caplog.text


def test_stream_memory_does_not_grow_with_total_output():
    data = frame(chat("x" * 2048))
    parser = SSEIntegrity("chat")
    tracemalloc.start()
    try:
        for _ in range(10000):
            parser.feed(data)
        parser.feed(frame(chat(finish="stop")))
        parser.finish()
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert peak < 128 * 1024, "More than 20 MiB of output must not accumulate"


@pytest.mark.asyncio
async def test_compressed_event_uses_the_decoded_size_limit():
    wire = Wire([gzip.compress(frame({"type": "response.completed", "text": "x" * 20000}))])
    observations = []
    raw = httpx.Response(200, headers={"content-encoding": "gzip"}, stream=wire,
                         request=httpx.Request("POST", "http://test.invalid/v1/responses"))
    response = verified_response(raw, "responses", 1, lambda *args: observations.append(args))
    response.stream.integrity.max_event_bytes = 1024
    try:
        with pytest.raises(IncompleteUpstreamStream, match="event_too_large"):
            await response.aread()
    finally:
        await response.aclose()
    assert observations == [("incomplete", "event_too_large")]
    assert wire.closed == 1 and not response.stream.integrity.line and not response.stream.integrity.data


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["complete", "eof", "partial", "cancel"])
async def test_stock_chat_to_responses_bridge_keeps_transport_protection(manager, monkeypatch, mode):
    value, wires, requests = manager
    value.test_mode = mode
    monkeypatch.setattr(litellm, "route_all_chat_openai_to_responses", True)
    before = litellm.callbacks
    litellm.callbacks = [upstream.adapter]
    router = litellm.Router(model_list=[{"model_name": "test", "litellm_params": {
        "model": "openai/gpt-5.6-luna", "api_base": "http://test.invalid/v1", "api_key": "synthetic-test", "max_retries": 0}}], num_retries=0)
    try:
        stream = await router.acompletion(model="test", stream=True, messages=[{"role": "user", "content": "safe"}],
            litellm_metadata={upstream.TRANSPORT_ID: "a"})
        if mode in {"eof", "partial"}:
            events, error = [], None
            try:
                async for event in stream:
                    events.append(event)
            except Exception as exc:
                error = exc
            assert error and not any(c.finish_reason for event in events for c in event.choices)
        elif mode != "cancel":
            assert [event async for event in stream]
        await stream.aclose()
        assert requests[0].url.path.endswith("/responses")
        await value.close_request("a")
        assert wires and all(w.closed == 1 for w in wires)
    finally:
        litellm.callbacks = before


@pytest.mark.asyncio
@pytest.mark.parametrize("complete", [False, True])
async def test_stock_router_requires_all_requested_choices_to_finish(manager, complete):
    value, wires, _ = manager
    value.test_frames = [frame(chat("first")), frame(chat(finish="stop"))]
    if complete:
        value.test_frames.append(frame(chat("second", finish="length", index=1)))
    value.test_frames.append(frame("[DONE]"))
    before = litellm.callbacks
    litellm.callbacks = [upstream.adapter]
    router = litellm.Router(model_list=[{"model_name": "test", "litellm_params": {
        "model": "openai/mock-chat", "api_base": "http://test.invalid/v1", "api_key": "synthetic-test", "max_retries": 0}}], num_retries=0)
    try:
        stream = await router.acompletion(model="test", stream=True, n=2,
            messages=[{"role": "user", "content": "safe"}], litellm_metadata={upstream.TRANSPORT_ID: "a"})
        guard = RuPIIGuardrail()
        protected = guard.async_post_call_streaming_iterator_hook(MagicMock(), stream,
            {"litellm_metadata": {upstream.TRANSPORT_ID: "a"}})
        events = []
        if complete:
            events = [event async for event in protected]
            assert events
        else:
            with pytest.raises(IncompleteUpstreamStream, match="done_without_terminal"):
                async for event in protected:
                    events.append(event)
            assert not any(c.index == 1 and c.finish_reason for event in events for c in event.choices)
        await stream.aclose()
        await value.close_request("a")
        assert all(w.closed == 1 for w in wires)
    finally:
        litellm.callbacks = before


@pytest.mark.asyncio
async def test_preconfigured_openai_sdk_preserves_account_and_headers(manager):
    value, _, requests = manager
    previous = AsyncOpenAI(api_key="synthetic-account-key", base_url="http://test.invalid/v1", timeout=9,
        default_headers={"X-Account": "synthetic-account"})
    try:
        kwargs = await upstream.adapter.async_pre_call_deployment_hook({
            "model": "openai/mock-chat", "stream": True, "client": previous,
            "litellm_metadata": {upstream.TRANSPORT_ID: "a"}}, "acompletion")
        sdk = kwargs["client"]
        assert sdk is not previous
        stream = await sdk.chat.completions.create(model="mock-chat", messages=[{"role": "user", "content": "safe"}], stream=True)
        assert [event async for event in stream]
        await stream.close()
        await value.close_request("a")
        assert requests[0].headers["X-Account"] == "synthetic-account"
        assert requests[0].headers["authorization"] == "Bearer synthetic-account-key"
        assert requests[0].extensions["timeout"]["read"] == 9
        assert not previous.is_closed()
    finally:
        await previous.close()


@pytest.mark.asyncio
async def test_simultaneous_pool_replacement_cannot_orphan_a_borrowed_pool(manager, monkeypatch):
    value, _, _ = manager
    monkeypatch.setattr(upstream, "MAX_HTTP_POOLS", 1)
    monkeypatch.setenv("NO_PROXY", "a")
    await value.client({}, "a", "chat")
    await value.close_request("a")
    close_pool = value._close_pool

    async def delayed_close(pool):
        await asyncio.sleep(0.01)
        await close_pool(pool)

    monkeypatch.setattr(value, "_close_pool", delayed_close)
    monkeypatch.setenv("NO_PROXY", "b")
    first, second = await asyncio.gather(value.client({}, "b", "chat"), value.client({}, "c", "chat"))
    assert first.pool is second.pool and len(value.pools) == 1 and first.pool.borrowers == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("choices", [0, -1, True, 129, "2"])
async def test_invalid_choice_count_does_not_create_a_pool(manager, choices):
    value, _, _ = manager
    with pytest.raises(ValueError, match="choice count"):
        await value.client({"n": choices}, "a", "chat")
    assert not value.pools and not value.requests
