"""The burst reporter never equates HTTP 200 with successful restoration."""
import asyncio
import json
from pathlib import Path
import sys

import httpx
import pytest

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
        assert result["successful_sse_within_60_seconds"] == expected


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


def test_subagents_share_key_but_not_dialogue_marker_or_input():
    specs = burst_probe.build_wave(["private-key"], count=4, sizes=(100, 200), serial_start=10)
    assert [s["owner_index"] for s in specs] == [0] * 4
    assert len({s["expected_marker"] for s in specs}) == 4
    assert [s["api"] for s in specs] == ["chat", "responses", "chat", "responses"]
    assert all(s["key"] == "private-key" and s["stream"] for s in specs)
    assert [s["offset"] for s in specs] == [0, 2 / 3, 4 / 3, 2]
    assert all(s["payload"]["model"].startswith("mock-") for s in specs)


@pytest.mark.parametrize("api", ["chat", "responses"])
def test_history_is_a_separate_reusable_field_not_a_new_file(api):
    specs = burst_probe.build_wave(["one", "two"], count=2, sizes=(120,), history_units=100, api=api)
    messages = [s["payload"]["messages" if api == "chat" else "input"] for s in specs]
    assert [s["owner_index"] for s in specs] == [0, 1]
    assert messages[0][0] == messages[1][0]
    for item, spec in zip(messages, specs):
        assert [len(m["content"].split()) for m in item] == [100, 20]
        assert spec["expected_marker"] not in item[0]["content"]
        assert spec["expected_marker"] in item[1]["content"]
        assert spec["units"] == 120 and spec["history_units"] == 100


def test_fresh_waves_differ_throughout_large_inputs_and_repeat_is_reproducible():
    a, b = [burst_probe.build_wave(["key"], count=1, sizes=(2000,), api="chat", serial_start=i)[0]
            for i in (100, 200)]
    left, right = [s["payload"]["messages"][0]["content"].split() for s in (a, b)]
    assert len(left) == len(right) == 2000
    assert all(left[i:i + 64] != right[i:i + 64] for i in range(0, 1900, 64))
    assert burst_probe.build_wave(["key"], count=2, fresh=False) == burst_probe.build_wave(["key"], count=2, fresh=False)


@pytest.mark.parametrize("kwargs", [
    {"count": 0}, {"count": 257}, {"count": True}, {"sizes": ()}, {"sizes": (0,)},
    {"sizes": (1_000_001,)}, {"sizes": (1_000_000,), "count": 5},
    {"history_units": -1}, {"history_units": 1000}, {"spread_seconds": float("nan")},
    {"spread_seconds": float("inf")}, {"spread_seconds": -1}, {"spread_seconds": "private-value"},
    {"serial_start": -1}, {"api": "private-value"}, {"stream": "true"},
])
def test_unsafe_profile_is_rejected_before_text_allocation(monkeypatch, kwargs):
    def forbidden(*args, **values):
        raise AssertionError("Text must not be allocated")
    monkeypatch.setattr(burst_probe, "test_text", forbidden)
    with pytest.raises(ValueError):
        burst_probe.build_wave(["key"], **dict({"count": 1}, **kwargs))


@pytest.mark.parametrize("keys", [[], "private-key", [None], [""]])
def test_invalid_key_pool_is_not_interpreted_as_users(keys):
    with pytest.raises(ValueError):
        burst_probe.build_wave(keys, count=1)


def test_first_sse_text_budget_is_not_full_stream_completion_budget():
    rows = [{"outcome": "success", "started": True, "http_status": 200, "seconds": 90,
             "stream": True, "first_text_seconds": 2},
            {"outcome": "error", "started": True, "http_status": 503, "seconds": 30,
             "stream": True, "phase": "inference", "failure_class": "triton_overloaded"}]
    result = burst_probe.summarize(rows, 90)
    assert result["within_60_seconds"] == 0
    assert result["successful_sse_within_60_seconds"] == 1
    assert result["sse_first_text_seconds"]["p95"] == 2
    assert result["sse_without_text"] == 1
    assert result["latency_seconds_failure"]["p95"] == 30
    assert result["failure_classes"] == {"triton_overloaded": 1}
    assert result["error_phases"] == {"inference": 1}


def test_multifield_history_has_tool_arguments_and_outputs_for_both_apis():
    specs = burst_probe.build_multifield_wave(["private-key"], count=2, sizes=(120,), serial_start=100)
    for spec in specs:
        items = spec["payload"]["messages" if spec["api"] == "chat" else "input"]
        argument = items[1]["tool_calls"][0]["function"]["arguments"] if spec["api"] == "chat" else items[1]["arguments"]
        tool_output = items[2]["content"] if spec["api"] == "chat" else items[2]["output"]
        fields = [items[0]["content"], json.loads(argument)["report"], tool_output, items[3]["content"]]
        assert sum(len(text.split()) for text in fields) == 120
        assert all(spec["expected_marker"] in text for text in fields)
        assert spec["text_fields"] == 4 and not spec["history_reused"]
    assert specs[0]["expected_marker"] != specs[1]["expected_marker"]


def test_multifield_reused_history_is_identical_but_current_message_is_new():
    left, right = [burst_probe.build_multifield_wave(["key"], count=2, sizes=(120,),
                   serial_start=serial, shared_history_serial=50) for serial in (100, 200)]
    for a, b in zip(left, right):
        field = "messages" if a["api"] == "chat" else "input"
        assert a["payload"][field][:-1] == b["payload"][field][:-1]
        assert a["payload"][field][-1] != b["payload"][field][-1]
        assert a["expected_marker"] == b["expected_marker"]
        assert a["history_reused"] and a["history_units"] == 90


@pytest.mark.parametrize("value", [-1, True, "private-value", 1_000_000_001])
def test_multifield_history_serial_is_bounded_before_allocation(monkeypatch, value):
    monkeypatch.setattr(burst_probe, "build_wave", lambda *a, **k: pytest.fail("must not allocate"))
    with pytest.raises(ValueError):
        burst_probe.build_multifield_wave(["key"], count=1, shared_history_serial=value)


def test_generated_inputs_and_profile_metadata_never_escape_in_report():
    specs = burst_probe.build_wave(["private-key"], count=2, sizes=(100,), history_units=50)
    async def handle(request):
        body = json.loads(request.content)
        index = 0 if "messages" in body else 1
        marker = specs[index]["expected_marker"]
        if index == 0:
            return httpx.Response(200, text='data: ' + json.dumps({"choices": [{"delta": {"content": marker}}]})
                                  + '\n\ndata: [DONE]\n\n')
        return httpx.Response(200, text='data: ' + json.dumps({"type": "response.output_text.delta", "delta": marker})
                              + '\n\ndata: {"type":"response.completed"}\n\n')
    result = asyncio.run(burst_probe.run_wave("http://mock", specs, transport=httpx.MockTransport(handle)))
    assert result["successful"] == 2
    assert all(row["history_units"] == 50 and row["fresh_segments"] for row in result["rows"])
    assert "private-key" not in json.dumps(result)
    assert all(s["expected_marker"] not in json.dumps(result) for s in specs)


def test_cli_records_overload_as_failure_without_overwriting_or_disclosing(tmp_path, capsys):
    keys = tmp_path / "keys.json"
    keys.write_text(json.dumps({"keys": ["private-test-key"]}))
    keys.chmod(0o600)
    output = tmp_path / "results.json"
    seen = []
    def overloaded(request):
        seen.append(request)
        return httpx.Response(503, json={"error": {"code": "analyzer_overloaded", "reason": "queue_timeout",
                                                  "message": "private-response-content"}})
    argv = ["--allow-load", "--url", "http://mock", "--keys-file", str(keys), "--output", str(output),
            "--requests", "2", "--spread-seconds", "0"]
    assert burst_probe.main(argv, transport=httpx.MockTransport(overloaded)) == 1
    report = json.loads(output.read_text())
    assert report["failed"] == 2 and len(seen) == 2
    assert report["error_reasons"] == {"queue_timeout": 2}
    assert output.stat().st_mode & 0o777 == 0o600
    for secret in ("private-test-key", "private-response-content"):
        assert secret not in output.read_text() + capsys.readouterr().out
    with pytest.raises(SystemExit):
        burst_probe.main(argv, transport=httpx.MockTransport(overloaded))
    assert len(seen) == 2
    assert json.loads(output.read_text()) == report


def test_cli_requires_explicit_load_acknowledgment(tmp_path):
    with pytest.raises(SystemExit):
        burst_probe.main(["--url", "http://mock", "--keys-file", str(tmp_path / "missing"),
                          "--output", str(tmp_path / "result")])
    assert not (tmp_path / "result").exists()


@pytest.mark.parametrize("shared, owners", [(False, 2), (True, 1)])
def test_cli_reports_active_owners_not_unused_keys(tmp_path, shared, owners):
    keys = tmp_path / "keys.json"
    keys.write_text(json.dumps({"keys": ["one", "two", "unused"]}))
    keys.chmod(0o600)
    output = tmp_path / "results.json"
    argv = ["--allow-load", "--url", "http://mock", "--keys-file", str(keys), "--output", str(output),
            "--requests", "2", "--spread-seconds", "0"] + (["--shared-key"] if shared else [])
    assert burst_probe.main(argv, transport=httpx.MockTransport(lambda _: httpx.Response(503, json={}))) == 1
    assert json.loads(output.read_text())["profile"]["owner_count"] == owners


def test_cli_rejects_readable_by_others_key_file_before_network(tmp_path):
    keys = tmp_path / "keys.json"
    keys.write_text(json.dumps({"keys": ["private"]}))
    keys.chmod(0o644)
    output = tmp_path / "results.json"
    def forbidden(request):
        raise AssertionError("Network must not be used")
    with pytest.raises(SystemExit):
        burst_probe.main(["--allow-load", "--url", "http://mock", "--keys-file", str(keys), "--output", str(output)],
                         transport=httpx.MockTransport(forbidden))
    assert not output.exists()


@pytest.mark.parametrize("url", ["file:///private/path", "http://user:private-password@mock", "http://mock?key=private"])
def test_cli_rejects_credential_bearing_or_non_http_urls(tmp_path, capsys, url):
    with pytest.raises(SystemExit):
        burst_probe.main(["--allow-load", "--url", url, "--keys-file", str(tmp_path / "missing"),
                          "--output", str(tmp_path / "result")])
    assert "private-password" not in capsys.readouterr().err
    assert not (tmp_path / "result").exists()
