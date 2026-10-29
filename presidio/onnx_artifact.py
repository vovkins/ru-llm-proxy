"""Validate build-produced export metadata without importing inference engines."""

import hashlib
import json
from pathlib import Path

try:
    from .model_artifact import ModelArtifactError, SHA256_PATTERN
    from .model_contract import (
        EXPECTED_ID2LABEL, INPUT_NAMES, MAX_BATCH_SIZE, MODEL_ID, MODEL_REVISION,
        MODEL_VARIANT, ONNX_OPSET, SEQUENCE_LENGTH,
    )
except ImportError:
    from model_artifact import ModelArtifactError, SHA256_PATTERN
    from model_contract import (
        EXPECTED_ID2LABEL, INPUT_NAMES, MAX_BATCH_SIZE, MODEL_ID, MODEL_REVISION,
        MODEL_VARIANT, ONNX_OPSET, SEQUENCE_LENGTH,
    )

EXPORT_MANIFEST_PATH = Path("/opt/ner-export/export-manifest.json")


def label_digest(id2label=EXPECTED_ID2LABEL):
    labels = [id2label[i] for i in range(len(id2label))]
    return hashlib.sha256(json.dumps(labels, separators=(",", ":")).encode()).hexdigest()


def validate_export_manifest(payload, source_manifest=None):
    expected = {
        "schema_version": 2, "model_profile": "tiny2", "precision": "fp32",
        "source_model_id": MODEL_ID, "source_revision": MODEL_REVISION,
        "source_variant": MODEL_VARIANT, "opset": ONNX_OPSET,
        "sequence_length": SEQUENCE_LENGTH, "max_batch_size": MAX_BATCH_SIZE,
        "input_names": list(INPUT_NAMES), "label_contract_sha256": label_digest(),
    }
    if not isinstance(payload, dict) or any(payload.get(k) != v for k, v in expected.items()):
        raise ModelArtifactError("export metadata does not match the fixed model contract")
    if any(type(payload.get(k)) is not int for k in ("schema_version", "opset", "sequence_length", "max_batch_size")):
        raise ModelArtifactError("export tensor dimensions must be integers")
    for key in ("onnx_sha256", "source_model_sha256", "tokenizer_sha256"):
        if not isinstance(payload.get(key), str) or not SHA256_PATTERN.fullmatch(payload[key]):
            raise ModelArtifactError("invalid export checksum: " + key)
    versions = payload.get("versions")
    if not isinstance(versions, dict) or any(
        not isinstance(versions.get(name), str) or not versions[name]
        for name in ("python", "torch", "transformers", "onnx", "onnxruntime", "tokenizers")
    ):
        raise ModelArtifactError("export tool versions are missing")
    if source_manifest is not None:
        files = [dict(path=f.path, size=f.size, sha256=f.sha256) for f in source_manifest.files]
        hashes = {f.path: f.sha256 for f in source_manifest.files}
        if (
            source_manifest.model_id != MODEL_ID or source_manifest.revision != MODEL_REVISION
            or payload.get("source_files") != files
            or payload["source_model_sha256"] != hashes.get("model.safetensors")
            or payload["tokenizer_sha256"] != hashes.get("tokenizer.json")
        ):
            raise ModelArtifactError("export source does not match the verified model")
    return payload


def load_export_manifest(path=EXPORT_MANIFEST_PATH, source_manifest=None):
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ModelArtifactError("cannot read trusted export metadata") from exc
    return validate_export_manifest(payload, source_manifest)
