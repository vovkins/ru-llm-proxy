"""Tests for safe DeepPavlov model archive handling."""

import hashlib
import tarfile
from io import BytesIO

import pytest

from download_model import _extract_model, _safe_extract, _validate_url, _verify_checksum


def _add_file(archive, name, payload):
    member = tarfile.TarInfo(name)
    member.size = len(payload)
    archive.addfile(member, BytesIO(payload))


def test_validate_url_rejects_local_paths():
    with pytest.raises(ValueError, match="Unsupported model URL"):
        _validate_url("file:///tmp/model.tar.gz")


def test_safe_extract_rejects_path_traversal(tmp_path):
    archive_path = tmp_path / "model.tar.gz"
    with tarfile.open(archive_path, "w:gz") as archive:
        _add_file(archive, "../escape.txt", b"bad")

    with pytest.raises(ValueError, match="path traversal"):
        _safe_extract(archive_path, tmp_path / "out")


def test_verify_checksum_accepts_expected_hash(tmp_path):
    archive_path = tmp_path / "model.tar.gz"
    archive_path.write_bytes(b"model")
    expected = hashlib.sha256(b"model").hexdigest()

    actual = _verify_checksum(archive_path, expected)

    assert actual == expected
    assert archive_path.exists()


def test_verify_checksum_deletes_bad_archive(tmp_path):
    archive_path = tmp_path / "model.tar.gz"
    archive_path.write_bytes(b"model")
    wrong = hashlib.sha256(b"other").hexdigest()

    with pytest.raises(ValueError, match="SHA-256 mismatch"):
        _verify_checksum(archive_path, wrong)

    assert not archive_path.exists()


def test_extract_model_accepts_expected_directory_layout(tmp_path):
    model_dir = tmp_path / "models" / "ner_rus_bert_torch"
    model_dir.parent.mkdir()
    archive_path = tmp_path / "model.tar.gz"

    with tarfile.open(archive_path, "w:gz") as archive:
        _add_file(archive, "ner_rus_bert_torch/config.json", b"{}")
        _add_file(archive, "ner_rus_bert_torch/model.bin", b"weights")

    _extract_model(archive_path, model_dir)

    assert (model_dir / "config.json").read_bytes() == b"{}"
    assert (model_dir / "model.bin").read_bytes() == b"weights"


def test_extract_model_wraps_flat_archive_layout(tmp_path):
    model_dir = tmp_path / "models" / "ner_rus_bert_torch"
    model_dir.parent.mkdir()
    archive_path = tmp_path / "model.tar.gz"

    with tarfile.open(archive_path, "w:gz") as archive:
        _add_file(archive, "config.json", b"{}")
        _add_file(archive, "model.bin", b"weights")

    _extract_model(archive_path, model_dir)

    assert (model_dir / "config.json").read_bytes() == b"{}"
    assert (model_dir / "model.bin").read_bytes() == b"weights"
