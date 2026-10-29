"""The burst reporter never equates HTTP 200 with successful restoration."""
import asyncio
import json
from pathlib import Path
import sys

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tests/load"))
import burst_probe


def spec(*, stream=False):
    return {"units": 10, "offset": 0, "owner_index": 0, "api": "responses", "stream": stream,
            "key": "private-test-key", "payload": {"input": "private-request-body"},
            "expected_marker": "private-response-body"}


def test_error_parser_keeps_identifiers_and_discards_content():
    value = burst_probe.error_fields({"error": {"code": "analyzer_overloaded", "message": "secret",
        "details": {"reason": "queue_full"}}}, {"Retry-After": "7"})
    assert value == {"code": "analyzer_overloaded", "reason": "queue_full", "retry_after_seconds": 7}
    assert "secret" not in json.dumps(value)


def test_json_success_does_not_persist_key_or_bodies():
    def handle(request):
        return httpx.Response(200, json={"output": [{"content": [{"text": "private-response-body"}]}]})
    result = asyncio.run(burst_probe.run_wave("http://mock", [spec()], transport=httpx.MockTransport(handle)))
    assert result["successful"] == 1 and result["within_60_seconds"] == 1
    assert not any(text in json.dumps(result) for text in ("private-test-key", "private-request-body", "private-response-body"))


def test_sse_requires_terminal_and_exact_restoration():
    def handle(request):
        return httpx.Response(200, text='data: {"type":"response.output_text.delta","delta":"private-response-body"}\n\n')
    result = asyncio.run(burst_probe.run_wave("http://mock", [spec(stream=True)], transport=httpx.MockTransport(handle)))
    assert result["successful"] == 0
    assert result["rows"][0]["outcome"] == "restoration_or_protocol_mismatch"


def test_sse_success_and_error_are_separate_even_with_http_200():
    for terminal, expected in (("response.completed", 1), ("response.failed", 0)):
        def handle(request):
            return httpx.Response(200, text='data: {"type":"response.output_text.delta","delta":"private-response-body"}\n\n'
                + 'data: '+json.dumps({"type": terminal, "response": {"error": {"code": "analyzer_unavailable"}} if expected == 0 else {}})+'\n\n')
        result = asyncio.run(burst_probe.run_wave("http://mock", [spec(stream=True)], transport=httpx.MockTransport(handle)))
        assert result["successful"] == expected


def test_cancelled_waiter_is_counted_not_successful():
    async def handle(request):
        await asyncio.sleep(1)
        return httpx.Response(200, json={})
    result = asyncio.run(burst_probe.run_wave("http://mock", [spec()], cancel_indices=[0],
        cancel_after=.01, transport=httpx.MockTransport(handle)))
    assert result["cancelled"] == 1 and result["successful"] == 0


def test_offsets_are_independent_of_slow_responses():
    active = maximum = 0
    async def handle(request):
        nonlocal active, maximum
        active += 1
        maximum = max(active, maximum)
        await asyncio.sleep(.05)
        active -= 1
        return httpx.Response(503, json={"error": {"code": "analyzer_overloaded"}})
    result = asyncio.run(burst_probe.run_wave("http://mock", [spec(), spec(), spec()],
                                           transport=httpx.MockTransport(handle)))
    assert maximum == 3 and result["started"] == result["failed"] == 3
