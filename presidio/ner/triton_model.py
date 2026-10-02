"""Validated CPU FP32 Triton adapter, using binary HTTP tensor transport."""
import hashlib
import json
import re
import os
import ssl
from types import SimpleNamespace

import httpx
import numpy as np


def label_digest(id2label):
    labels = [id2label[i] for i in range(len(id2label))]
    return hashlib.sha256(json.dumps(labels, separators=(',', ':')).encode()).hexdigest()


class TritonModel:
    def __init__(self, settings, manifest, config, client=None):
        from .huggingface_recognizer import EXPECTED_ID2LABEL
        self.settings = settings
        self.config = SimpleNamespace(**config)
        self.client = client or httpx.Client(timeout=settings.timeout, trust_env=False,
            verify=ssl.create_default_context(cafile=os.getenv('SSL_CERT_FILE') or None))
        self.path = f'/v2/models/{settings.model_name}/versions/{settings.model_version}'
        files = {entry.path: entry.sha256 for entry in manifest.files}
        self.expected = {'source_model_sha256': files['model.safetensors'],
                         'tokenizer_sha256': files['tokenizer.json'],
                         'label_contract_sha256': label_digest(EXPECTED_ID2LABEL),
                         'precision': 'fp32', 'model_profile': 'tiny2'}
        self.graph_digest = None
        self.max_batch_size = 0
        try:
            self._check_contract()
        except Exception:
            self.client.close()
            raise

    def _get(self, suffix):
        response = self.client.get(self.settings.url + self.path + suffix,
                                   timeout=min(self.settings.timeout, 5.))
        response.raise_for_status()
        return response

    def _check_contract(self):
        from .huggingface_recognizer import NERConfigurationError
        self._get('/ready')
        config = self._get('/config').json()
        parameters = config.get('parameters', {})
        actual = {key: value.get('string_value') for key, value in parameters.items()
                  if isinstance(value, dict)}
        if any(actual.get(key) != value for key, value in self.expected.items()):
            raise NERConfigurationError('Triton model identity does not match verified local Tiny2 artifact')
        graph = actual.get('onnx_sha256', '')
        if not re.fullmatch(r'[0-9a-f]{64}', graph) or (self.graph_digest and graph != self.graph_digest):
            raise NERConfigurationError('Triton graph identity changed or is missing')
        inputs = {item.get('name'): item for item in config.get('input', [])}
        outputs = config.get('output', [])
        if (config.get('backend') != 'onnxruntime' or
            set(inputs) != {'input_ids', 'attention_mask', 'token_type_ids'} or
            any(item.get('data_type') != 'TYPE_INT64' or item.get('dims') != [-1] for item in inputs.values()) or
            len(outputs) != 1 or outputs[0].get('name') != 'logits' or
            outputs[0].get('data_type') != 'TYPE_FP32' or outputs[0].get('dims') != [-1, 17]):
            raise NERConfigurationError('Triton tensor contract is invalid')
        instances = config.get('instance_group', [])
        if not instances or any(group.get('kind') != 'KIND_CPU' for group in instances):
            raise NERConfigurationError('Triton profile requires CPU instances')
        maximum = config.get('max_batch_size')
        if type(maximum) is not int or not 1 <= maximum <= 32:
            raise NERConfigurationError('Triton maximum batch size is invalid')
        self.max_batch_size = maximum
        self.graph_digest = graph

    def ready(self):
        try:
            self._check_contract()
            return True
        except Exception:
            return False

    def eval(self):
        return self

    def __call__(self, **inputs):
        import torch
        from .huggingface_recognizer import NERConfigurationError, NERUnavailableError
        arrays = {name: np.ascontiguousarray(value.detach().cpu().numpy(), dtype='<i8')
                  for name, value in inputs.items()}
        shape = arrays['input_ids'].shape
        if (len(shape) != 2 or not 1 <= shape[0] <= self.max_batch_size or
            shape[1] != 386 or any(item.shape != shape for item in arrays.values())):
            raise NERConfigurationError('Triton input shape must be batch x386')
        header = {'inputs': [{'name': name, 'shape': list(value.shape), 'datatype': 'INT64',
                              'parameters': {'binary_data_size': value.nbytes}}
                             for name, value in arrays.items()],
                  'outputs': [{'name': 'logits', 'parameters': {'binary_data': True}}]}
        encoded = json.dumps(header, separators=(',', ':')).encode()
        body = encoded + b''.join(value.tobytes() for value in arrays.values())
        try:
            response = self.client.post(self.settings.url + self.path + '/infer', content=body,
                headers={'Content-Type': 'application/octet-stream',
                         'Inference-Header-Content-Length': str(len(encoded))})
            response.raise_for_status()
            size = int(response.headers['Inference-Header-Content-Length'])
            if not 0 < size <= 65536 or size > len(response.content):
                raise ValueError('Invalid header size')
            metadata = json.loads(response.content[:size])
            outputs = metadata['outputs']
            expected_shape = [shape[0], shape[1], 17]
            if (metadata.get('model_name') != self.settings.model_name or
                metadata.get('model_version') != self.settings.model_version or
                len(outputs) != 1 or outputs[0]['name'] != 'logits' or
                outputs[0]['datatype'] != 'FP32' or outputs[0]['shape'] != expected_shape):
                raise ValueError('Invalid output contract')
            binary = response.content[size:]
            expected_bytes = int(np.prod(expected_shape)) * 4
            if (len(binary) != expected_bytes or
                outputs[0].get('parameters', {}).get('binary_data_size') != expected_bytes):
                raise ValueError('Invalid output byte count')
            logits = np.frombuffer(binary, dtype='<f4').reshape(expected_shape).copy()
            if not np.isfinite(logits).all():
                raise ValueError('Nonfinite output')
            return SimpleNamespace(logits=torch.from_numpy(logits))
        except Exception as exc:
            # Do not log the response/body: tensors may encode sensitive text.
            raise NERUnavailableError(phase='inference', failure_class='triton_inference_failed') from exc
