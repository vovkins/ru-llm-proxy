"""Export the verified local Tiny2 artifact to a CPU FP32 Triton repository."""
import argparse
import json
from pathlib import Path

from model_artifact import file_sha256, load_and_verify_embedded_manifest
from model_profiles import PROFILES
from ner.huggingface_recognizer import EXPECTED_ID2LABEL, HuggingFaceNERRecognizer
from ner.triton_model import label_digest


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--model-directory', type=Path, default=PROFILES['tiny2'].directory)
    parser.add_argument('--out', type=Path, required=True)
    args = parser.parse_args()
    import numpy as np
    import onnx
    import onnxruntime as ort
    import torch
    from transformers import AutoModelForTokenClassification, AutoTokenizer
    torch.set_num_threads(1)
    manifest = load_and_verify_embedded_manifest(args.model_directory)
    if (manifest.model_id, manifest.revision) != (PROFILES['tiny2'].model_id, PROFILES['tiny2'].revision):
        raise ValueError('Only the pinned Tiny2 artifact can be exported')
    tokenizer = AutoTokenizer.from_pretrained(args.model_directory, local_files_only=True,
                                             trust_remote_code=False, use_fast=True, fix_mistral_regex=False)
    model = AutoModelForTokenClassification.from_pretrained(args.model_directory,
        local_files_only=True, trust_remote_code=False, use_safetensors=True).cpu().eval()
    HuggingFaceNERRecognizer._validate_runtime_components(tokenizer, model)
    inputs = tokenizer(['password=pine-dawn727', 'postgresql://svc-a:1592@db'],
                       padding='max_length', max_length=386, truncation=True, return_tensors='pt')

    class ExportModel(torch.nn.Module):
        def __init__(self):
            super().__init__()
            self.net = model

        def forward(self, input_ids, attention_mask, token_type_ids):
            return self.net(input_ids=input_ids, attention_mask=attention_mask,
                            token_type_ids=token_type_ids).logits

    version = args.out / 'tiny2' / '1'
    version.mkdir(parents=True, exist_ok=False)
    graph = version / 'model.onnx'
    names = ['input_ids', 'attention_mask', 'token_type_ids']
    with torch.inference_mode():
        torch.onnx.export(ExportModel().eval(), tuple(inputs[name] for name in names), str(graph),
                          input_names=names, output_names=['logits'], opset_version=17,
                          dynamic_axes={name: {0: 'batch', 1: 'sequence'} for name in [*names, 'logits']},
                          dynamo=False)
        reference = model(**inputs).logits.numpy()
    onnx.checker.check_model(str(graph), full_check=True)
    options = ort.SessionOptions()
    options.intra_op_num_threads = options.inter_op_num_threads = 1
    session = ort.InferenceSession(str(graph), options, providers=['CPUExecutionProvider'])
    actual = session.run(['logits'], {name: inputs[name].numpy() for name in names})[0]
    if not np.allclose(reference, actual, atol=1e-4, rtol=1e-4):
        raise ValueError('Exported FP32 graph does not match source logits')
    files = {item.path: item.sha256 for item in manifest.files}
    provenance = {'schema_version': 1, 'model_profile': 'tiny2', 'precision': 'fp32',
                  'source_model_sha256': files['model.safetensors'],
                  'tokenizer_sha256': files['tokenizer.json'],
                  'label_contract_sha256': label_digest(EXPECTED_ID2LABEL),
                  'onnx_sha256': file_sha256(graph), 'source_model_id': manifest.model_id,
                  'source_revision': manifest.revision, 'opset': 17,
                  'torch_version': str(torch.__version__), 'ort_validation_version': ort.__version__}
    (args.out / 'tiny2' / 'export-manifest.json').write_text(json.dumps(provenance, indent=2))
    print('Verified Tiny2 FP32 export:', provenance['onnx_sha256'])


if __name__ == '__main__':
    main()
