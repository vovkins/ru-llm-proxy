"""Tests for pinned Hugging Face model acquisition and verification."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from presidio.download_hf_model import download_model, model_file_url
from presidio.model_artifact import (
    EMBEDDED_MANIFEST_NAME,
    ModelArtifactError,
    load_manifest,
    verify_model_directory,
)


EXPECTED_MODEL_FILES = {
    "config.json",
    "model.safetensors",
    "special_tokens_map.json",
    "tokenizer.json",
    "tokenizer_config.json",
    "vocab.txt",
}


def _manifest_payload(files: dict[str, bytes]) -> dict:
    return {
        "schema_version": 1,
        "model_id": "owner/model",
        "revision": "a" * 40,
        "architecture": "BertForTokenClassification",
        "license": "apache-2.0",
        "base_model": "owner/base",
        "files": [
            {
                "path": name,
                "size": len(content),
                "sha256": hashlib.sha256(content).hexdigest(),
            }
            for name, content in files.items()
        ],
    }


def _write_manifest(path: Path, files: dict[str, bytes]) -> Path:
    path.write_text(
        json.dumps(_manifest_payload(files), indent=2) + "\n",
        encoding="utf-8",
    )
    return path


def _write_model_directory(
    path: Path,
    manifest_path: Path,
    files: dict[str, bytes],
) -> Path:
    path.mkdir()
    for name, content in files.items():
        (path / name).write_bytes(content)
    (path / EMBEDDED_MANIFEST_NAME).write_bytes(manifest_path.read_bytes())
    return path


def test_project_manifest_pins_only_runtime_model_files():
    manifest_path = Path(__file__).resolve().parents[1] / "model_manifest.json"
    manifest = load_manifest(manifest_path)

    assert manifest.model_id == "fef2/ner_rus_bert-secret_detection"
    assert manifest.revision == "52b5b0745aac14f73fcf2ac0f91d9b5001a85ae4"
    assert manifest.architecture == "BertForTokenClassification"
    assert {model_file.path for model_file in manifest.files} == EXPECTED_MODEL_FILES
    weights = next(item for item in manifest.files if item.path == "model.safetensors")
    assert weights.size == 709127044
    assert weights.sha256 == "6a2c875d02398554ec69384f489a0bf4fe3505fc347c6cdd3385d4fd31ef21a4"


@pytest.mark.parametrize(
    "mutation,match",
    [
        (lambda payload: payload.update(schema_version=2), "schema"),
        (lambda payload: payload["files"].append(payload["files"][0]), "duplicate"),
        (lambda payload: payload["files"][0].update(path="../config.json"), "filename"),
        (lambda payload: payload["files"][0].update(sha256="invalid"), "SHA-256"),
        (lambda payload: payload["files"][0].update(size=0), "size"),
    ],
)
def test_load_manifest_rejects_invalid_contract(tmp_path, mutation, match):
    payload = _manifest_payload({"config.json": b"{}"})
    mutation(payload)
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ModelArtifactError, match=match):
        load_manifest(manifest_path)


def test_verify_model_directory_accepts_exact_verified_files(tmp_path):
    files = {"config.json": b"{}", "model.safetensors": b"safe-weights"}
    manifest_path = _write_manifest(tmp_path / "manifest.json", files)
    model_directory = _write_model_directory(
        tmp_path / "model",
        manifest_path,
        files,
    )

    verify_model_directory(model_directory, load_manifest(manifest_path))


def test_verify_model_directory_rejects_corruption_and_extra_files(tmp_path):
    files = {"config.json": b"{}"}
    manifest_path = _write_manifest(tmp_path / "manifest.json", files)
    model_directory = _write_model_directory(
        tmp_path / "model",
        manifest_path,
        files,
    )
    manifest = load_manifest(manifest_path)

    (model_directory / "config.json").write_bytes(b"modified")
    with pytest.raises(ModelArtifactError, match="size mismatch"):
        verify_model_directory(model_directory, manifest)

    (model_directory / "config.json").write_bytes(b"{}")
    (model_directory / "unexpected.py").write_text("pass", encoding="utf-8")
    with pytest.raises(ModelArtifactError, match="contents mismatch"):
        verify_model_directory(model_directory, manifest)


def test_model_file_url_uses_pinned_revision(tmp_path):
    manifest_path = _write_manifest(tmp_path / "manifest.json", {"config.json": b"{}"})
    manifest = load_manifest(manifest_path)

    url = model_file_url(manifest, manifest.files[0])

    assert url == (
        "https://huggingface.co/owner/model/resolve/"
        f"{'a' * 40}/config.json?download=true"
    )
    assert "main" not in url


def test_download_model_is_atomic_and_embeds_manifest(tmp_path, monkeypatch):
    files = {"config.json": b"{}", "model.safetensors": b"safe-weights"}
    manifest_path = _write_manifest(tmp_path / "manifest.json", files)
    output_directory = tmp_path / "model"

    def fake_download(_url, destination, expected):
        destination.write_bytes(files[expected.path])

    monkeypatch.setattr("presidio.download_hf_model.download_file", fake_download)

    manifest = download_model(manifest_path, output_directory)

    assert manifest.revision == "a" * 40
    assert (output_directory / EMBEDDED_MANIFEST_NAME).read_bytes() == manifest_path.read_bytes()
    verify_model_directory(output_directory, manifest)


def test_download_model_rejects_existing_different_revision(tmp_path):
    files = {"config.json": b"{}"}
    manifest_path = _write_manifest(tmp_path / "manifest.json", files)
    output_directory = _write_model_directory(
        tmp_path / "model",
        manifest_path,
        files,
    )
    changed_payload = _manifest_payload(files)
    changed_payload["revision"] = "b" * 40
    changed_manifest = tmp_path / "changed.json"
    changed_manifest.write_text(json.dumps(changed_payload), encoding="utf-8")

    with pytest.raises(ModelArtifactError, match="does not match"):
        download_model(changed_manifest, output_directory)
