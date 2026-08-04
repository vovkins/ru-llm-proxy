"""Validation for the immutable Hugging Face NER model artifact."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path


MODEL_DIRECTORY = Path("/opt/models/ner_rus_bert-secret_detection")
EMBEDDED_MANIFEST_NAME = ".model-manifest.json"
MANIFEST_SCHEMA_VERSION = 1
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
CHUNK_SIZE = 1024 * 1024


class ModelArtifactError(RuntimeError):
    """Raised when the pinned model artifact is missing or inconsistent."""


@dataclass(frozen=True)
class ModelFile:
    path: str
    size: int
    sha256: str


@dataclass(frozen=True)
class ModelManifest:
    model_id: str
    revision: str
    architecture: str
    license: str
    base_model: str
    files: tuple[ModelFile, ...]


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(CHUNK_SIZE), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_manifest(path: Path) -> ModelManifest:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ModelArtifactError(f"cannot read model manifest: {type(exc).__name__}") from exc

    if not isinstance(payload, dict):
        raise ModelArtifactError("model manifest must be a JSON object")
    if payload.get("schema_version") != MANIFEST_SCHEMA_VERSION:
        raise ModelArtifactError("unsupported model manifest schema")

    metadata_fields = ("model_id", "revision", "architecture", "license", "base_model")
    metadata: dict[str, str] = {}
    for field in metadata_fields:
        value = payload.get(field)
        if not isinstance(value, str) or not value.strip():
            raise ModelArtifactError(f"model manifest field {field} must be non-empty")
        metadata[field] = value.strip()

    raw_files = payload.get("files")
    if not isinstance(raw_files, list) or not raw_files:
        raise ModelArtifactError("model manifest must contain files")

    files: list[ModelFile] = []
    seen_paths: set[str] = set()
    for raw_file in raw_files:
        if not isinstance(raw_file, dict):
            raise ModelArtifactError("model manifest file entry must be an object")
        file_path = raw_file.get("path")
        size = raw_file.get("size")
        sha256 = raw_file.get("sha256")
        if (
            not isinstance(file_path, str)
            or not file_path
            or Path(file_path).name != file_path
            or file_path.startswith(".")
        ):
            raise ModelArtifactError("model manifest file path must be a plain filename")
        if file_path in seen_paths:
            raise ModelArtifactError(f"duplicate model manifest path: {file_path}")
        if not isinstance(size, int) or isinstance(size, bool) or size <= 0:
            raise ModelArtifactError(f"invalid model file size: {file_path}")
        if not isinstance(sha256, str) or not SHA256_PATTERN.fullmatch(sha256):
            raise ModelArtifactError(f"invalid model file SHA-256: {file_path}")
        seen_paths.add(file_path)
        files.append(ModelFile(path=file_path, size=size, sha256=sha256))

    return ModelManifest(files=tuple(files), **metadata)


def verify_model_directory(
    model_directory: Path,
    manifest: ModelManifest,
    *,
    allow_embedded_manifest: bool = True,
) -> None:
    if not model_directory.is_dir():
        raise ModelArtifactError("model directory does not exist")

    expected_names = {model_file.path for model_file in manifest.files}
    if allow_embedded_manifest:
        expected_names.add(EMBEDDED_MANIFEST_NAME)
    actual_names = {entry.name for entry in model_directory.iterdir()}
    if actual_names != expected_names:
        missing = sorted(expected_names - actual_names)
        unexpected = sorted(actual_names - expected_names)
        raise ModelArtifactError(
            f"model directory contents mismatch: missing={missing}, unexpected={unexpected}"
        )

    for model_file in manifest.files:
        path = model_directory / model_file.path
        if path.is_symlink() or not path.is_file():
            raise ModelArtifactError(f"model artifact is not a regular file: {model_file.path}")
        actual_size = path.stat().st_size
        if actual_size != model_file.size:
            raise ModelArtifactError(
                f"model artifact size mismatch: {model_file.path}"
            )
        if file_sha256(path) != model_file.sha256:
            raise ModelArtifactError(
                f"model artifact SHA-256 mismatch: {model_file.path}"
            )


def load_and_verify_embedded_manifest(model_directory: Path) -> ModelManifest:
    manifest = load_manifest(model_directory / EMBEDDED_MANIFEST_NAME)
    verify_model_directory(model_directory, manifest)
    return manifest
