"""Tests for safe DeepPavlov model archive handling."""

import hashlib
import tarfile
from io import BytesIO

import pytest

from download_model import _safe_extract, _validate_url, _verify_checksum


def test_validate_url_rejects_local_paths():
    with pytest.raises(ValueError, match="Unsupported model URL"):
        _validate_url("file:///tmp/model.tar.gz")


def test_safe_extract_rejects_path_traversal(tmp_path):
    archive_path = tmp_path / "model.tar.gz"
    with tarfile.open(archive_path, "w:gz") as archive:
        payload = b"bad"
        member = tarfile.TarInfo("../escape.txt")
        member.size = len(payload)
        archive.addfile(member, BytesIO(payload))

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
