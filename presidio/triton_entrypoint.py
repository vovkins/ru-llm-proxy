"""Verify immutable ONNX bytes, generate bounded CPU config, run Triton."""
import hashlib
import json
import os
from pathlib import Path
import re


def configure(root=Path('/models')):
    directory = root / 'tiny2'
    manifest = json.loads((directory / 'export-manifest.json').read_text())
    if manifest.get('schema_version') != 1 or manifest.get('precision') != 'fp32' or manifest.get('model_profile') != 'tiny2':
        raise ValueError('Invalid export manifest')
    graph = directory / '1/model.onnx'
    digest = hashlib.sha256()
    with graph.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(block)
    if digest.hexdigest() != manifest.get('onnx_sha256'):
        raise ValueError('ONNX checksum mismatch')
    instances = int(os.getenv('TRITON_MODEL_INSTANCES', '1'))
    if not 1 <= instances <= 32:
        raise ValueError('TRITON_MODEL_INSTANCES must be between 1 and 32')
    parameters = []
    for key in ['source_model_sha256', 'tokenizer_sha256', 'label_contract_sha256', 'onnx_sha256']:
        value = manifest.get(key, '')
        if not isinstance(value, str) or not re.fullmatch(r'[0-9a-f]{64}', value):
            raise ValueError('Invalid export identity')
        parameters.append(f'parameters {{ key: "{key}" value: {{ string_value: "{value}" }} }}')
    parameters += ['parameters { key: "precision" value: { string_value: "fp32" } }',
                   'parameters { key: "model_profile" value: { string_value: "tiny2" } }']
    config = '''name: "tiny2"
backend: "onnxruntime"
max_batch_size: 32
version_policy { specific { versions: [1] } }
input [
 { name: "input_ids" data_type: TYPE_INT64 dims: [-1] },
 { name: "attention_mask" data_type: TYPE_INT64 dims: [-1] },
 { name: "token_type_ids" data_type: TYPE_INT64 dims: [-1] }
]
output [ { name: "logits" data_type: TYPE_FP32 dims: [-1, 17] } ]
parameters { key: "intra_op_thread_count" value: { string_value: "1" } }
parameters { key: "inter_op_thread_count" value: { string_value: "1" } }
parameters { key: "execution_mode" value: { string_value: "0" } }
dynamic_batching { max_queue_delay_microseconds: 1000 }
'''
    config += f'instance_group [ {{ count: {instances} kind: KIND_CPU }} ]\n'
    (directory / 'config.pbtxt').write_text(config + '\n'.join(parameters) + '\n')


if __name__ == '__main__':
    configure()
    os.execvp('tritonserver', ['tritonserver', '--model-repository=/models',
              '--model-control-mode=none', '--disable-auto-complete-config',
              '--allow-gpu-metrics=false', '--allow-grpc=false'])
