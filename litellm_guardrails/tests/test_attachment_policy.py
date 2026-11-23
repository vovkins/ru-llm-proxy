"""Attachment DLP regressions using only synthetic content."""

import base64
import copy
import json
from unittest.mock import AsyncMock

import httpx
import litellm
import pytest

from litellm_guardrails.attachment_policy import (
    MAX_ATTACHMENT_BYTES,
    RejectFileUploadsMiddleware,
)
from litellm_guardrails.pii_guardrail import HTTPException, ProxyException, RuPIIGuardrail
from litellm_guardrails.tests.test_anthropic_restoration import message
from litellm_guardrails.tests.test_pii_guardrail import _entity, _MemoryRedis
from litellm_guardrails.tests.test_responses_history import response


EMAIL = "fixture-person@example.test"
CALL_TYPES = ("acompletion", "aresponses", "anthropic_messages")


def _request(call_type, text, *, json_file=False):
    filename = "fixture.json" if json_file else "fixture.txt"
    if call_type == "anthropic_messages":
        container = {"type": "text", "media_type": "text/plain", "data": text}
        block = {"type": "document", "title": filename, "source": container}
        return {"messages": [{"role": "user", "content": [block]}]}, block, lambda: container["data"]
    mime_type = "application/json" if json_file else "text/plain"
    prefix = f"data:{mime_type};base64,"
    container = {"filename": filename, "file_data": prefix + base64.b64encode(text.encode()).decode()}
    if call_type == "aresponses":
        block = {"type": "input_file", **container}
        request = {"input": [{"role": "user", "content": [block]}]}
        container = block
    else:
        block = {"type": "file", "file": container}
        request = {"messages": [{"role": "user", "content": [block]}]}
    return request, block, lambda: base64.b64decode(container["file_data"].removeprefix(prefix)).decode()


def _guard(**overrides):
    options = {
        "dictionary_substitutions_enabled": True,
        "dictionary_substitutions_json": json.dumps({"substitutions": [{
            "id": "fixture", "source": "DemoQuartz", "replacement": "DemoCobalt",
        }]}),
        "pre_egress_policy_mode": "block",
        "final_payload_leak_check_mode": "block",
    }
    guard = RuPIIGuardrail(**(options | overrides))
    guard._redis = _MemoryRedis()
    guard._analysis_cache_secret = None

    async def analyze(text):
        return [_entity(text, EMAIL, "EMAIL_ADDRESS")] if EMAIL in text else []

    guard._analyze_text = AsyncMock(side_effect=analyze)
    return guard


def _reply(call_type, text):
    if call_type == "aresponses":
        reply = response("resp-fixture", text)
        return reply, lambda: reply["output"][0]["content"][0]["text"]
    if call_type == "anthropic_messages":
        reply = message(text)
        return reply, lambda: reply["content"][0]["text"]
    reply = litellm.ModelResponse(choices=[{
        "index": 0, "message": {"role": "assistant", "content": text}, "finish_reason": "stop",
    }])
    return reply, lambda: reply.choices[0].message.content


@pytest.mark.asyncio
@pytest.mark.parametrize("call_type", CALL_TYPES)
@pytest.mark.parametrize("json_file", [False, True])
async def test_attachment_runs_dictionary_analyzer_and_restoration(call_type, json_file):
    guard = _guard()
    original = (r'{"text":"\u0044emoQuartz ' + EMAIL + '","number":1e99,"flag":true}'
                if json_file else f"DemoQuartz {EMAIL}")
    request, _, outgoing = _request(call_type, original, json_file=json_file)

    await guard.async_pre_call_hook(None, None, request, call_type)

    analyzed = guard._analyze_text.await_args.args[0]
    assert "DemoCobalt" in analyzed and "DemoQuartz" not in analyzed and EMAIL in analyzed
    encoded_output = outgoing()
    assert "DemoCobalt <EMAIL_ADDRESS_1>" in encoded_output and EMAIL not in encoded_output
    if json_file:
        assert json.loads(encoded_output) == {
            "text": "DemoCobalt <EMAIL_ADDRESS_1>", "number": 1e99, "flag": True,
        }
        assert '"number":1e99' in encoded_output

    reply, client_output = _reply(call_type, encoded_output)
    await guard.async_post_call_success_hook(request, None, reply)

    if json_file:
        assert json.loads(client_output()) == json.loads(original)
    else:
        assert client_output() == original
    assert not guard._redis.values


@pytest.mark.asyncio
@pytest.mark.parametrize("call_type", CALL_TYPES)
@pytest.mark.parametrize("json_file", [False, True])
async def test_pre_egress_inspects_original_attachment_before_dictionary_and_analyzer(call_type, json_file):
    guard = _guard()
    config = "API_KEY=fixture-only\nPASSWORD=fixture-password"
    content = json.dumps({"document": config}) if json_file else config
    request, _, _ = _request(call_type, content, json_file=json_file)
    original = copy.deepcopy(request)

    with pytest.raises(ProxyException) as error:
        await guard.async_pre_call_hook(None, None, request, call_type)

    response_body = error.value.provider_specific_fields
    assert response_body["error"]["code"] == "pre_egress_policy_blocked"
    assert request == original
    guard._analyze_text.assert_not_awaited()
    assert not guard._redis.values


@pytest.mark.asyncio
async def test_final_filter_checks_after_dictionary_inside_base64():
    guard = _guard(final_payload_leak_check_canaries=("DemoCobalt",))
    request, _, _ = _request("acompletion", "DemoQuartz")
    original = copy.deepcopy(request)

    with pytest.raises(litellm.UnprocessableEntityError) as error:
        await guard.async_pre_call_hook(None, None, request, "acompletion")

    assert error.value.response.json()["error"]["code"] == "final_payload_leak_check_blocked"
    assert request == original
    assert not guard._redis.values


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["analyzer", "redis", "pre_egress", "dictionary"])
async def test_attachment_failures_are_closed_with_fail_open_settings(failure):
    overrides = {"dictionary_substitutions_failure_mode": "fail_open", "failure_mode": "fail_open"}
    if failure == "pre_egress":
        overrides["pre_egress_policy_mode"] = "off"
    elif failure == "dictionary":
        overrides["dictionary_substitutions_enabled"] = False
    guard = _guard(**overrides)
    if failure == "analyzer":
        guard._analyze_text.side_effect = OSError()
    elif failure == "redis":
        guard._redis.setex = AsyncMock(side_effect=OSError())
    request, _, _ = _request("acompletion", "DemoQuartz")
    original = copy.deepcopy(request)

    with pytest.raises(HTTPException) as error:
        await guard.async_pre_call_hook(None, None, request, "acompletion")

    assert error.value.status_code == 503
    assert error.value.detail["error"]["code"] == "attachment_dlp_unavailable"
    assert request == original
    assert not guard._redis.values


@pytest.mark.asyncio
@pytest.mark.parametrize("failure,status", [
    ("file_id", 422), ("file_url", 422), ("base64", 422), ("json", 422),
    ("size", 413), ("count", 413), ("mime", 415), ("utf8", 422),
])
async def test_rejects_unprocessable_inline_data(failure, status):
    guard = _guard()
    text = "X" * (MAX_ATTACHMENT_BYTES + 1) if failure == "size" else "plain fixture"
    request, block, _ = _request("aresponses", text, json_file=failure == "json")
    if failure in ("file_id", "file_url"):
        block[failure] = "synthetic-reference"
    elif failure == "base64":
        block["file_data"] = "data:text/plain;base64,**"
    elif failure == "mime":
        block["file_data"] = block["file_data"].replace("text/plain", "application/pdf")
    elif failure == "utf8":
        block["file_data"] = "data:text/plain;base64,/w=="
    elif failure == "count":
        request["input"][0]["content"] = [copy.deepcopy(block) for _ in range(5)]

    with pytest.raises(HTTPException) as error:
        await guard.async_pre_call_hook(None, None, request, "aresponses")

    assert error.value.status_code == status
    guard._analyze_text.assert_not_awaited()
    assert not guard._redis.values


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["/v1/files", "/files", "/openai/v1/files", "/v1/files/"])
async def test_upload_route_returns_before_reading_or_routing_body(path):
    app = AsyncMock()
    body = AsyncMock(side_effect=AssertionError("Request body must stay unread"))
    outgoing = AsyncMock()

    await RejectFileUploadsMiddleware(app)(
        {"type": "http", "method": "POST", "path": path}, body, outgoing,
    )

    app.assert_not_awaited()
    body.assert_not_awaited()
    headers, reply = (call.args[0] for call in outgoing.await_args_list)
    assert headers["status"] == 403
    assert json.loads(reply["body"])["error"]["code"] == "attachment_upload_disabled"


@pytest.mark.asyncio
async def test_upload_gate_keeps_other_routes_working():
    async def app(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    transport = httpx.ASGITransport(app=RejectFileUploadsMiddleware(app))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        assert (await client.get("/v1/files")).status_code == 200
        assert (await client.post("/v1/responses")).status_code == 200


@pytest.mark.asyncio
async def test_mixed_request_shares_mapping_across_attachment_and_text():
    guard = _guard()
    request, _, outgoing = _request("acompletion", f"DemoQuartz {EMAIL}")
    blocks = request["messages"][0]["content"]
    blocks.insert(0, {"type": "text", "text": f"DemoQuartz {EMAIL}"})

    await guard.async_pre_call_hook(None, None, request, "acompletion")

    assert blocks[0]["text"] == "DemoCobalt <EMAIL_ADDRESS_1>"
    assert outgoing() == "DemoCobalt <EMAIL_ADDRESS_2>"
    reply, client_output = _reply("acompletion", blocks[0]["text"] + "\n" + outgoing())
    await guard.async_post_call_success_hook(request, None, reply)
    assert client_output() == f"DemoQuartz {EMAIL}\nDemoQuartz {EMAIL}"


@pytest.mark.asyncio
async def test_block_mode_never_commits_document_with_pii():
    guard = _guard(pii_mode="block")
    request, _, _ = _request("anthropic_messages", f"DemoQuartz {EMAIL}")
    original = copy.deepcopy(request)

    with pytest.raises(litellm.UnprocessableEntityError) as error:
        await guard.async_pre_call_hook(None, None, request, "anthropic_messages")

    assert error.value.response.json()["error"]["code"] == "pii_blocked"
    assert request == original
    assert not guard._redis.values


@pytest.mark.asyncio
async def test_json_key_collisions_are_refused_before_committing_the_file():
    guard = _guard(dictionary_substitutions_json=json.dumps({"substitutions": [{
        "id": "fixture", "source": "DemoQuartz", "replacement": "DemoCobalt", "restore": False,
    }]}))
    request, _, _ = _request(
        "acompletion", '{"DemoQuartz":1,"DemoCobalt":2}', json_file=True,
    )
    original = copy.deepcopy(request)

    with pytest.raises(ProxyException) as error:
        await guard.async_pre_call_hook(None, None, request, "acompletion")

    assert error.value.status_code == 422
    assert request == original
    assert not guard._redis.values


@pytest.mark.asyncio
async def test_extra_body_has_the_same_attachment_reference_policy():
    guard = _guard()
    request = {"extra_body": {"input": [{"type": "input_file", "file_id": "file-fixture"}]}}

    with pytest.raises(HTTPException) as error:
        await guard.async_pre_call_hook(None, None, request, "aresponses")

    assert error.value.detail["error"]["code"] == "attachment_reference_unsupported"
    guard._analyze_text.assert_not_awaited()
