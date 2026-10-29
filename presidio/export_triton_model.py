"""Export the verified Tiny2 artifact and validate CPU FP32 parity."""

import argparse
import importlib.metadata
import json
import platform
import tempfile
from pathlib import Path

from model_artifact import MODEL_DIRECTORY, file_sha256, load_and_verify_embedded_manifest
from model_contract import (
    EXPECTED_ID2LABEL, INPUT_NAMES, MAX_BATCH_SIZE, MODEL_ID, MODEL_REVISION,
    MODEL_VARIANT, ONNX_OPSET, SEQUENCE_LENGTH,
)
from onnx_artifact import label_digest, validate_export_manifest


def validate_components(tokenizer, model):
    config = model.config
    if (
        not tokenizer.is_fast or config.architectures != ["BertForTokenClassification"]
        or config.id2label != EXPECTED_ID2LABEL or len(tokenizer) != config.vocab_size
        or config.max_position_embeddings < SEQUENCE_LENGTH
        or tokenizer.num_special_tokens_to_add(pair=False) != 2
    ):
        raise ValueError("Source model/tokenizer contract is invalid")


def parity_cases(tokenizer):
    texts = ["", "password=pine-dawn727", "Иван Петров, договор №123/А", "Ива\u0301н 🙂"]
    for size in (1, 2, 4, MAX_BATCH_SIZE):
        inputs = tokenizer([texts[i % len(texts)] for i in range(size)],
                           padding="max_length", max_length=SEQUENCE_LENGTH,
                           truncation=True, return_tensors="pt")
        # Exact 384-content-token boundary, independent of text tokenization.
        if size > 1:
            ids = tokenizer.build_inputs_with_special_tokens([100] * (SEQUENCE_LENGTH - 2))
            inputs["input_ids"][-1] = inputs["input_ids"].new_tensor(ids)
            inputs["attention_mask"][-1].fill_(1)
            inputs["token_type_ids"][-1].fill_(0)
        yield {name: inputs[name] for name in INPUT_NAMES}


def export_model(model_directory, output):
    import numpy as np
    import onnx
    import onnxruntime as ort
    import torch
    from transformers import AutoModelForTokenClassification, AutoTokenizer

    torch.set_num_threads(1)
    manifest = load_and_verify_embedded_manifest(model_directory)
    if (manifest.model_id, manifest.revision) != (MODEL_ID, MODEL_REVISION):
        raise ValueError("Only the pinned Tiny2 artifact can be exported")
    tokenizer = AutoTokenizer.from_pretrained(model_directory, local_files_only=True,
        trust_remote_code=False, use_fast=True, fix_mistral_regex=False)
    model = AutoModelForTokenClassification.from_pretrained(model_directory,
        local_files_only=True, trust_remote_code=False, use_safetensors=True).cpu().eval()
    validate_components(tokenizer, model)

    class ExportModel(torch.nn.Module):
        def forward(self, input_ids, attention_mask, token_type_ids):
            return self.net(input_ids=input_ids, attention_mask=attention_mask,
                            token_type_ids=token_type_ids).logits

    wrapper = ExportModel()
    wrapper.net = model
    cases = list(parity_cases(tokenizer))
    output = Path(output)
    if output.exists():
        raise ValueError("Export output already exists")
    output.parent.mkdir(parents=True, exist_ok=True)
    # Publish only after graph validation, parity and provenance are complete.
    with tempfile.TemporaryDirectory(prefix="tiny2-export-", dir=output.parent) as tmp:
        root = Path(tmp) / "models"
        version = root / "tiny2" / "1"
        version.mkdir(parents=True)
        graph = version / "model.onnx"
        with torch.inference_mode():
            torch.onnx.export(wrapper.eval(), tuple(cases[1][name] for name in INPUT_NAMES),
                str(graph), input_names=list(INPUT_NAMES), output_names=["logits"],
                opset_version=ONNX_OPSET, dynamo=False,
                dynamic_axes={name: {0: "batch"} for name in (*INPUT_NAMES, "logits")})
        onnx.checker.check_model(str(graph), full_check=True)
        options = ort.SessionOptions()
        options.intra_op_num_threads = options.inter_op_num_threads = 1
        session = ort.InferenceSession(str(graph), options, providers=["CPUExecutionProvider"])
        checks = []
        for inputs in cases:
            with torch.inference_mode():
                reference = model(**inputs).logits.numpy()
            actual = session.run(["logits"], {n: v.numpy() for n, v in inputs.items()})[0]
            if (actual.shape != reference.shape or actual.dtype != np.float32
                or not np.isfinite(actual).all()
                or not np.allclose(reference, actual, atol=1e-4, rtol=1e-4)):
                raise ValueError("Exported FP32 graph does not match source logits")
            checks.append({"batch": len(actual),
                           "max_absolute_error": float(np.max(np.abs(actual-reference))),
                           "argmax_differences": int(np.sum(actual.argmax(-1) != reference.argmax(-1)))})
        hashes = {f.path: f.sha256 for f in manifest.files}
        provenance = {
            "schema_version": 2, "model_profile": "tiny2", "precision": "fp32",
            "source_model_id": MODEL_ID, "source_revision": MODEL_REVISION,
            "source_variant": MODEL_VARIANT,
            "source_files": [dict(path=f.path, size=f.size, sha256=f.sha256) for f in manifest.files],
            "source_model_sha256": hashes["model.safetensors"],
            "tokenizer_sha256": hashes["tokenizer.json"],
            "label_contract_sha256": label_digest(), "onnx_sha256": file_sha256(graph),
            "opset": ONNX_OPSET, "sequence_length": SEQUENCE_LENGTH,
            "max_batch_size": MAX_BATCH_SIZE, "input_names": list(INPUT_NAMES),
            "versions": {name: importlib.metadata.version(name) for name in
                         ("torch", "transformers", "onnx", "onnxruntime", "tokenizers")},
            "parity": {"atol": 1e-4, "rtol": 1e-4, "cases": checks},
            "builtin_label_bias": {name: 2.0 for name in ("PERSON", "LOCATION", "ORGANIZATION")},
        }
        provenance["versions"]["python"] = platform.python_version()
        validate_export_manifest(provenance, manifest)
        (root / "tiny2" / "export-manifest.json").write_text(json.dumps(provenance, indent=2) + "\n")
        root.replace(output)
    print("Verified Tiny2 FP32 export:", provenance["onnx_sha256"], flush=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--model-directory", type=Path, default=MODEL_DIRECTORY)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    export_model(args.model_directory, args.out)


if __name__ == "__main__":
    main()
