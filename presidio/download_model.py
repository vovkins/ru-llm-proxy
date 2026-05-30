#!/usr/bin/env python3
"""Download DeepPavlov ner_rus_bert model files safely.

The Docker build uses this script to prefetch the model archive without
building the DeepPavlov model itself. The download is atomic, optional SHA-256
verification is supported, and tar extraction rejects unsafe paths.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import tarfile
import tempfile
import urllib.parse
import urllib.request
from pathlib import Path


DEFAULT_MODEL_URL = "http://files.deeppavlov.ai/v1/ner/ner_rus_bert_torch_new.tar.gz"
DEFAULT_MODEL_DIR = "/root/.deeppavlov/models/ner_rus_bert_torch"
ARCHIVE_NAME = "ner_rus_bert_torch_new.tar.gz"
CHUNK_SIZE = 1024 * 1024


def _get_int_env(name: str, default: int) -> int:
    raw_value = os.getenv(name)
    if raw_value is None:
        return default
    try:
        return int(raw_value)
    except ValueError:
        print(f"Invalid {name}={raw_value!r}; falling back to {default}")
        return default


DOWNLOAD_TIMEOUT_SECONDS = _get_int_env("DEEPPAVLOV_NER_DOWNLOAD_TIMEOUT_SECONDS", 120)


def _validate_url(url: str) -> None:
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError(f"Unsupported model URL: {url}")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file_obj:
        for chunk in iter(lambda: file_obj.read(CHUNK_SIZE), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _download(url: str, destination: Path) -> None:
    _validate_url(url)
    destination.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = destination.with_suffix(destination.suffix + ".tmp")
    tmp_path.unlink(missing_ok=True)

    print(f"Downloading model archive from {url}")
    with urllib.request.urlopen(url, timeout=DOWNLOAD_TIMEOUT_SECONDS) as response:
        with tmp_path.open("wb") as file_obj:
            shutil.copyfileobj(response, file_obj, length=CHUNK_SIZE)

    tmp_path.replace(destination)


def _verify_checksum(path: Path, expected_sha256: str | None) -> str:
    actual_sha256 = _sha256(path)
    if expected_sha256:
        normalized = expected_sha256.strip().lower()
        if len(normalized) != 64 or any(c not in "0123456789abcdef" for c in normalized):
            raise ValueError("DEEPPAVLOV_NER_MODEL_SHA256 must be a 64-character hex string")
        if actual_sha256 != normalized:
            path.unlink(missing_ok=True)
            raise ValueError(
                f"SHA-256 mismatch for {path.name}: expected {normalized}, got {actual_sha256}"
            )
        print(f"Verified model archive SHA-256: {actual_sha256}")
    else:
        print(f"Model archive SHA-256: {actual_sha256}")
        print("Set DEEPPAVLOV_NER_MODEL_SHA256 to enforce checksum verification.")
    return actual_sha256


def _validate_member(member: tarfile.TarInfo, destination: Path) -> None:
    member_path = Path(member.name)
    if member_path.is_absolute():
        raise ValueError(f"Refusing absolute path in tar archive: {member.name}")
    if member.issym() or member.islnk():
        raise ValueError(f"Refusing link entry in tar archive: {member.name}")

    resolved_target = (destination / member.name).resolve()
    resolved_destination = destination.resolve()
    if not resolved_target.is_relative_to(resolved_destination):
        raise ValueError(f"Refusing path traversal in tar archive: {member.name}")


def _safe_extract(archive_path: Path, destination: Path) -> None:
    with tarfile.open(archive_path, "r:gz") as archive:
        members = archive.getmembers()
        for member in members:
            _validate_member(member, destination)
        archive.extractall(destination, members=members)


def _extract_model(archive_path: Path, model_dir: Path) -> None:
    model_root = model_dir.parent
    expected_name = model_dir.name

    with tempfile.TemporaryDirectory(prefix="deeppavlov-model-", dir=model_root) as tmp:
        tmp_dir = Path(tmp)
        print(f"Extracting model archive into {tmp_dir}")
        _safe_extract(archive_path, tmp_dir)

        extracted_model_dir = tmp_dir / expected_name
        if extracted_model_dir.is_dir():
            # Archive contains the expected subdirectory
            pass
        else:
            # Archive contains files directly — wrap them into the expected directory
            print(f"Archive has flat layout; creating {expected_name}/ directory")
            flat_files = list(tmp_dir.iterdir())
            extracted_model_dir.mkdir()
            for f in flat_files:
                f.rename(extracted_model_dir / f.name)

        if model_dir.exists():
            raise FileExistsError(f"Model directory appeared during extraction: {model_dir}")

        extracted_model_dir.replace(model_dir)


def main() -> None:
    url = os.getenv("DEEPPAVLOV_NER_MODEL_URL", DEFAULT_MODEL_URL)
    expected_sha256 = os.getenv("DEEPPAVLOV_NER_MODEL_SHA256", "").strip() or None
    model_dir = Path(os.getenv("DEEPPAVLOV_NER_MODEL_DIR", DEFAULT_MODEL_DIR))
    archive_path = model_dir.parent / ARCHIVE_NAME

    if model_dir.exists():
        print(f"Model already exists: {model_dir}")
        return

    _download(url, archive_path)
    _verify_checksum(archive_path, expected_sha256)
    _extract_model(archive_path, model_dir)
    archive_path.unlink(missing_ok=True)
    print(f"Model downloaded and extracted to {model_dir}")


if __name__ == "__main__":
    main()
