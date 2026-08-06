#!/usr/bin/env python3
"""Download and verify the project's pinned Hugging Face NER model."""

from __future__ import annotations

import argparse
import shutil
import tempfile
import urllib.parse
import urllib.request
from pathlib import Path

try:
    from .model_artifact import (
        EMBEDDED_MANIFEST_NAME,
        MODEL_DIRECTORY,
        ModelArtifactError,
        ModelFile,
        ModelManifest,
        file_sha256,
        load_manifest,
        verify_model_directory,
    )
except ImportError:  # Docker build executes this file as a standalone script.
    from model_artifact import (
        EMBEDDED_MANIFEST_NAME,
        MODEL_DIRECTORY,
        ModelArtifactError,
        ModelFile,
        ModelManifest,
        file_sha256,
        load_manifest,
        verify_model_directory,
    )


HUGGING_FACE_BASE_URL = "https://huggingface.co"
DOWNLOAD_TIMEOUT_SECONDS = 120


def model_file_url(manifest: ModelManifest, model_file: ModelFile) -> str:
    filename = urllib.parse.quote(model_file.path, safe="")
    return (
        f"{HUGGING_FACE_BASE_URL}/{manifest.model_id}/resolve/"
        f"{manifest.revision}/{filename}?download=true"
    )


def download_file(url: str, destination: Path, expected: ModelFile) -> None:
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "ru-llm-proxy-model-build/1"},
    )
    destination.parent.mkdir(parents=True, exist_ok=True)
    with urllib.request.urlopen(request, timeout=DOWNLOAD_TIMEOUT_SECONDS) as response:
        with destination.open("wb") as output:
            shutil.copyfileobj(response, output, length=1024 * 1024)

    if destination.stat().st_size != expected.size:
        raise ModelArtifactError(f"downloaded model file size mismatch: {expected.path}")
    if file_sha256(destination) != expected.sha256:
        raise ModelArtifactError(f"downloaded model file SHA-256 mismatch: {expected.path}")


def download_model(
    manifest_path: Path,
    output_directory: Path = MODEL_DIRECTORY,
) -> ModelManifest:
    manifest = load_manifest(manifest_path)
    if output_directory.exists():
        embedded = load_manifest(output_directory / EMBEDDED_MANIFEST_NAME)
        if embedded != manifest:
            raise ModelArtifactError("existing model revision does not match build manifest")
        verify_model_directory(output_directory, manifest)
        print(
            f"Pinned model is already verified: {manifest.model_id}@{manifest.revision}",
            flush=True,
        )
        return manifest

    output_directory.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(
        prefix="hf-ner-model-",
        dir=output_directory.parent,
    ) as temporary_root:
        staging = Path(temporary_root) / "model"
        staging.mkdir()
        for model_file in manifest.files:
            print(f"Downloading pinned model file: {model_file.path}", flush=True)
            download_file(
                model_file_url(manifest, model_file),
                staging / model_file.path,
                model_file,
            )

        verify_model_directory(staging, manifest, allow_embedded_manifest=False)
        shutil.copyfile(manifest_path, staging / EMBEDDED_MANIFEST_NAME)
        verify_model_directory(staging, manifest)
        staging.replace(output_directory)

    print(
        f"Verified pinned model: {manifest.model_id}@{manifest.revision}",
        flush=True,
    )
    return manifest


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--manifest",
        type=Path,
        default=Path(__file__).with_name("model_manifest.json"),
    )
    parser.add_argument("--output", type=Path, default=MODEL_DIRECTORY)
    return parser


def main() -> None:
    args = build_parser().parse_args()
    download_model(args.manifest, args.output)


if __name__ == "__main__":
    main()
