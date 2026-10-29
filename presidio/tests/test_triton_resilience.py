"""Bounded transport, recovery and cancellation without external providers."""
import concurrent.futures
import json
import threading
import time
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
import numpy as np
import pytest

from presidio.ner.triton_recognizer import (
    NERConfigurationError, NERInferenceCancelled, NERProcessingError,
    NERUnavailableError, TritonNERRecognizer,
)
from presidio.ner.work_budget import check_work, remaining_seconds, work_scope
from presidio.tests.test_ner import FakeTokenizer
from presidio.tests.test_triton_backend import fixture_model


def inputs():
    return {name: np.zeros((1, 386), dtype=np.int64)
            for name in ("input_ids", "attention_mask", "token_type_ids")}


class CountedStream(httpx.SyncByteStream):
    def __init__(self, chunks):
        self.chunks = iter(chunks)
        self.reads = 0
        self.closed = False

    def __iter__(self):
        for chunk in self.chunks:
            self.reads += 1
            yield chunk

    def close(self):
        self.closed = True


@pytest.mark.parametrize("suffix,status,limit", [
    ("/ready", 200, 4096), ("/config", 200, 262144),
    ("/infer", 200, 91784), ("/infer", 500, 8192),
])
@pytest.mark.parametrize("declared", [False, True])
def test_body_limit_closes_before_consuming_all(suffix, status, limit, declared):
    model, state = fixture_model()
    stream = CountedStream([b"x" * 4096] * (limit // 4096 + 10))
    state["response"] = lambda _r: httpx.Response(status, stream=stream,
        headers={"Content-Length": str(limit + 1)} if declared else {})
    with pytest.raises(NERUnavailableError) as error:
        model._request("POST" if suffix == "/infer" else "GET", suffix, limit=limit)
    assert error.value.failure_class == "triton_contract_invalid"
    assert stream.closed
    assert stream.reads == (0 if declared else limit // 4096 + 1)
    model.close()


@pytest.mark.parametrize("headers,status", [
    ({"Content-Encoding": "gzip"}, 200), ({"Content-Length": "1, 1"}, 200),
    ({"Content-Length": "-1"}, 200), ({"Location": "http://elsewhere/infer"}, 307),
])
def test_unsupported_response_does_not_follow_or_retry(headers, status):
    model, state = fixture_model()
    stream = CountedStream([b"private response body"])
    state["response"] = lambda _r: httpx.Response(status, headers=headers, stream=stream)
    before = len(state["requests"])
    with pytest.raises(NERUnavailableError) as error:
        model(**inputs())
    assert error.value.failure_class == "triton_contract_invalid"
    assert len(state["requests"]) == before + 1
    assert stream.closed


@pytest.mark.parametrize("status,kind", [(429, "triton_overloaded"), (503, "triton_overloaded"),
    (500, "triton_unavailable"), (502, "triton_unavailable"), (504, "triton_unavailable")])
def test_http_failure_is_typed_without_post_retry(status, kind, caplog):
    model, state = fixture_model()
    state["response"] = lambda _r: httpx.Response(status, content=b"SECRET_DO_NOT_LOG")
    before = len(state["requests"])
    with pytest.raises(NERUnavailableError) as error:
        model(**inputs())
    assert error.value.failure_class == kind
    assert len(state["requests"]) == before + 1
    assert "SECRET_DO_NOT_LOG" not in caplog.text


def recognizer_fixture():
    model, state = fixture_model()
    recognizer = TritonNERRecognizer(tokenizer=FakeTokenizer([(0, 4)]), model=model)
    return recognizer, model, state


def test_transient_outage_recovers_same_model_with_one_control_inference():
    recognizer, model, state = recognizer_fixture()
    state["ready"] = False
    assert not recognizer.is_ready()
    assert recognizer.failure_class() == "triton_unavailable"
    state["ready"] = True
    before = len(state["requests"])
    assert not recognizer.is_ready()  # Recovery cooldown, not a retry loop.
    assert len(state["requests"]) == before
    recognizer._next_probe_at = 0
    assert recognizer.is_ready()
    assert recognizer.failure_class() is None
    added = state["requests"][before:]
    assert [r.method for r in added] == ["GET", "GET", "POST"]
    assert recognizer._model is model
    before = len(state["requests"])
    for _ in range(10):
        assert recognizer.is_ready()
    assert len(state["requests"]) == before


def test_identity_change_is_permanent_even_if_endpoint_returns_ready():
    recognizer, _model, state = recognizer_fixture()
    state["remote"]["parameters"]["onnx_sha256"]["string_value"] = "d" * 64
    assert not recognizer.is_ready()
    assert recognizer.failure_class() == "triton_contract_invalid"
    state["remote"]["parameters"]["onnx_sha256"]["string_value"] = "c" * 64
    recognizer._next_probe_at = 0
    before = len(state["requests"])
    assert not recognizer.is_ready()
    assert len(state["requests"]) == before


def test_recovery_does_not_clear_concurrent_critical_failure(monkeypatch):
    recognizer, model, _state = recognizer_fixture()
    recognizer._mark_failed(phase="inference", failure_class="triton_unavailable", clear_components=False)
    recognizer._next_probe_at = 0
    def changed():
        recognizer._mark_failed(phase="inference", failure_class="triton_contract_invalid", clear_components=False)
    monkeypatch.setattr(model, "check_contract", changed)
    assert not recognizer.is_ready()
    assert recognizer.failure_class() == "triton_contract_invalid"
    recognizer._mark_failed(phase="inference", failure_class="triton_unavailable", clear_components=False)
    assert recognizer.failure_class() == "triton_contract_invalid"


def test_concurrent_health_checks_do_not_queue_probes(monkeypatch):
    recognizer, model, _state = recognizer_fixture()
    entered, release = threading.Event(), threading.Event()
    def probe():
        entered.set()
        assert release.wait(2)
    monkeypatch.setattr(model, "check_contract", probe)
    with concurrent.futures.ThreadPoolExecutor(8) as executor:
        first = executor.submit(recognizer.is_ready)
        assert entered.wait(1)
        futures = [executor.submit(recognizer.is_ready) for _ in range(20)]
        assert all(f.result(1) for f in futures)
        release.set()
        assert first.result(1)


@pytest.mark.parametrize("failure", ["triton_overloaded", "analysis_deadline_exceeded"])
def test_request_failure_does_not_poison_ready(failure):
    recognizer, _model, _state = recognizer_fixture()
    recognizer._mark_failed_if_service_wide(NERProcessingError(phase="inference", failure_class=failure))
    assert recognizer.is_ready()


def test_cancellation_during_transport_closes_response_not_shared_client():
    model, state = fixture_model()
    event = threading.Event()
    def chunks():
        event.set()
        yield b"ignored"
    stream = CountedStream(chunks())
    state["response"] = lambda _r: httpx.Response(200, stream=stream,
        headers={"Inference-Header-Content-Length": "100"})
    with work_scope(seconds=1, cancellation=event), pytest.raises(NERInferenceCancelled):
        model(**inputs())
    assert stream.closed
    assert not model.client.is_closed
    assert model.transport_snapshot() == {"local_inflight": 0, "inference_completion_unconfirmed_total": 1}
    state["response"] = None
    assert model(**inputs()).logits.shape == (1, 386, 17)


def test_budget_does_not_restart_at_each_batch():
    model, state = fixture_model()
    with work_scope(seconds=0.03):
        model(**inputs())
        remaining = remaining_seconds()
        time.sleep(0.04)
        before = len(state["requests"])
        with pytest.raises(NERProcessingError) as error:
            model(**inputs())
        assert error.value.failure_class == "analysis_deadline_exceeded"
        assert remaining < 0.03
        assert len(state["requests"]) == before


def test_cancellation_only_scope_has_no_infinite_network_parameter():
    model, state = fixture_model()
    with work_scope(cancellation=threading.Event()):
        assert remaining_seconds() is None
        assert model(**inputs()).logits.shape == (1, 386, 17)
    assert "parameters" not in state["last_inputs"]


@contextmanager
def http_peer(handler):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass
        def do_POST(self):
            self.rfile.read(int(self.headers["Content-Length"]))
            handler(self)
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(2)


def real_client(model, url):
    model.client.close()
    model.client = httpx.Client(trust_env=False, limits=httpx.Limits(max_connections=1))
    from dataclasses import replace
    model.settings = replace(model.settings, url=url, timeout=0.2)


def test_slow_drip_is_bounded_by_total_budget_not_read_timeout():
    model, _state = fixture_model()
    def drip(peer):
        peer.send_response(200)
        peer.send_header("Inference-Header-Content-Length", "100")
        peer.end_headers()
        try:
            for _ in range(100):
                peer.wfile.write(b"x")
                peer.wfile.flush()
                time.sleep(0.01)
        except (BrokenPipeError, ConnectionResetError):
            pass
    with http_peer(drip) as url:
        real_client(model, url)
        started = time.monotonic()
        with work_scope(seconds=0.06), pytest.raises(NERProcessingError):
            model(**inputs())
        assert time.monotonic() - started < 0.4
        assert not model.client.is_closed
        assert model.transport_snapshot()["inference_completion_unconfirmed_total"] == 1
    model.close()


def test_inflight_slot_is_not_released_early_and_close_waits():
    model, _state = fixture_model()
    entered, release = threading.Event(), threading.Event()
    def blocked(peer):
        entered.set()
        release.wait(1)
        peer.send_response(503)
        peer.send_header("Content-Length", "0")
        peer.end_headers()
    with http_peer(blocked) as url:
        real_client(model, url)
        with concurrent.futures.ThreadPoolExecutor(2) as executor:
            first = executor.submit(lambda: model(**inputs()))
            assert entered.wait(1)
            with pytest.raises(NERUnavailableError) as error:
                model(**inputs())
            assert error.value.failure_class == "triton_overloaded"
            assert model.transport_snapshot()["local_inflight"] == 1
            closed = executor.submit(model.close)
            time.sleep(0.02)
            assert not closed.done()
            release.set()
            with pytest.raises(NERUnavailableError):
                first.result(1)
            closed.result(1)
            model.close()
            assert model.client.is_closed


def test_socket_stall_is_bounded_and_a_pool_wait_is_not_an_extra_queue():
    model, _state = fixture_model()
    entered, release = threading.Event(), threading.Event()
    def stalled(peer):
        entered.set()
        release.wait(1)
    with http_peer(stalled) as url:
        real_client(model, url)
        with concurrent.futures.ThreadPoolExecutor(1) as executor:
            first = executor.submit(lambda: model(**inputs()))
            assert entered.wait(1)
            started = time.monotonic()
            with pytest.raises(NERUnavailableError) as error:
                model._request("GET", "/ready", limit=4096)
            assert error.value.failure_class == "triton_overloaded"
            assert time.monotonic() - started < 0.3
            with pytest.raises((NERUnavailableError, NERProcessingError)):
                first.result(0.5)
            release.set()
        assert model.transport_snapshot()["local_inflight"] == 0
        assert model.transport_snapshot()["inference_completion_unconfirmed_total"] == 1


def test_parallel_inference_has_no_global_serialization(monkeypatch):
    monkeypatch.setenv("PRESIDIO_ANALYZER_CONCURRENCY_LIMIT", "2")
    model, state = fixture_model()
    barrier = threading.Barrier(2)
    def parallel_response(_request):
        barrier.wait(timeout=1)
        return httpx.Response(503)
    state["response"] = parallel_response
    def call():
        with pytest.raises(NERUnavailableError) as error:
            model(**inputs())
        return error.value.failure_class
    with concurrent.futures.ThreadPoolExecutor(2) as executor:
        assert list(executor.map(lambda _n: call(), range(2))) == ["triton_overloaded"] * 2
    assert model.transport_snapshot()["local_inflight"] == 0


def test_connection_loss_is_unconfirmed_without_retry():
    model, state = fixture_model()
    def disconnected(request):
        raise httpx.ReadError("private upstream error", request=request)
    state["response"] = disconnected
    before = len(state["requests"])
    with pytest.raises(NERUnavailableError) as error:
        model(**inputs())
    assert error.value.failure_class == "triton_unavailable"
    assert len(state["requests"]) == before + 1
    assert model.transport_snapshot()["inference_completion_unconfirmed_total"] == 1


def test_control_warmup_failure_never_publishes_ready():
    recognizer, model, state = recognizer_fixture()
    recognizer._mark_failed(phase="inference", failure_class="triton_unavailable", clear_components=False)
    recognizer._next_probe_at = 0
    handle = model.client._transport.handler
    # Avoid recursive fixture overrides for the successful config/ready calls.
    def control(request):
        if request.method == "POST":
            return httpx.Response(503)
        state["response"] = None
        try:
            return handle(request)
        finally:
            state["response"] = control
    state["response"] = control
    assert not recognizer.is_ready()
    assert recognizer.failure_class() == "triton_unavailable"
    state["response"] = None
    recognizer._next_probe_at = 0
    assert recognizer.is_ready()


@pytest.mark.parametrize("change", ["duplicate", "bool_shape", "nan_json", "wrong_size"])
def test_ambiguous_output_metadata_is_critical(change):
    def changed(metadata, binary):
        if change == "bool_shape":
            metadata["outputs"][0]["shape"][0] = True
        elif change == "nan_json":
            metadata["extra"] = float("nan")
        elif change == "wrong_size":
            metadata["outputs"][0]["parameters"]["binary_data_size"] = False
        return metadata, binary
    model, state = fixture_model(changed)
    if change == "duplicate":
        metadata = b'{"model_name":"tiny2","model_name":"tiny2"}'
        state["response"] = lambda _r: httpx.Response(200,
            content=metadata + b"\0" * (386 * 17 * 4),
            headers={"Inference-Header-Content-Length": str(len(metadata))})
    with pytest.raises(NERUnavailableError) as error:
        model(**inputs())
    assert error.value.failure_class == "triton_contract_invalid"


@pytest.mark.parametrize("change", ["missing", "extra", "float", "shape"])
def test_invalid_local_tensors_are_rejected_without_post(change):
    model, state = fixture_model()
    values = inputs()
    if change == "missing":
        values.pop("attention_mask")
    elif change == "extra":
        values["extra"] = values["input_ids"]
    elif change == "float":
        values["input_ids"] = values["input_ids"].astype(np.float32)
    else:
        values["input_ids"] = np.zeros((1, 385), dtype=np.int64)
    before = len(state["requests"])
    with pytest.raises(NERConfigurationError):
        model(**values)
    assert len(state["requests"]) == before


def test_cancelled_multibatch_request_stops_before_next_post(monkeypatch):
    model, state = fixture_model()
    recognizer = TritonNERRecognizer(tokenizer=FakeTokenizer([(i,i+1) for i in range(1600)]),
                                    model=model, inference_batch_size=1)
    assert recognizer.is_ready()
    event = threading.Event()
    original = model._request
    def stop_after_first(method, *args, **kwargs):
        result = original(method, *args, **kwargs)
        if method == "POST":
            event.set()
        return result
    monkeypatch.setattr(model, "_request", stop_after_first)
    before = sum(r.method == "POST" for r in state["requests"])
    with pytest.raises(NERInferenceCancelled):
        recognizer.analyze("x" * 1600, cancellation_event=event)
    assert sum(r.method == "POST" for r in state["requests"]) == before + 1
    assert recognizer.is_ready()
