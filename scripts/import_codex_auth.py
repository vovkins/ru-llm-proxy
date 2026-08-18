#!/usr/bin/env python3
"""Import an isolated Codex OAuth session into a LiteLLM profile."""

from __future__ import annotations

import argparse
import base64
import binascii
import fcntl
import json
import math
import os
import re
import stat
import sys
import tempfile
from pathlib import Path
from typing import Any, Sequence

import yaml


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PROFILES_PATH = ROOT / "config" / "openai-oauth" / "profiles.local.yaml"
DEFAULT_SECRETS_ROOT = ROOT / "secrets" / "openai-oauth"
PROFILE_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
MAX_INPUT_FILE_BYTES = 128 * 1024
EXPECTED_TOKEN_FIELDS = ("access_token", "refresh_token", "id_token")


class AuthImportError(Exception):
    """A validation or filesystem error safe to show to an operator."""


def _require_regular_file(path: Path, label: str) -> os.stat_result:
    try:
        file_stat = path.lstat()
    except FileNotFoundError as exc:
        raise AuthImportError(f"{label} does not exist") from exc
    except OSError as exc:
        raise AuthImportError(f"cannot inspect {label}") from exc

    if stat.S_ISLNK(file_stat.st_mode):
        raise AuthImportError(f"{label} must not be a symbolic link")
    if not stat.S_ISREG(file_stat.st_mode):
        raise AuthImportError(f"{label} must be a regular file")
    if file_stat.st_size <= 0:
        raise AuthImportError(f"{label} is empty")
    if file_stat.st_size > MAX_INPUT_FILE_BYTES:
        raise AuthImportError(f"{label} exceeds the size limit")
    return file_stat


def _read_regular_text(path: Path, label: str) -> str:
    expected_stat = _require_regular_file(path, label)
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        file_fd = os.open(path, flags)
    except OSError as exc:
        raise AuthImportError(f"cannot open {label} safely") from exc

    try:
        opened_stat = os.fstat(file_fd)
        if not stat.S_ISREG(opened_stat.st_mode):
            raise AuthImportError(f"{label} must be a regular file")
        if (opened_stat.st_dev, opened_stat.st_ino) != (
            expected_stat.st_dev,
            expected_stat.st_ino,
        ):
            raise AuthImportError(f"{label} changed during validation")
        if opened_stat.st_size <= 0:
            raise AuthImportError(f"{label} is empty")
        if opened_stat.st_size > MAX_INPUT_FILE_BYTES:
            raise AuthImportError(f"{label} exceeds the size limit")
        with os.fdopen(file_fd, "r", encoding="utf-8") as handle:
            file_fd = -1
            return handle.read(MAX_INPUT_FILE_BYTES + 1)
    except UnicodeError as exc:
        raise AuthImportError(f"{label} is not valid UTF-8") from exc
    finally:
        if file_fd >= 0:
            os.close(file_fd)


def _load_profiles(path: Path) -> list[dict[str, Any]]:
    try:
        data = yaml.safe_load(_read_regular_text(path, "profile configuration"))
    except yaml.YAMLError as exc:
        raise AuthImportError("profile configuration is not valid YAML") from exc

    if not isinstance(data, dict) or data.get("schema_version") != 1:
        raise AuthImportError("profile configuration must use schema version 1")
    profiles = data.get("profiles")
    if not isinstance(profiles, list):
        raise AuthImportError("profile configuration must contain a profiles list")

    validated: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for profile in profiles:
        if not isinstance(profile, dict) or set(profile) != {"id", "enabled"}:
            raise AuthImportError("each profile must contain only id and enabled")
        profile_id = profile.get("id")
        enabled = profile.get("enabled")
        if not isinstance(profile_id, str) or not PROFILE_ID_PATTERN.fullmatch(profile_id):
            raise AuthImportError("profile configuration contains an invalid id")
        if not isinstance(enabled, bool):
            raise AuthImportError("profile enabled must be a boolean")
        if profile_id in seen_ids:
            raise AuthImportError("profile configuration contains duplicate ids")
        seen_ids.add(profile_id)
        validated.append(profile)
    return validated


def _require_enabled_profile(path: Path, profile_id: str) -> None:
    if not PROFILE_ID_PATTERN.fullmatch(profile_id):
        raise AuthImportError("profile id has an invalid format")

    matching = [profile for profile in _load_profiles(path) if profile["id"] == profile_id]
    if not matching:
        raise AuthImportError("profile is not present in the local configuration")
    if matching[0]["enabled"] is not True:
        raise AuthImportError("profile is disabled in the local configuration")


def _decode_jwt_claims(token: str, label: str) -> dict[str, Any]:
    parts = token.split(".")
    if len(parts) != 3 or not parts[1]:
        raise AuthImportError(f"{label} is not a valid JWT")

    payload = parts[1] + "=" * (-len(parts[1]) % 4)
    try:
        decoded = base64.urlsafe_b64decode(payload.encode("ascii"))
        claims = json.loads(decoded.decode("utf-8"))
    except (UnicodeError, ValueError, binascii.Error, json.JSONDecodeError) as exc:
        raise AuthImportError(f"{label} is not a valid JWT") from exc
    if not isinstance(claims, dict):
        raise AuthImportError(f"{label} JWT payload must be an object")
    return claims


def _account_id_from_claims(claims: dict[str, Any]) -> str | None:
    auth_claims = claims.get("https://api.openai.com/auth")
    if not isinstance(auth_claims, dict):
        return None
    account_id = auth_claims.get("chatgpt_account_id")
    if isinstance(account_id, str) and account_id and account_id == account_id.strip():
        return account_id
    return None


def _load_codex_auth(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(_read_regular_text(path, "Codex auth file"))
    except json.JSONDecodeError as exc:
        raise AuthImportError("Codex auth file is not valid JSON") from exc

    if not isinstance(data, dict):
        raise AuthImportError("Codex auth file must contain a JSON object")
    if data.get("auth_mode") != "chatgpt":
        raise AuthImportError("Codex auth file must use chatgpt authentication")
    if data.get("OPENAI_API_KEY") not in (None, ""):
        raise AuthImportError("API key authentication cannot be imported as an OAuth profile")

    tokens = data.get("tokens")
    if not isinstance(tokens, dict):
        raise AuthImportError("Codex auth file must contain a tokens object")

    normalized_tokens: dict[str, str] = {}
    for field in EXPECTED_TOKEN_FIELDS:
        value = tokens.get(field)
        if not isinstance(value, str) or not value or value != value.strip():
            raise AuthImportError(f"Codex auth file has an invalid {field}")
        normalized_tokens[field] = value

    access_claims = _decode_jwt_claims(normalized_tokens["access_token"], "access token")
    id_claims = _decode_jwt_claims(normalized_tokens["id_token"], "id token")
    expires_at = access_claims.get("exp")
    if (
        isinstance(expires_at, bool)
        or not isinstance(expires_at, (int, float))
        or not math.isfinite(expires_at)
        or expires_at <= 0
    ):
        raise AuthImportError("access token does not contain a valid expiry")

    declared_account_id = tokens.get("account_id")
    if declared_account_id is not None and (
        not isinstance(declared_account_id, str)
        or not declared_account_id
        or declared_account_id != declared_account_id.strip()
    ):
        raise AuthImportError("Codex auth file has an invalid account identifier")

    account_ids = {
        value
        for value in (
            declared_account_id,
            _account_id_from_claims(access_claims),
            _account_id_from_claims(id_claims),
        )
        if value is not None
    }
    if not account_ids:
        raise AuthImportError("Codex auth file does not contain an account identifier")
    if len(account_ids) != 1:
        raise AuthImportError("Codex auth file contains inconsistent account identifiers")

    return {
        "access_token": normalized_tokens["access_token"],
        "refresh_token": normalized_tokens["refresh_token"],
        "id_token": normalized_tokens["id_token"],
        "expires_at": int(expires_at),
        "account_id": account_ids.pop(),
    }


def _ensure_private_directory(path: Path) -> None:
    try:
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
        if path.is_symlink() or not path.is_dir():
            raise AuthImportError("secret destination must be a regular directory")
        path.chmod(0o700)
    except AuthImportError:
        raise
    except OSError as exc:
        raise AuthImportError("cannot prepare secret destination") from exc


def _fsync_directory(path: Path) -> None:
    directory_fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def _same_file(left: Path, right: Path) -> bool:
    try:
        return left.samefile(right)
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise AuthImportError("cannot compare source and destination files") from exc


def _load_existing_account_id(path: Path) -> str:
    try:
        data = json.loads(_read_regular_text(path, "existing profile auth file"))
    except json.JSONDecodeError as exc:
        raise AuthImportError("existing profile auth file is not valid JSON") from exc
    if not isinstance(data, dict):
        raise AuthImportError("existing profile auth file must contain a JSON object")
    account_id = data.get("account_id")
    if (
        not isinstance(account_id, str)
        or not account_id
        or account_id != account_id.strip()
    ):
        raise AuthImportError("existing profile auth file has an invalid account identifier")
    return account_id


def _write_auth(
    destination: Path,
    auth_data: dict[str, Any],
    *,
    replace: bool,
    source: Path,
) -> None:
    profile_dir = destination.parent
    _ensure_private_directory(profile_dir)
    lock_path = profile_dir / ".import.lock"
    lock_flags = (
        os.O_CREAT
        | os.O_RDWR
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        lock_fd = os.open(lock_path, lock_flags, 0o600)
    except OSError as exc:
        raise AuthImportError("cannot lock profile secret destination") from exc
    temp_path: Path | None = None
    try:
        os.fchmod(lock_fd, 0o600)
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        destination_exists = destination.exists() or destination.is_symlink()
        if replace:
            if not destination_exists:
                raise AuthImportError("profile auth file does not exist; omit --replace")
            if _same_file(source, destination):
                raise AuthImportError("source must not be the active profile auth file")
            existing_account_id = _load_existing_account_id(destination)
            if existing_account_id != auth_data["account_id"]:
                raise AuthImportError("replacement belongs to a different account")
        elif destination_exists:
            raise AuthImportError("profile auth file already exists")

        temp_fd, temp_name = tempfile.mkstemp(prefix=".auth.json.", dir=profile_dir)
        temp_path = Path(temp_name)
        with os.fdopen(temp_fd, "w", encoding="utf-8") as handle:
            os.fchmod(handle.fileno(), 0o600)
            json.dump(auth_data, handle, ensure_ascii=True, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())

        if not replace and (destination.exists() or destination.is_symlink()):
            raise AuthImportError("profile auth file already exists")
        os.replace(temp_path, destination)
        temp_path = None
        destination.chmod(0o600)
        _fsync_directory(profile_dir)
    except AuthImportError:
        raise
    except OSError as exc:
        raise AuthImportError("cannot write profile auth file") from exc
    finally:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)
        os.close(lock_fd)


def import_codex_auth(
    *,
    profile_id: str,
    source: Path,
    profiles_path: Path = DEFAULT_PROFILES_PATH,
    secrets_root: Path = DEFAULT_SECRETS_ROOT,
    replace: bool = False,
) -> Path:
    """Validate and import one Codex session without modifying the source file."""

    _require_enabled_profile(profiles_path, profile_id)
    _ensure_private_directory(secrets_root)
    destination = secrets_root / profile_id / "auth.json"
    if destination.exists() and _same_file(source, destination):
        raise AuthImportError("source must not be the active profile auth file")
    auth_data = _load_codex_auth(source)
    _write_auth(destination, auth_data, replace=replace, source=source)
    return destination


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Import an isolated Codex OAuth auth.json into a LiteLLM profile.",
    )
    parser.add_argument("--profile-id", required=True, help="Enabled profile id")
    parser.add_argument("--source", required=True, type=Path, help="Codex auth.json path")
    parser.add_argument(
        "--replace",
        action="store_true",
        help="Atomically replace an existing session for the same account",
    )
    parser.add_argument(
        "--profiles-file",
        type=Path,
        default=DEFAULT_PROFILES_PATH,
        help="Local profile configuration path",
    )
    parser.add_argument(
        "--secrets-dir",
        type=Path,
        default=DEFAULT_SECRETS_ROOT,
        help="Destination root for profile auth files",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        destination = import_codex_auth(
            profile_id=args.profile_id,
            source=args.source,
            profiles_path=args.profiles_file,
            secrets_root=args.secrets_dir,
            replace=args.replace,
        )
    except AuthImportError as exc:
        print(f"Import failed: {exc}", file=sys.stderr)
        return 1

    action = "Updated" if args.replace else "Imported"
    print(f"{action} OAuth profile '{args.profile_id}' in {destination}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
