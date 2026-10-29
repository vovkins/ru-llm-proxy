"""Transport, identity and safe-failure checks for the optional Triton backend."""
import json
from dataclasses import replace
from types import SimpleNamespace

import httpx
import numpy as np
import pytest

from presidio.model_artifact import ModelFile, ModelManifest
from presidio.model_profiles import PROFILES
from presidio.ner.backend_settings import BackendSettings, resolve_backend
from presidio.ner.triton_recognizer import (
    EXPECTED_ID2LABEL, TritonNERRecognizer, NERConfigurationError, NERUnavailableError)
from presidio.ner.triton_model import TritonModel, label_digest
from presidio.model_contract import INPUT_NAMES, MODEL_ID, MODEL_REVISION, MODEL_VARIANT


def fixture_model(handler_change=None, remote_change=None):
    manifest = ModelManifest(PROFILES['tiny2'].model_id, PROFILES['tiny2'].revision,
        'BertForTokenClassification', 'test', 'test', (
            ModelFile('model.safetensors', 1, 'a' * 64),
            ModelFile('tokenizer.json', 1, 'b' * 64)))
    config = {'architectures': ['BertForTokenClassification'], 'id2label': EXPECTED_ID2LABEL}
    params = {'source_model_sha256': 'a' * 64, 'tokenizer_sha256': 'b' * 64,
              'label_contract_sha256': label_digest(EXPECTED_ID2LABEL), 'precision': 'fp32',
              'model_profile': 'tiny2', 'onnx_sha256': 'c' * 64}
    remote = {'backend': 'onnxruntime', 'max_batch_size': 32,
              'parameters': {key: {'string_value': value} for key, value in params.items()},
              'input': [{'name': name, 'data_type': 'TYPE_INT64', 'dims': [386]} for name in
                        ['input_ids', 'attention_mask', 'token_type_ids']],
              'output': [{'name': 'logits', 'data_type': 'TYPE_FP32', 'dims': [386, 17]}],
              'instance_group': [{'kind': 'KIND_CPU'}]}
    if remote_change:
        remote_change(remote)
    state = {'ready': True, 'remote': remote, 'last_inputs': None, 'requests': [], 'response': None}

    def handle(request):
        state['requests'].append(request)
        if state['response'] is not None:
            return state['response'](request)
        if request.url.path.endswith('/ready'):
            return httpx.Response(200 if state['ready'] else 503)
        if request.url.path.endswith('/config'):
            return httpx.Response(200, json=remote)
        size = int(request.headers['Inference-Header-Content-Length'])
        header = json.loads(request.content[:size])
        shape = header['inputs'][0]['shape']
        state['last_inputs'] = header
        logits = np.zeros([*shape, 17], dtype='<f4')
        logits[..., 0] = 5.
        out = {'model_name': 'tiny2', 'model_version': '1', 'outputs': [
            {'name': 'logits', 'datatype': 'FP32', 'shape': list(logits.shape),
             'parameters': {'binary_data_size': logits.nbytes}}]}
        data = logits.tobytes()
        if handler_change:
            out, data = handler_change(out, data)
        encoded = json.dumps(out).encode()
        return httpx.Response(200, content=encoded + data,
                              headers={'Inference-Header-Content-Length': str(len(encoded))})
    client = httpx.Client(transport=httpx.MockTransport(handle))
    trusted = dict(schema_version=2, model_profile='tiny2', precision='fp32',
        source_model_id=MODEL_ID, source_revision=MODEL_REVISION, source_variant=MODEL_VARIANT,
        source_model_sha256='a'*64, tokenizer_sha256='b'*64, onnx_sha256='c'*64,
        label_contract_sha256=label_digest(EXPECTED_ID2LABEL), sequence_length=386,
        max_batch_size=32, input_names=list(INPUT_NAMES), opset=17,
        source_files=[dict(path=f.path, size=f.size, sha256=f.sha256) for f in manifest.files],
        versions={name:'test' for name in ('python','torch','transformers','onnx','onnxruntime','tokenizers')})
    model = TritonModel(BackendSettings(backend='triton', url='http://test'), manifest,
                        config, client, export_manifest=trusted)
    return model, state


def test_default_backend_is_triton():
    assert resolve_backend({}).backend == 'triton'


@pytest.mark.parametrize('changes', [
    {'PRESIDIO_ANALYZER_NER_BACKEND': 'typo'},
    {'PRESIDIO_ANALYZER_TRITON_URL': 'http://user:secret@server'},
    {'PRESIDIO_ANALYZER_TRITON_MODEL_NAME': '../model'},
    {'PRESIDIO_ANALYZER_TRITON_MODEL_VERSION': 'latest'},
    {'PRESIDIO_ANALYZER_TRITON_TIMEOUT_SECONDS': 'nan'},
])
def test_reject_invalid_backend_settings(changes):
    with pytest.raises(ValueError):
        resolve_backend({'PRESIDIO_ANALYZER_NER_BACKEND': 'triton', **changes})


def test_binary_transport_preserves_shapes_and_logits():
    model, state = fixture_model()
    inputs = {name: np.zeros((2, 386), dtype=np.int64) for name in
              ['input_ids', 'attention_mask', 'token_type_ids']}
    result = model(**inputs).logits
    assert tuple(result.shape) == (2, 386, 17)
    assert result.dtype == np.float32
    assert np.all(result[..., 0] == 5)
    assert state['last_inputs']['inputs'][0]['parameters']['binary_data_size'] == 2 * 386 * 8


@pytest.mark.parametrize('alteration', ['precision', 'weights', 'labels', 'gpu', 'graph'])
def test_ready_rejects_remote_contract_changes(alteration):
    model, state = fixture_model()
    if alteration == 'gpu':
        state['remote']['instance_group'][0]['kind'] = 'KIND_GPU'
    else:
        key = {'precision': 'precision', 'weights': 'source_model_sha256',
               'labels': 'label_contract_sha256', 'graph': 'onnx_sha256'}[alteration]
        state['remote']['parameters'][key]['string_value'] = 'd' * 64
    assert not model.ready()


def test_first_server_graph_must_match_build_produced_metadata():
    def wrong_graph(remote):
        remote['parameters']['onnx_sha256']['string_value'] = 'd' * 64

    with pytest.raises(NERConfigurationError, match='identity'):
        fixture_model(remote_change=wrong_graph)


@pytest.mark.parametrize('alteration', ['shape', 'dtype', 'name', 'version', 'short', 'nan'])
def test_bad_response_fails_closed(alteration):
    def change(out, data):
        if alteration == 'short':
            data = data[:-4]
        elif alteration == 'nan':
            data = np.full((1, 386, 17), np.nan, dtype='<f4').tobytes()
        elif alteration in {'name', 'version'}:
            out['model_name' if alteration == 'name' else 'model_version'] = 'wrong'
        elif alteration == 'shape':
            out['outputs'][0]['shape'][-1] = 16
        else:
            out['outputs'][0]['datatype'] = 'FP16'
        return out, data
    model, _ = fixture_model(change)
    inputs = {name: np.zeros((1, 386), dtype=np.int64) for name in
              ['input_ids', 'attention_mask', 'token_type_ids']}
    with pytest.raises(NERUnavailableError) as error:
        model(**inputs)
    assert error.value.failure_class == 'triton_contract_invalid'


def test_local_execution_is_not_a_supported_backend():
    with pytest.raises(ValueError, match="obsolete NER settings"):
        resolve_backend({"PRESIDIO_ANALYZER_NER_BACKEND": "transformers"})


def test_loader_uses_only_verified_tokenizer_and_triton(monkeypatch, tmp_path):
    from presidio.ner import triton_recognizer as module
    from presidio.tests.test_ner import FakeTokenizer
    model, state = fixture_model()
    monkeypatch.setattr("presidio.ner.triton_model.TritonModel", lambda *_args: model)
    (tmp_path / "config.json").write_text(json.dumps({"vocab_size": 3}))
    monkeypatch.setattr(module, "load_and_verify_runtime_artifact", lambda _path: SimpleNamespace(
        model_id=PROFILES["tiny2"].model_id, revision=PROFILES["tiny2"].revision,
        architecture="BertForTokenClassification"))
    monkeypatch.setattr("presidio.ner.tokenization.ContentTokenizer", lambda _p: FakeTokenizer([(0, 4)]))
    recognizer = TritonNERRecognizer(model_directory=tmp_path)
    recognizer.load_model()
    assert recognizer.is_ready()
    assert state["last_inputs"]["inputs"][0]["shape"] == [1, 386]
    state["ready"] = False
    recognizer._next_probe_at = 0
    with pytest.raises(NERUnavailableError):
        recognizer.require_ready()
    assert recognizer.state() == "failed"
