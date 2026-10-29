"""Compare the pinned source, exported graph and a real internal Triton server."""

import argparse
import json
from pathlib import Path
from types import SimpleNamespace

from export_triton_model import parity_cases, validate_components
from model_artifact import MODEL_DIRECTORY, file_sha256, load_and_verify_embedded_manifest
from model_contract import INPUT_NAMES
from onnx_artifact import load_export_manifest


def verify(model_directory, export_root, url):
    import httpx
    import numpy as np
    import onnxruntime as ort
    import torch
    from transformers import AutoModelForTokenClassification, AutoTokenizer
    from ner.triton_model import TritonModel
    from ner.tokenization import ContentTokenizer
    from ner.decoding import classify_logits

    torch.set_num_threads(1)
    source = load_and_verify_embedded_manifest(model_directory)
    directory = export_root / "tiny2"
    metadata = load_export_manifest(directory / "export-manifest.json", source)
    graph = directory / "1/model.onnx"
    if file_sha256(graph) != metadata["onnx_sha256"]:
        raise ValueError("Graph differs from the trusted export")
    tokenizer = AutoTokenizer.from_pretrained(model_directory, local_files_only=True,
        trust_remote_code=False, use_fast=True, fix_mistral_regex=False)
    model = AutoModelForTokenClassification.from_pretrained(model_directory,
        local_files_only=True, trust_remote_code=False, use_safetensors=True).cpu().eval()
    validate_components(tokenizer, model)
    content_tokenizer = ContentTokenizer(model_directory)
    texts = ["", "password=pine-dawn727", "\u0418\u0432\u0430\u043d \u041f\u0435\u0442\u0440\u043e\u0432",
             "\u0418\u0432\u0430\u0301\u043d \U0001f642\n", "a\u00adb\r\nc", "x " * 1600]
    for text in texts:
        expected = tokenizer(text, add_special_tokens=False, return_offsets_mapping=True)
        actual = content_tokenizer(text)
        if (expected["input_ids"] != actual["input_ids"] or expected["offset_mapping"] != actual["offset_mapping"]):
            raise ValueError("Content tokenization differs from the reference")
        ids = actual["input_ids"][:384]
        if (tokenizer.build_inputs_with_special_tokens(ids) != content_tokenizer.build_inputs_with_special_tokens(ids)
            or tokenizer.create_token_type_ids_from_sequences(ids) != content_tokenizer.create_token_type_ids_from_sequences(ids)):
            raise ValueError("Special tokens differ from the reference")
    options = ort.SessionOptions()
    options.intra_op_num_threads = options.inter_op_num_threads = 1
    session = ort.InferenceSession(str(graph), options, providers=["CPUExecutionProvider"])
    settings = SimpleNamespace(url=url.rstrip("/"), timeout=60, model_name="tiny2", model_version="1")
    rows = []
    with httpx.Client(trust_env=False, timeout=60) as client:
        remote = TritonModel(settings, source, model.config.to_dict(), client,
                             export_manifest=metadata)
        for inputs in parity_cases(tokenizer):
            with torch.inference_mode():
                reference = model(**inputs).logits.numpy()
            local = session.run(["logits"], {n: v.numpy() for n, v in inputs.items()})[0]
            actual = remote(**{n: v.numpy() for n, v in inputs.items()}).logits
            score_error = 0.0
            for bias in (0.0, 1.0, 10.0):
                predicted, scores = classify_logits(actual, bias)
                raw = torch.from_numpy(actual)
                probabilities = torch.softmax(raw, dim=-1)
                corrected = raw.clone()
                corrected[..., 0] += bias
                expected_ids = corrected.argmax(-1) if bias else probabilities.argmax(-1)
                expected_scores = probabilities.gather(2, expected_ids.unsqueeze(2)).squeeze(2).numpy()
                if (not np.array_equal(predicted, expected_ids.numpy())
                    or not np.allclose(scores, expected_scores, atol=2e-7, rtol=2e-6)):
                    raise ValueError("NumPy decoding differs from the reference")
                score_error = max(score_error, float(np.max(np.abs(scores - expected_scores))))
            for output in (local, actual):
                if (output.shape != reference.shape or not np.isfinite(output).all()
                    or not np.allclose(output, reference, atol=1e-4, rtol=1e-4)):
                    raise ValueError("FP32 parity failed")
            rows.append(dict(batch=len(actual),
                numpy_score_max_abs=score_error,
                source_ort_max_abs=float(np.max(np.abs(reference-local))),
                source_triton_max_abs=float(np.max(np.abs(reference-actual))),
                ort_triton_max_abs=float(np.max(np.abs(local-actual))),
                argmax_differences=int(np.sum(reference.argmax(-1) != actual.argmax(-1)))))
        rejected = []
        for batch, sequence, dtype in ((0,386,"INT64"),(33,386,"INT64"),
                                       (1,385,"INT64"),(1,387,"INT64"),(1,386,"INT32")):
            data = np.zeros((batch, sequence), dtype="<i8" if dtype == "INT64" else "<i4")
            header = dict(inputs=[dict(name=n, shape=list(data.shape), datatype=dtype,
                parameters=dict(binary_data_size=data.nbytes)) for n in INPUT_NAMES],
                outputs=[dict(name="logits", parameters=dict(binary_data=True))])
            encoded = json.dumps(header).encode()
            response = client.post(settings.url + remote.path + "/infer",
                content=encoded + data.tobytes()*3,
                headers={"Content-Type":"application/octet-stream",
                         "Inference-Header-Content-Length":str(len(encoded))})
            if response.status_code not in {400, 413, 422}:
                raise ValueError("Invalid tensor request was not safely rejected")
            rejected.append(dict(batch=batch, sequence=sequence, dtype=dtype,
                                 status=response.status_code))
        if not remote.ready():
            raise ValueError("Triton did not remain ready after invalid requests")
    print(json.dumps(dict(onnx_sha256=metadata["onnx_sha256"], atol=1e-4, rtol=1e-4,
                          tokenizer_cases=len(texts), cases=rows, rejected=rejected), sort_keys=True), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--model-directory", type=Path, default=MODEL_DIRECTORY)
    parser.add_argument("--export-root", type=Path, default=Path("/export/models"))
    parser.add_argument("--triton-url", required=True)
    args = parser.parse_args()
    verify(args.model_directory, args.export_root, args.triton_url)
