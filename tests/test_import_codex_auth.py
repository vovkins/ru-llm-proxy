"""Tests for safe conversion of an isolated Codex OAuth session."""

import base64
import json
import os
import stat
import time
from pathlib import Path

import pytest

from scripts import import_codex_auth as importer


def _jwt(payload: dict) -> str:
    def encode(value: dict) -> str:
        raw = json.dumps(value, separators=(",", ":")).encode("utf-8")
        return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")

    return f"{encode({'alg': 'none', 'typ': 'JWT'})}.{encode(payload)}.signature"


def _write_profiles(path: Path) -> None:
    path.write_text(
        """schema_version: 1
models:
  - public_name: gpt-5.4
    provider_model: chatgpt/gpt-5.4
profiles:
  - id: subscription-a
    enabled: true
  - id: subscription-disabled
    enabled: false
""",
        encoding="utf-8",
    )


def _codex_auth(*, account_id: str = "account-a") -> tuple[dict, dict[str, str]]:
    auth_claim = {"https://api.openai.com/auth": {"chatgpt_account_id": account_id}}
    tokens = {
        "access_token": _jwt({"exp": time.time() + 3600, **auth_claim}),
        "refresh_token": "refresh-secret-value",
        "id_token": _jwt(auth_claim),
        "account_id": account_id,
    }
    return (
        {
            "auth_mode": "chatgpt",
            "OPENAI_API_KEY": None,
            "tokens": tokens,
            "last_refresh": "2026-08-18T00:00:00Z",
        },
        tokens,
    )


def _write_source(path: Path, data: dict) -> bytes:
    raw = json.dumps(data).encode("utf-8")
    path.write_bytes(raw)
    path.chmod(0o600)
    return raw


@pytest.fixture
def import_files(tmp_path):
    profiles_path = tmp_path / "profiles.local.yaml"
    source = tmp_path / "codex-auth.json"
    secrets_root = tmp_path / "secrets" / "openai-oauth"
    _write_profiles(profiles_path)
    auth_data, tokens = _codex_auth()
    source_raw = _write_source(source, auth_data)
    return profiles_path, source, source_raw, secrets_root, tokens


def test_import_converts_codex_tokens_and_preserves_source(import_files):
    profiles_path, source, source_raw, secrets_root, tokens = import_files

    destination = importer.import_codex_auth(
        profile_id="subscription-a",
        source=source,
        profiles_path=profiles_path,
        secrets_root=secrets_root,
    )

    imported = json.loads(destination.read_text(encoding="utf-8"))
    assert set(imported) == {
        "access_token",
        "refresh_token",
        "id_token",
        "expires_at",
        "account_id",
    }
    assert imported["access_token"] == tokens["access_token"]
    assert imported["refresh_token"] == tokens["refresh_token"]
    assert imported["id_token"] == tokens["id_token"]
    assert imported["account_id"] == tokens["account_id"]
    assert imported["expires_at"] > time.time()
    assert source.read_bytes() == source_raw
    assert stat.S_IMODE(secrets_root.stat().st_mode) == 0o700
    assert stat.S_IMODE(destination.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(destination.stat().st_mode) == 0o600


@pytest.mark.parametrize("profile_id", ["missing", "subscription-disabled"])
def test_import_rejects_unknown_or_disabled_profile(import_files, profile_id):
    profiles_path, source, _, secrets_root, _ = import_files

    with pytest.raises(importer.AuthImportError):
        importer.import_codex_auth(
            profile_id=profile_id,
            source=source,
            profiles_path=profiles_path,
            secrets_root=secrets_root,
        )


@pytest.mark.parametrize("profile_id", ["../escape", "UPPERCASE", "-leading"])
def test_import_rejects_invalid_profile_id(import_files, profile_id):
    profiles_path, source, _, secrets_root, _ = import_files

    with pytest.raises(importer.AuthImportError, match="invalid format"):
        importer.import_codex_auth(
            profile_id=profile_id,
            source=source,
            profiles_path=profiles_path,
            secrets_root=secrets_root,
        )


def test_import_rejects_non_chatgpt_and_api_key_auth(import_files):
    profiles_path, source, _, secrets_root, _ = import_files
    auth_data, _ = _codex_auth()
    auth_data["auth_mode"] = "apikey"
    _write_source(source, auth_data)

    with pytest.raises(importer.AuthImportError, match="chatgpt authentication"):
        importer.import_codex_auth(
            profile_id="subscription-a",
            source=source,
            profiles_path=profiles_path,
            secrets_root=secrets_root,
        )

    auth_data["auth_mode"] = "chatgpt"
    auth_data["OPENAI_API_KEY"] = "sk-must-not-be-used"
    _write_source(source, auth_data)
    with pytest.raises(importer.AuthImportError, match="API key authentication"):
        importer.import_codex_auth(
            profile_id="subscription-a",
            source=source,
            profiles_path=profiles_path,
            secrets_root=secrets_root,
        )


@pytest.mark.parametrize("field", ["access_token", "refresh_token", "id_token"])
def test_import_rejects_missing_tokens(import_files, field):
    profiles_path, source, _, secrets_root, _ = import_files
    auth_data, _ = _codex_auth()
    del auth_data["tokens"][field]
    _write_source(source, auth_data)

    with pytest.raises(importer.AuthImportError, match=field):
        importer.import_codex_auth(
            profile_id="subscription-a",
            source=source,
            profiles_path=profiles_path,
            secrets_root=secrets_root,
        )


def test_import_rejects_malformed_json_and_jwt(import_files):
    profiles_path, source, _, secrets_root, _ = import_files
    source.write_text("{", encoding="utf-8")

    with pytest.raises(importer.AuthImportError, match="valid JSON"):
        importer.import_codex_auth(
            profile_id="subscription-a",
            source=source,
            profiles_path=profiles_path,
            secrets_root=secrets_root,
        )

    auth_data, _ = _codex_auth()
    auth_data["tokens"]["access_token"] = "not-a-jwt"
    _write_source(source, auth_data)
    with pytest.raises(importer.AuthImportError, match="valid JWT"):
        importer.import_codex_auth(
            profile_id="subscription-a",
            source=source,
            profiles_path=profiles_path,
            secrets_root=secrets_root,
        )


def test_import_rejects_inconsistent_account_ids(import_files):
    profiles_path, source, _, secrets_root, _ = import_files
    auth_data, _ = _codex_auth()
    auth_data["tokens"]["account_id"] = "different-account"
    _write_source(source, auth_data)

    with pytest.raises(importer.AuthImportError, match="inconsistent"):
        importer.import_codex_auth(
            profile_id="subscription-a",
            source=source,
            profiles_path=profiles_path,
            secrets_root=secrets_root,
        )


def test_import_rejects_source_symlink(import_files, tmp_path):
    profiles_path, source, _, secrets_root, _ = import_files
    source_link = tmp_path / "source-link.json"
    source_link.symlink_to(source)

    with pytest.raises(importer.AuthImportError, match="symbolic link"):
        importer.import_codex_auth(
            profile_id="subscription-a",
            source=source_link,
            profiles_path=profiles_path,
            secrets_root=secrets_root,
        )


def test_import_rejects_oversized_source(import_files):
    profiles_path, source, _, secrets_root, _ = import_files
    source.write_bytes(b"x" * (importer.MAX_INPUT_FILE_BYTES + 1))

    with pytest.raises(importer.AuthImportError, match="size limit"):
        importer.import_codex_auth(
            profile_id="subscription-a",
            source=source,
            profiles_path=profiles_path,
            secrets_root=secrets_root,
        )


def test_import_allows_expired_access_token_for_litellm_refresh(import_files):
    profiles_path, source, _, secrets_root, _ = import_files
    auth_data, _ = _codex_auth()
    auth_claim = {
        "https://api.openai.com/auth": {"chatgpt_account_id": "account-a"}
    }
    auth_data["tokens"]["access_token"] = _jwt({"exp": time.time() - 60, **auth_claim})
    _write_source(source, auth_data)

    destination = importer.import_codex_auth(
        profile_id="subscription-a",
        source=source,
        profiles_path=profiles_path,
        secrets_root=secrets_root,
    )

    imported = json.loads(destination.read_text(encoding="utf-8"))
    assert imported["expires_at"] < time.time()
    assert imported["refresh_token"] == auth_data["tokens"]["refresh_token"]


def test_import_refuses_to_replace_existing_profile(import_files):
    profiles_path, source, _, secrets_root, _ = import_files
    arguments = {
        "profile_id": "subscription-a",
        "source": source,
        "profiles_path": profiles_path,
        "secrets_root": secrets_root,
    }
    destination = importer.import_codex_auth(**arguments)
    original = destination.read_bytes()

    with pytest.raises(importer.AuthImportError, match="already exists"):
        importer.import_codex_auth(**arguments)

    assert destination.read_bytes() == original


def test_failed_atomic_replace_leaves_no_partial_auth_file(import_files, monkeypatch):
    profiles_path, source, _, secrets_root, _ = import_files

    def fail_replace(source_path, destination_path):
        raise OSError("synthetic replace failure")

    monkeypatch.setattr(importer.os, "replace", fail_replace)
    with pytest.raises(importer.AuthImportError, match="cannot write"):
        importer.import_codex_auth(
            profile_id="subscription-a",
            source=source,
            profiles_path=profiles_path,
            secrets_root=secrets_root,
        )

    profile_dir = secrets_root / "subscription-a"
    assert not (profile_dir / "auth.json").exists()
    assert not list(profile_dir.glob(".auth.json.*"))


def test_cli_output_does_not_disclose_tokens_or_account_id(import_files, capsys):
    profiles_path, source, _, secrets_root, tokens = import_files

    exit_code = importer.main(
        [
            "--profile-id",
            "subscription-a",
            "--source",
            str(source),
            "--profiles-file",
            str(profiles_path),
            "--secrets-dir",
            str(secrets_root),
        ]
    )

    output = "\n".join(capsys.readouterr())
    assert exit_code == 0
    for secret in tokens.values():
        assert secret not in output
