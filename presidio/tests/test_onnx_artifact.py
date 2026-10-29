"""Build provenance, immutable graph identity and fixed serving shapes."""

import copy
import hashlib
import json
from pathlib import Path

import pytest

from presidio.model_artifact import ModelArtifactError, load_manifest
from presidio.model_contract import (
    INPUT_NAMES, MODEL_ID, MODEL_REVISION, MODEL_VARIANT, EXPECTED_ID2LABEL,
)
from presidio.onnx_artifact import label_digest, load_export_manifest, validate_export_manifest


def fixture_metadata():
    source = load_manifest(Path(__file__).resolve().parents[1] / "model_manifest.json")
    hashes = {f.path: f.sha256 for f in source.files}
    return source, dict(
        schema_version=2, model_profile="tiny2", precision="fp32", opset=17,
        source_model_id=MODEL_ID, source_revision=MODEL_REVISION, source_variant=MODEL_VARIANT,
        source_model_sha256=hashes["model.safetensors"], tokenizer_sha256=hashes["tokenizer.json"],
        onnx_sha256=hashlib.sha256(b"synthetic-graph").hexdigest(),
        label_contract_sha256=label_digest(), input_names=list(INPUT_NAMES),
        sequence_length=386, max_batch_size=32,
        source_files=[dict(path=f.path, size=f.size, sha256=f.sha256) for f in source.files],
        versions={name:"test" for name in ("python","torch","transformers","onnx","onnxruntime","tokenizers")},
    )


def test_export_metadata_is_checked_against_the_local_source(tmp_path):
    source, metadata = fixture_metadata()
    path = tmp_path / "export-manifest.json"
    path.write_text(json.dumps(metadata))
    assert load_export_manifest(path, source) == metadata


@pytest.mark.parametrize("field,value", [
    ("schema_version", 1), ("precision", "fp16"), ("source_revision", "main"),
    ("source_model_id", "other/model"), ("source_variant", "other"), ("opset", 18),
    ("sequence_length", 384), ("max_batch_size", 64), ("input_names", ["input_ids"]),
    ("label_contract_sha256", "a"*64), ("onnx_sha256", "bad"),
    ("source_model_sha256", "a"*64), ("tokenizer_sha256", "a"*64),
    ("source_files", []), ("versions", {}),
])
def test_incompatible_metadata_is_rejected(field, value):
    source, metadata = fixture_metadata()
    metadata[field] = value
    with pytest.raises(ModelArtifactError):
        validate_export_manifest(metadata, source)


def test_modified_nonweight_source_file_is_rejected():
    source, metadata = fixture_metadata()
    metadata = copy.deepcopy(metadata)
    metadata["source_files"][0]["sha256"] = "a"*64
    with pytest.raises(ModelArtifactError, match="source"):
        validate_export_manifest(metadata, source)


def test_missing_or_malformed_trusted_metadata_is_not_learned_from_server(tmp_path):
    path = tmp_path / "missing.json"
    with pytest.raises(ModelArtifactError):
        load_export_manifest(path)
    path.write_text("[]")
    with pytest.raises(ModelArtifactError):
        load_export_manifest(path)


@pytest.mark.parametrize("instances", [None, "1", "2"])
def test_entrypoint_checks_bytes_before_creating_fixed_cpu_config(tmp_path, monkeypatch, instances):
    from presidio.triton_entrypoint import configure

    _, metadata = fixture_metadata()
    directory = tmp_path / "tiny2"
    (directory / "1").mkdir(parents=True)
    graph = directory / "1/model.onnx"
    graph.write_bytes(b"synthetic-graph")
    (directory / "export-manifest.json").write_text(json.dumps(metadata))
    if instances is None:
        monkeypatch.delenv("TRITON_MODEL_INSTANCES", raising=False)
    else:
        monkeypatch.setenv("TRITON_MODEL_INSTANCES", instances)
    configure(tmp_path)
    config = (directory / "config.pbtxt").read_text()
    assert config.count("dims: [386]") == 3
    assert "dims: [386, 17]" in config and "KIND_CPU" in config
    assert "timeout_action: REJECT" in config
    assert "max_queue_size: 64" in config
    assert "default_timeout_microseconds: 60000000" in config
    assert "allow_timeout_override: true" in config
    assert f"count: {instances or '2'} kind: KIND_CPU" in config
    assert 'key: "intra_op_thread_count" value: { string_value: "1" }' in config
    assert 'key: "inter_op_thread_count" value: { string_value: "1" }' in config
    graph.write_bytes(b"modified-graph")
    (directory / "config.pbtxt").unlink()
    with pytest.raises(ValueError, match="checksum"):
        configure(tmp_path)
    assert not (directory / "config.pbtxt").exists()


def test_exporter_imports_without_any_inference_or_analyzer_dependencies():
    import os
    import subprocess
    import sys

    root = Path(__file__).resolve().parents[1]
    command = "import export_triton_model, sys; assert not {'torch','onnx','presidio_analyzer','transformers'} & sys.modules.keys()"
    env = dict(os.environ, PYTHONPATH=str(root), PYTHONDONTWRITEBYTECODE="1")
    subprocess.run([sys.executable, "-S", "-c", command], env=env, check=True, capture_output=True)


def test_label_order_is_identical_to_existing_decoder():
    from presidio.ner.triton_recognizer import EXPECTED_ID2LABEL as decoder_labels

    assert EXPECTED_ID2LABEL == decoder_labels
