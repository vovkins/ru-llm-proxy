#!/usr/bin/env python3
"""Download (or copy from a local artifact folder) and verify the pinned NER model.

The profile (``--profile`` or PRESIDIO_ANALYZER_NER_MODEL_PROFILE) selects the
manifest and install directory. Files come from Hugging Face unless the profile's
folder under ``--local-artifacts-root`` holds them; every file is checked against
the manifest's size and SHA-256 either way.
"""

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
    from .model_profiles import resolve_profile
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
    from model_profiles import resolve_profile


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


def copy_file(source: Path, destination: Path, expected: ModelFile) -> None:
    if source.is_symlink() or not source.is_file():
        raise ModelArtifactError(f"local model file is missing: {expected.path}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, destination)
    if destination.stat().st_size != expected.size:
        raise ModelArtifactError(f"local model file size mismatch: {expected.path}")
    if file_sha256(destination) != expected.sha256:
        raise ModelArtifactError(f"local model file SHA-256 mismatch: {expected.path}")


def local_source_directory(root: Path | None, profile_name: str) -> Path | None:
    """Return the profile's local artifact folder when it holds model files."""
    if root is None:
        return None
    candidate = root / profile_name
    if candidate.is_dir() and any(
        entry.is_file() and not entry.name.startswith(".") and entry.name != "README.md"
        for entry in candidate.iterdir()
    ):
        return candidate
    return None


def download_model(
    manifest_path: Path,
    output_directory: Path = MODEL_DIRECTORY,
    source_directory: Path | None = None,
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
            if source_directory is not None:
                print(f"Copying pinned model file: {model_file.path}", flush=True)
                copy_file(
                    source_directory / model_file.path,
                    staging / model_file.path,
                    model_file,
                )
                continue
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
        "--profile",
        default=None,
        help="NER model profile; defaults to PRESIDIO_ANALYZER_NER_MODEL_PROFILE or bert",
    )
    parser.add_argument("--manifest", type=Path, default=None)
    parser.add_argument("--output", type=Path, default=None)
    parser.add_argument(
        "--local-artifacts-root",
        type=Path,
        default=None,
        help="folder with <profile>/ model files to copy instead of downloading",
    )
    return parser


def main() -> None:
    args = build_parser().parse_args()
    environ = None
    if args.profile:
        environ = {"PRESIDIO_ANALYZER_NER_MODEL_PROFILE": args.profile}
    profile = resolve_profile(environ)
    manifest = args.manifest or Path(__file__).with_name(profile.manifest_filename)
    output = args.output or profile.directory
    source = local_source_directory(args.local_artifacts_root, profile.name)
    download_model(manifest, output, source)


if __name__ == "__main__":
    main()
