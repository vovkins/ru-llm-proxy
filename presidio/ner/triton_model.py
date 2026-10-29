"""Validated CPU FP32 Triton adapter, using binary HTTP tensor transport."""
import json
import os
import ssl
import threading
from types import SimpleNamespace

import httpx
import numpy as np

from .work_budget import check_work, remaining_seconds, work_scope

try:
    from onnx_artifact import label_digest, load_export_manifest, validate_export_manifest
    from capacity import load_capacity_settings
    from model_contract import validate_triton_config
except ImportError:
    from presidio.onnx_artifact import label_digest, load_export_manifest, validate_export_manifest
    from presidio.capacity import load_capacity_settings
    from presidio.model_contract import validate_triton_config

HEADER_LIMIT = 65_536
CONFIG_LIMIT = 262_144
ERROR_LIMIT = 8_192
CONTROL_SECONDS = 5.0
INPUT_NAMES = ("input_ids", "attention_mask", "token_type_ids")


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _json(data):
    def invalid_constant(_value):
        raise ValueError("nonfinite JSON number")
    result = json.loads(data, object_pairs_hook=_unique_object, parse_constant=invalid_constant)
    if not isinstance(result, dict):
        raise ValueError("JSON object required")
    return result

class TritonModel:
    def __init__(self, settings, manifest, config, client=None, *, export_manifest=None):
        from .triton_recognizer import EXPECTED_ID2LABEL
        self.settings = settings
        self.config = SimpleNamespace(**config)
        trusted = (load_export_manifest(source_manifest=manifest) if export_manifest is None
                   else validate_export_manifest(export_manifest, manifest))
        concurrency = load_capacity_settings().concurrency_limit
        self.client = client if client is not None else httpx.Client(
            timeout=settings.timeout, trust_env=False, follow_redirects=False,
            limits=httpx.Limits(max_connections=concurrency + 1,
                               max_keepalive_connections=concurrency + 1),
            verify=ssl.create_default_context(cafile=os.getenv('SSL_CERT_FILE') or None))
        self._slots = threading.BoundedSemaphore(concurrency)
        self._lock = threading.Lock()
        self._idle = threading.Condition(self._lock)
        self._users = 0
        self._closed = False
        self._inflight = 0
        self._unconfirmed = 0
        self.path = f'/v2/models/{settings.model_name}/versions/{settings.model_version}'
        files = {entry.path: entry.sha256 for entry in manifest.files}
        self.expected = {'source_model_sha256': files['model.safetensors'],
                         'tokenizer_sha256': files['tokenizer.json'],
                         'label_contract_sha256': label_digest(EXPECTED_ID2LABEL),
                         'precision': 'fp32', 'model_profile': 'tiny2',
                         'onnx_sha256': trusted['onnx_sha256']}
        self.graph_digest = trusted['onnx_sha256']
        self.max_batch_size = 0
        try:
            self.check_contract()
        except Exception:
            self.close()
            raise

    def close(self):
        with self._idle:
            if self._closed:
                return
            self._closed = True
            while self._users:
                self._idle.wait()
        self.client.close()

    def transport_snapshot(self):
        with self._lock:
            return {"local_inflight": self._inflight,
                    "inference_completion_unconfirmed_total": self._unconfirmed}

    def _timeout(self, cap):
        check_work()
        remaining = remaining_seconds()
        limit = cap if remaining is None else min(cap, remaining)
        return httpx.Timeout(limit, connect=min(5.0, limit), pool=min(0.1, limit))

    def _request(self, method, suffix, *, limit, content=None, headers=None, binary_bytes=None):
        from .triton_recognizer import NERUnavailableError
        with self._idle:
            if self._closed:
                raise NERUnavailableError(phase="readiness", failure_class="not_ready")
            self._users += 1
        try:
            with work_scope(seconds=self.settings.timeout if method == "POST" else CONTROL_SECONDS):
                return self._request_open(method, suffix, limit=limit, content=content,
                                          headers=headers, binary_bytes=binary_bytes)
        finally:
            with self._idle:
                self._users -= 1
                self._idle.notify_all()

    def _request_open(self, method, suffix, *, limit, content=None, headers=None, binary_bytes=None):
        from .triton_recognizer import NERBackendError, NERInferenceCancelled, NERUnavailableError
        phase = "inference" if method == "POST" else "triton_readiness"
        cap = self.settings.timeout if method == "POST" else CONTROL_SECONDS
        try:
            with self.client.stream(method, self.settings.url + self.path + suffix,
                    content=content, headers={"Accept-Encoding": "identity", **(headers or {})},
                    timeout=self._timeout(cap), follow_redirects=False) as response:
                check_work()
                if response.headers.get("content-encoding", "identity").lower() != "identity":
                    raise ValueError("encoded response is not supported")
                successful = response.status_code == 200
                body_limit = limit if successful else ERROR_LIMIT
                if successful and binary_bytes is not None:
                    size = response.headers.get("Inference-Header-Content-Length", "")
                    if not size.isascii() or not size.isdigit() or len(size) > 5 or not 0 < int(size) <= HEADER_LIMIT:
                        raise ValueError("invalid inference header size")
                    body_limit = binary_bytes + int(size)
                length = response.headers.get("content-length")
                if length is not None and (not length.isascii() or not length.isdigit()
                        or len(length) > 10 or int(length) > body_limit):
                    raise ValueError("response exceeds byte limit")
                if successful and binary_bytes is not None and length is not None and int(length) != body_limit:
                    raise ValueError("invalid binary response length")
                body = bytearray()
                # Raw chunks avoid transparent decompression. A started read
                # remains bounded by its read timeout, not server cancellation.
                chunks = (response.content,) if response.is_stream_consumed else response.iter_raw()
                for chunk in chunks:
                    check_work()
                    if len(body) + len(chunk) > body_limit:
                        raise ValueError("response exceeds byte limit")
                    body.extend(chunk)
                check_work()
                if length is not None and len(body) != int(length):
                    raise ValueError("response length mismatch")
                if response.status_code in (429, 503):
                    raise NERUnavailableError(phase=phase, failure_class=(
                        "triton_overloaded" if method == "POST" else "triton_unavailable"))
                if response.status_code in (500, 502, 504):
                    raise NERUnavailableError(phase=phase, failure_class="triton_unavailable")
                if not successful:
                    raise ValueError("unexpected Triton status")
                return response.headers, bytes(body)
        except (NERBackendError, NERInferenceCancelled) as error:
            if method == "POST" and (isinstance(error, NERInferenceCancelled) or
                                     error.failure_class == "analysis_deadline_exceeded"):
                with self._lock:
                    self._unconfirmed += 1
            raise
        except httpx.PoolTimeout:
            raise NERUnavailableError(phase=phase, failure_class="triton_overloaded") from None
        except httpx.HTTPError:
            if method == "POST":
                with self._lock:
                    self._unconfirmed += 1
            check_work()
            raise NERUnavailableError(phase=phase, failure_class="triton_unavailable") from None
        except (ValueError, TypeError, KeyError, OverflowError):
            raise NERUnavailableError(phase=phase, failure_class="triton_contract_invalid") from None

    def check_contract(self):
        with work_scope(seconds=CONTROL_SECONDS):
            self._check_contract()

    def _check_contract(self):
        from .triton_recognizer import NERConfigurationError
        self._request('GET', '/ready', limit=4_096)
        headers, body = self._request('GET', '/config', limit=CONFIG_LIMIT)
        try:
            if headers.get('content-type', 'application/json').split(';')[0] != 'application/json':
                raise ValueError('Unexpected configuration format')
            config = _json(body)
            self._validate_contract(config)
        except NERConfigurationError:
            raise
        except (ValueError, TypeError, KeyError, AttributeError, RecursionError):
            raise NERConfigurationError('Triton tensor contract is invalid') from None

    def _validate_contract(self, config):
        from .triton_recognizer import NERConfigurationError
        try:
            maximum = validate_triton_config(config, self.expected)
        except ValueError as error:
            raise NERConfigurationError(str(error)) from None
        if self.max_batch_size and maximum != self.max_batch_size:
            raise NERConfigurationError('Triton maximum batch size changed')
        self.max_batch_size = maximum

    def ready(self):
        try:
            self.check_contract()
            return True
        except Exception:
            return False

    def eval(self):
        return self

    def __call__(self, **inputs):
        from .triton_recognizer import NERConfigurationError, NERUnavailableError
        check_work()
        if set(inputs) != set(INPUT_NAMES) or any(np.asarray(v).dtype != np.int64 for v in inputs.values()):
            raise NERConfigurationError('Triton requires exactly three INT64 inputs')
        arrays = {name: np.ascontiguousarray(value, dtype='<i8')
                  for name, value in inputs.items()}
        shape = arrays['input_ids'].shape
        if (len(shape) != 2 or not 1 <= shape[0] <= self.max_batch_size or
            shape[1] != 386 or any(item.shape != shape for item in arrays.values())):
            raise NERConfigurationError('Triton input shape must be batch x386')
        header = {'inputs': [{'name': name, 'shape': list(value.shape), 'datatype': 'INT64',
                              'parameters': {'binary_data_size': value.nbytes}}
                             for name, value in arrays.items()],
                  'outputs': [{'name': 'logits', 'parameters': {'binary_data': True}}]}
        remaining = remaining_seconds()
        if remaining is not None:
            header['parameters'] = {'timeout': max(1, min(60_000_000, int(remaining * 1_000_000)))}
        encoded = json.dumps(header, separators=(',', ':')).encode()
        body = encoded + b''.join(value.tobytes() for value in arrays.values())
        if not self._slots.acquire(blocking=False):
            raise NERUnavailableError(phase='inference', failure_class='triton_overloaded')
        with self._lock:
            self._inflight += 1
        try:
            expected_shape = [shape[0], shape[1], 17]
            expected_bytes = shape[0] * 386 * 17 * 4
            headers, body = self._request('POST', '/infer', limit=expected_bytes + HEADER_LIMIT,
                binary_bytes=expected_bytes, content=body,
                headers={'Content-Type': 'application/octet-stream',
                         'Inference-Header-Content-Length': str(len(encoded))})
            try:
                if headers.get('content-type', 'application/octet-stream').split(';')[0] != 'application/octet-stream':
                    raise ValueError('Unexpected tensor format')
                size_text = headers['Inference-Header-Content-Length']
                if not size_text.isascii() or not size_text.isdigit() or len(size_text) > 5:
                    raise ValueError('Invalid header size')
                size = int(size_text)
                if not 0 < size <= HEADER_LIMIT or len(body) != size + expected_bytes:
                    raise ValueError('Invalid response size')
                metadata = _json(body[:size])
                outputs = metadata['outputs']
                if (metadata.get('model_name') != self.settings.model_name or
                    metadata.get('model_version') != self.settings.model_version or
                    len(outputs) != 1 or outputs[0]['name'] != 'logits' or
                    outputs[0]['datatype'] != 'FP32' or outputs[0]['shape'] != expected_shape or
                    any(type(d) is not int for d in outputs[0]['shape']) or
                    type(outputs[0].get('parameters', {}).get('binary_data_size')) is not int or
                    outputs[0].get('parameters', {}).get('binary_data_size') != expected_bytes):
                    raise ValueError('Invalid output contract')
                logits = np.frombuffer(body, dtype='<f4', offset=size).reshape(expected_shape).copy()
                if not np.isfinite(logits).all():
                    raise ValueError('Nonfinite output')
                return SimpleNamespace(logits=logits)
            except (ValueError, TypeError, KeyError, AttributeError, IndexError, RecursionError):
                raise NERUnavailableError(phase='inference', failure_class='triton_contract_invalid') from None
        finally:
            with self._lock:
                self._inflight -= 1
            self._slots.release()
