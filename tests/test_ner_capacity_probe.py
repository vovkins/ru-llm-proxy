"""Research metrics do not convert errors into empty successful predictions."""
from pathlib import Path
import hashlib
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tests/load'))
import ner_capacity_probe as probe


def test_fresh_segments_change_every_small_fragment_not_just_tail():
    a, b = (probe.test_text(2000, serial, fresh=True).split() for serial in (1, 2))
    assert len(a) == len(b) == 2000
    for offset in range(0, 1900, 64):
        assert a[offset:offset + 64] != b[offset:offset + 64]
    assert a[0] == b[0] == 'capacity@example.test'


def test_metrics_separate_failures_and_success_percentiles():
    rows = [{'status': 200, 'seconds': .1}, {'status': 503, 'seconds': 1, 'retry_after': '2'}]
    scored = probe.summarize(rows, 2, scheduled=3)
    assert scored['scheduled'] == 3 and scored['completed'] == 2
    assert scored['successful'] == 1 and scored['successful_rps'] == .5
    assert scored['latency_ms']['p95'] == 100
    assert scored['retry_after_counts'] == {'2': 1}


def test_probe_does_not_persist_response_text_or_exception_body(monkeypatch):
    monkeypatch.setattr(probe, 'request', lambda *a, **kw: {'entities': [{'entity_type': 'PERSON'}],
                                                          'private': 'never persist'})
    value = probe.run_requests('http://local', '/api/v1/analyze', units=10, requests=2, concurrency=1)
    assert value['successful'] == 2 and 'never persist' not in str(value)
    assert value['samples'][0]['entity_counts'] == {'PERSON': 1}


def test_fresh_probe_metadata_describes_first_actual_input(monkeypatch):
    seen = []
    def fake_request(url, path, payload, **kwargs):
        seen.append(payload['text'])
        return {'entities': []}
    monkeypatch.setattr(probe, 'request', fake_request)
    value = probe.run_requests('http://local', '/api/v1/analyze', units=2000,
                               requests=2, concurrency=1, fresh=True)
    assert seen[0] != seen[1]
    assert value['characters'] == len(seen[0])
    assert value['utf8_bytes'] == len(seen[0].encode())
    assert value['text_sha256'] == hashlib.sha256(seen[0].encode()).hexdigest()


def test_transport_failure_is_unavailable_not_model_miss(monkeypatch):
    def fail(*args, **kwargs):
        raise TimeoutError('sensitive body')
    monkeypatch.setattr(probe, 'request', fail)
    value = probe.run_requests('http://local', '/api/v1/analyze', units=10, requests=1, concurrency=1)
    assert value['successful'] == 0 and value['latency_ms']['p95'] is None
    assert value['samples'][0]['error_type'] == 'TimeoutError'
    assert 'sensitive body' not in str(value)
