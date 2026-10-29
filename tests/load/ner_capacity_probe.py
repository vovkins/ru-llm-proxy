"""Internal research routes and bounded CPU measurements; never a product mode."""
from __future__ import annotations

from collections import Counter
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from pathlib import Path
import sys
import time
import urllib.error
import urllib.request

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT), str(ROOT / 'tests/load')]
from load_support import percentile, synthetic_text


def test_text(units, serial=0, *, fresh=False):
    """Keep new segments distinguishable, not just the last word of a prompt."""
    text = synthetic_text(units, marker='capacity@example.test', seed=0)
    if not fresh:
        return text
    words = text.split()
    for index in range(16, len(words) - 2, 32):
        words[index:index + 3] = ['фрагмент', str(serial), str(index)]
    return ' '.join(words)


def request(url, path, payload=None, *, timeout=1200):
    data = json.dumps(payload, ensure_ascii=False).encode() if payload is not None else None
    req = urllib.request.Request(url.rstrip('/') + path, data=data,
                                 headers={'Content-Type': 'application/json'})
    with urllib.request.urlopen(req, timeout=timeout) as response:
        return json.load(response)


def summarize(rows, elapsed, *, scheduled=None):
    ok = [r for r in rows if r['status'] == 200]
    return {'scheduled': len(rows) if scheduled is None else scheduled, 'completed': len(rows),
            'successful': len(ok), 'http_statuses': dict(Counter(str(r['status']) for r in rows)),
            'elapsed_seconds': elapsed, 'successful_rps': len(ok) / elapsed if elapsed else None,
            'latency_ms': {f'p{p}': percentile([r['seconds'] * 1000 for r in ok], p / 100)
                           for p in (50, 95, 99)},
            'retry_after_counts': dict(Counter(r.get('retry_after') for r in rows if r.get('retry_after'))),
            'samples': rows}


def run_requests(url, path, *, units, requests, concurrency, raw_batch=None, fresh=False, timeout=1200):
    if not 1 <= units <= 1_000_000 or not 1 <= requests <= 4096 or not 1 <= concurrency <= 64:
        raise ValueError('Probe sizes, request count or concurrency are outside research bounds')
    def one(index):
        text = test_text(units, index, fresh=fresh)
        payload = {'text': text, 'language': 'ru', 'score_threshold': .35}
        if raw_batch is not None:
            payload['batch_size'] = raw_batch
        started = time.perf_counter()
        result = {'index': index}
        try:
            data = request(url, path, payload, timeout=timeout)
            result.update(status=200, entity_counts=dict(Counter(e['entity_type'] for e in data.get('entities', []))))
            if raw_batch is not None:
                result['inference'] = data
        except urllib.error.HTTPError as exc:
            result.update(status=exc.code, retry_after=exc.headers.get('Retry-After'))
            try:
                body = json.load(exc)
                detail = body.get('detail', {})
                if isinstance(detail, dict):
                    result['error_type'] = detail.get('failure_class') or detail.get('error_type')
            except (ValueError, OSError):
                pass
        except (OSError, TimeoutError, ValueError) as exc:
            result.update(status='client_error', error_type=type(exc).__name__)
        result['seconds'] = time.perf_counter() - started
        return result

    started = time.perf_counter()
    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        rows = list(pool.map(one, range(requests)))
    result = summarize(rows, time.perf_counter() - started)
    text = test_text(units, fresh=fresh)
    result.update(units=units, generator_unit='whitespace_token', characters=len(text),
                  utf8_bytes=len(text.encode()), text_sha256=hashlib.sha256(text.encode()).hexdigest(),
                  concurrency=concurrency, raw_batch=raw_batch, fresh_segments=fresh,
                  arrival_model='closed_loop_fixed_count')
    return result


def serve():
    import analyzer_server as server
    from fastapi import HTTPException
    from ner.text_processing import normalize_for_ner
    import uvicorn

    @server.app.post('/__capacity__/prepare')
    def prepare(payload: dict):
        text = payload['text']
        if len(text) > 1_000_000:
            raise HTTPException(422, detail={'error_type': 'prepare_too_large_use_analyzer_metrics'})
        started = time.perf_counter()
        normalized = normalize_for_ner(text).text
        normalization = time.perf_counter() - started
        started = time.perf_counter()
        ids, _, windows = server.ner_recognizer._prepare_window_input(normalized)
        return {'characters': len(text), 'utf8_bytes': len(text.encode()), 'ner_tokens': len(ids),
                'planned_windows': len(windows), 'normalization_seconds': normalization,
                'tokenization_windowing_seconds': time.perf_counter() - started,
                'text_sha256': hashlib.sha256(text.encode()).hexdigest()}

    @server.app.post('/__capacity__/triton')
    def triton(payload: dict):
        if server.ner_recognizer.runtime_info().get('backend') != 'triton':
            raise HTTPException(422, detail={'error_type': 'not_triton'})
        import numpy as np
        batch = payload.get('batch_size', 1)
        if type(batch) is not int or not 1 <= batch <= 32:
            raise HTTPException(422, detail={'error_type': 'invalid_batch'})
        recognizer = server.ner_recognizer
        ids, _, windows = recognizer._prepare_window_input(normalize_for_ner(payload['text']).text)
        if not windows:
            raise HTTPException(422, detail={'error_type': 'empty_input'})
        window = windows[0]
        content = ids[window.start_token:window.end_token]
        tokenizer = recognizer._tokenizer
        values = tokenizer.build_inputs_with_special_tokens(content)
        length = len(values)
        values += [tokenizer.pad_token_id] * (386 - length)
        inputs = {'input_ids': np.asarray([values] * batch, dtype=np.int64),
                  'attention_mask': np.asarray([[1] * length + [0] * (386 - length)] * batch, dtype=np.int64),
                  'token_type_ids': np.zeros((batch, 386), dtype=np.int64)}
        # Measure the server, not the adapter's intentional Analyzer admission limit.
        metadata = {'inputs': [{'name': name, 'shape': list(value.shape), 'datatype': 'INT64',
                                 'parameters': {'binary_data_size': value.nbytes}}
                                for name, value in inputs.items()],
                    'outputs': [{'name': 'logits', 'parameters': {'binary_data': True}}]}
        header = json.dumps(metadata, separators=(',', ':')).encode()
        body = header + b''.join(value.astype('<i8', copy=False).tobytes() for value in inputs.values())
        req = urllib.request.Request('http://nginx:8000/v2/models/tiny2/versions/1/infer', data=body,
            headers={'Content-Type': 'application/octet-stream', 'Inference-Header-Content-Length': str(len(header))})
        started = time.perf_counter()
        with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(req, timeout=60) as response:
            size = int(response.headers['Inference-Header-Content-Length'])
            if not 0 < size <= 65536:
                raise ValueError('Invalid research response header size')
            expected = batch * 386 * 17 * 4
            data = response.read(size + expected + 1)
        output = json.loads(data[:size])['outputs'][0]
        if len(data) != size + expected or output['shape'] != [batch, 386, 17]:
            raise ValueError('Unexpected research tensor shape/size')
        return {'batch': batch, 'content_tokens_per_window': len(content),
                'logits_shape': output['shape'], 'model_http_seconds': time.perf_counter() - started}

    # Only a disposable, network-isolated test process exposes these routes.
    uvicorn.run(server.app, host='0.0.0.0', port=5001, access_log=False, log_level='warning')


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=('serve', 'run'))
    parser.add_argument('--url')
    parser.add_argument('--path', default='/api/v1/analyze')
    parser.add_argument('--units', type=int, default=1000)
    parser.add_argument('--requests', type=int, default=8)
    parser.add_argument('--concurrency', type=int, default=1)
    parser.add_argument('--raw-batch', type=int)
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    if args.command == 'serve':
        serve()
    else:
        value = run_requests(args.url, args.path, units=args.units, requests=args.requests,
                             concurrency=args.concurrency, raw_batch=args.raw_batch)
        args.output.write_text(json.dumps(value, indent=2) + '\n')
