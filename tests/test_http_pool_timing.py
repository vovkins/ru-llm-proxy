"""The diagnostic trace must distinguish pre-send, setup and backend time."""
import asyncio
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent / "load"))
from http_pool_timing import RequestPoolTiming  # noqa: E402


def trace_case(events):
    now = [0.0]
    timing = RequestPoolTiming(clock=lambda: now[0])
    async def run():
        for at, name in events:
            now[0] = at
            await timing.trace(name, {"headers": "private-value", "exception": "private-value"})
    asyncio.run(run())
    assert "private-value" not in json.dumps(timing.__dict__, default=str)
    return timing.summary()


def test_keepalive_pre_send_is_not_backend_latency():
    result = trace_case([(2, "http11.send_request_headers.started"),
                         (7, "http11.receive_response_headers.complete")])
    assert result["pool_wait_upper_seconds"] == 2
    assert result["connection_setup_seconds"] == 0
    assert result["after_send_to_headers_seconds"] == 5
    assert result["pure_pool_wait_measured"] is False


def test_tcp_and_tls_time_are_not_counted_as_pool_wait():
    result = trace_case([(1, "connection.connect_tcp.started"),
        (2, "connection.connect_tcp.complete"), (2, "connection.start_tls.started"),
        (3, "connection.start_tls.complete"), (4, "http11.send_request_headers.started")])
    assert result["connection_setup_seconds"] == 2
    assert result["pool_wait_upper_seconds"] == 2


def test_missing_or_failed_trace_is_not_reported_as_zero_wait():
    assert trace_case([])["pool_wait_upper_seconds"] is None
    for tail in ([], [(2, "connection.connect_tcp.failed")]):
        result = trace_case([(1, "connection.connect_tcp.started")] + tail)
        assert result["connection_setup_seconds"] is None
        assert result["pool_wait_upper_seconds"] is None


def test_repeated_setup_is_summed_and_unpaired_events_are_unknown():
    result = trace_case([(1, "connection.connect_tcp.started"),
        (2, "connection.connect_tcp.complete"), (3, "connection.connect_tcp.started"),
        (4, "connection.connect_tcp.complete"), (5, "http11.send_request_headers.started")])
    assert result["connection_setup_events"] == 2
    assert result["pool_wait_upper_seconds"] == 3
    result = trace_case([(1, "connection.connect_tcp.complete"),
                        (2, "http11.send_request_headers.started")])
    assert result["pool_wait_upper_seconds"] is None


def test_unknown_events_and_later_headers_do_not_replace_first_send():
    result = trace_case([(1, "private-event"), (2, "http11.send_request_headers.started"),
        (3, "http11.send_request_headers.started"), (4, "http11.receive_response_headers.complete"),
        (8, "http11.receive_response_headers.complete")])
    assert result["pre_send_seconds"] == 2
    assert result["after_send_to_headers_seconds"] == 2
