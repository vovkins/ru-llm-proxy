#!/usr/bin/env python3
"""Validate generated LiteLLM config and imported OpenAI OAuth profiles."""

from __future__ import annotations

import argparse
import base64
import binascii
import fcntl
import json
import math
import os
import stat
import sys
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator, Sequence

import yaml

if __package__:
    from . import generate_openai_oauth_config as config_generator
else:
    import generate_openai_oauth_config as config_generator


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_BASE_CONFIG = ROOT / "litellm-config.yaml"
DEFAULT_PROFILES_PATH = ROOT / "config" / "openai-oauth" / "profiles.local.yaml"
DEFAULT_GENERATED_CONFIG = ROOT / "config" / "generated" / "litellm-config.local.yaml"
DEFAULT_SECRETS_ROOT = ROOT / "secrets" / "openai-oauth"
MAX_AUTH_FILE_BYTES = 128 * 1024
MAX_GENERATED_CONFIG_BYTES = 2 * 1024 * 1024
AUTH_FIELDS = {
    "access_token",
    "refresh_token",
    "id_token",
    "expires_at",
    "account_id",
}


class SetupValidationError(Exception):
    """A validation or filesystem error safe to show to an operator."""


@dataclass(frozen=True)
class ValidationSummary:
    """Non-sensitive facts about a successful validation."""

    profiles: int
    oauth_deployments: int


@dataclass(frozen=True)
class _AuthIdentity:
    account_id: str
    inode: tuple[int, int]


def _require_directory(path: Path, label: str, *, mode: int) -> os.stat_result:
    try:
        path_stat = path.lstat()
    except FileNotFoundError as exc:
        raise SetupValidationError(f"{label} does not exist") from exc
    except OSError as exc:
        raise SetupValidationError(f"cannot inspect {label}") from exc

    if stat.S_ISLNK(path_stat.st_mode):
        raise SetupValidationError(f"{label} must not be a symbolic link")
    if not stat.S_ISDIR(path_stat.st_mode):
        raise SetupValidationError(f"{label} must be a directory")
    if stat.S_IMODE(path_stat.st_mode) != mode:
        raise SetupValidationError(f"{label} must have mode {mode:04o}")
    return path_stat


def _require_regular_metadata(
    path: Path,
    label: str,
    *,
    exact_mode: int | None = None,
    reject_group_or_other_write: bool = False,
    require_nonempty: bool = True,
    size_limit: int,
) -> os.stat_result:
    try:
        path_stat = path.lstat()
    except FileNotFoundError as exc:
        raise SetupValidationError(f"{label} does not exist") from exc
    except OSError as exc:
        raise SetupValidationError(f"cannot inspect {label}") from exc

    if stat.S_ISLNK(path_stat.st_mode):
        raise SetupValidationError(f"{label} must not be a symbolic link")
    if not stat.S_ISREG(path_stat.st_mode):
        raise SetupValidationError(f"{label} must be a regular file")
    if require_nonempty and path_stat.st_size <= 0:
        raise SetupValidationError(f"{label} is empty")
    if path_stat.st_size > size_limit:
        raise SetupValidationError(f"{label} exceeds the size limit")
    if path_stat.st_nlink != 1:
        raise SetupValidationError(f"{label} must not have hard links")

    file_mode = stat.S_IMODE(path_stat.st_mode)
    if exact_mode is not None and file_mode != exact_mode:
        raise SetupValidationError(f"{label} must have mode {exact_mode:04o}")
    if reject_group_or_other_write and file_mode & 0o022:
        raise SetupValidationError(f"{label} must not be group- or world-writable")
    return path_stat


def _read_regular_bytes(
    path: Path,
    label: str,
    *,
    exact_mode: int | None = None,
    reject_group_or_other_write: bool = False,
    size_limit: int,
) -> tuple[bytes, os.stat_result]:
    expected_stat = _require_regular_metadata(
        path,
        label,
        exact_mode=exact_mode,
        reject_group_or_other_write=reject_group_or_other_write,
        size_limit=size_limit,
    )
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        file_fd = os.open(path, flags)
    except OSError as exc:
        raise SetupValidationError(f"cannot open {label} safely") from exc

    try:
        opened_stat = os.fstat(file_fd)
        if (opened_stat.st_dev, opened_stat.st_ino) != (
            expected_stat.st_dev,
            expected_stat.st_ino,
        ):
            raise SetupValidationError(f"{label} changed during validation")
        with os.fdopen(file_fd, "rb") as handle:
            file_fd = -1
            try:
                raw = handle.read(size_limit + 1)
            except OSError as exc:
                raise SetupValidationError(f"cannot read {label}") from exc
        if len(raw) > size_limit:
            raise SetupValidationError(f"{label} exceeds the size limit")
        return raw, opened_stat
    finally:
        if file_fd >= 0:
            os.close(file_fd)


@contextmanager
def _profile_read_lock(lock_path: Path, profile_label: str) -> Iterator[None]:
    expected_stat = _require_regular_metadata(
        lock_path,
        f"{profile_label} lock file",
        exact_mode=0o600,
        require_nonempty=False,
        size_limit=4096,
    )
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        lock_fd = os.open(lock_path, flags)
    except OSError as exc:
        raise SetupValidationError(
            f"cannot open {profile_label} lock file safely"
        ) from exc
    try:
        opened_stat = os.fstat(lock_fd)
        if (opened_stat.st_dev, opened_stat.st_ino) != (
            expected_stat.st_dev,
            expected_stat.st_ino,
        ):
            raise SetupValidationError(
                f"{profile_label} lock file changed during validation"
            )
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_SH)
        except OSError as exc:
            raise SetupValidationError(
                f"cannot lock {profile_label} for validation"
            ) from exc
        yield
    finally:
        os.close(lock_fd)


def _decode_jwt_claims(token: str, profile_label: str, token_label: str) -> dict:
    parts = token.split(".")
    if len(parts) != 3 or not parts[1]:
        raise SetupValidationError(f"{profile_label} has an invalid {token_label} JWT")
    payload = parts[1] + "=" * (-len(parts[1]) % 4)
    try:
        decoded = base64.urlsafe_b64decode(payload.encode("ascii"))
        claims = json.loads(decoded.decode("utf-8"))
    except (UnicodeError, ValueError, binascii.Error, json.JSONDecodeError) as exc:
        raise SetupValidationError(
            f"{profile_label} has an invalid {token_label} JWT"
        ) from exc
    if not isinstance(claims, dict):
        raise SetupValidationError(
            f"{profile_label} has an invalid {token_label} JWT payload"
        )
    return claims


def _account_id_from_claims(claims: dict[str, Any]) -> str | None:
    auth_claims = claims.get("https://api.openai.com/auth")
    if not isinstance(auth_claims, dict):
        return None
    account_id = auth_claims.get("chatgpt_account_id")
    if isinstance(account_id, str) and account_id and account_id == account_id.strip():
        return account_id
    return None


def _load_auth_identity(
    auth_path: Path,
    profile_label: str,
) -> _AuthIdentity:
    raw, file_stat = _read_regular_bytes(
        auth_path,
        f"{profile_label} auth file",
        exact_mode=0o600,
        size_limit=MAX_AUTH_FILE_BYTES,
    )
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise SetupValidationError(
            f"{profile_label} auth file is not valid JSON"
        ) from exc
    if not isinstance(data, dict) or set(data) != AUTH_FIELDS:
        raise SetupValidationError(
            f"{profile_label} auth file must contain the expected fields"
        )

    tokens: dict[str, str] = {}
    for field in ("access_token", "refresh_token", "id_token"):
        value = data.get(field)
        if not isinstance(value, str) or not value or value != value.strip():
            raise SetupValidationError(
                f"{profile_label} auth file has an invalid {field}"
            )
        tokens[field] = value

    expires_at = data.get("expires_at")
    if (
        isinstance(expires_at, bool)
        or not isinstance(expires_at, int)
        or expires_at <= 0
    ):
        raise SetupValidationError(
            f"{profile_label} auth file has an invalid expires_at"
        )
    account_id = data.get("account_id")
    if (
        not isinstance(account_id, str)
        or not account_id
        or account_id != account_id.strip()
    ):
        raise SetupValidationError(
            f"{profile_label} auth file has an invalid account identifier"
        )

    access_claims = _decode_jwt_claims(
        tokens["access_token"], profile_label, "access token"
    )
    id_claims = _decode_jwt_claims(tokens["id_token"], profile_label, "id token")
    access_expiry = access_claims.get("exp")
    if (
        isinstance(access_expiry, bool)
        or not isinstance(access_expiry, (int, float))
        or not math.isfinite(access_expiry)
        or access_expiry <= 0
        or not float(access_expiry).is_integer()
        or int(access_expiry) != expires_at
    ):
        raise SetupValidationError(
            f"{profile_label} auth file expiry does not match the access token"
        )

    access_account_id = _account_id_from_claims(access_claims)
    id_account_id = _account_id_from_claims(id_claims)
    if (
        access_account_id is None
        or id_account_id is None
        or {account_id, access_account_id, id_account_id} != {account_id}
    ):
        raise SetupValidationError(
            f"{profile_label} auth file has inconsistent account identifiers"
        )
    return _AuthIdentity(
        account_id=account_id,
        inode=(file_stat.st_dev, file_stat.st_ino),
    )


def _build_expected_config(
    base_path: Path,
    profiles_path: Path,
) -> tuple[bytes, list[str], int]:
    try:
        with tempfile.TemporaryDirectory(
            prefix="ru-llm-proxy-oauth-preflight-"
        ) as root:
            output_path = Path(root) / "litellm-config.local.yaml"
            summary = config_generator.generate_config(
                base_path=base_path,
                profiles_path=profiles_path,
                output_path=output_path,
            )
            expected = output_path.read_bytes()
    except config_generator.ConfigGenerationError as exc:
        raise SetupValidationError(str(exc)) from exc
    except OSError as exc:
        raise SetupValidationError(
            "cannot prepare expected generated configuration"
        ) from exc

    try:
        generated = yaml.safe_load(expected)
    except yaml.YAMLError as exc:
        raise SetupValidationError(
            "generated configuration could not be parsed"
        ) from exc
    profile_ids = sorted(
        {
            model_info["openai_oauth_profile"]
            for deployment in generated["model_list"]
            if isinstance(deployment, dict)
            and isinstance((model_info := deployment.get("model_info")), dict)
            and isinstance(model_info.get("openai_oauth_profile"), str)
        }
    )
    if not profile_ids or summary.oauth_deployments <= 0:
        raise SetupValidationError(
            "generated configuration does not contain OAuth deployments"
        )
    return expected, profile_ids, summary.oauth_deployments


def validate_setup(
    *,
    base_path: Path = DEFAULT_BASE_CONFIG,
    profiles_path: Path = DEFAULT_PROFILES_PATH,
    generated_path: Path = DEFAULT_GENERATED_CONFIG,
    secrets_root: Path = DEFAULT_SECRETS_ROOT,
) -> ValidationSummary:
    """Validate that the local OAuth pool is safe and ready for LiteLLM."""

    expected, profile_ids, oauth_deployments = _build_expected_config(
        base_path,
        profiles_path,
    )
    actual, _ = _read_regular_bytes(
        generated_path,
        "generated LiteLLM config",
        reject_group_or_other_write=True,
        size_limit=MAX_GENERATED_CONFIG_BYTES,
    )
    if actual != expected:
        raise SetupValidationError(
            "generated LiteLLM config is stale; run the generator again"
        )

    _require_directory(secrets_root, "OAuth secrets root", mode=0o700)
    account_ids: set[str] = set()
    auth_inodes: set[tuple[int, int]] = set()
    for profile_id in profile_ids:
        profile_label = f"OAuth profile {profile_id}"
        profile_dir = secrets_root / profile_id
        _require_directory(profile_dir, f"{profile_label} directory", mode=0o700)
        with _profile_read_lock(profile_dir / ".import.lock", profile_label):
            identity = _load_auth_identity(profile_dir / "auth.json", profile_label)
        if identity.inode in auth_inodes:
            raise SetupValidationError(
                "enabled OAuth profiles must not share an auth file"
            )
        if identity.account_id in account_ids:
            raise SetupValidationError(
                "enabled OAuth profiles must use distinct account identifiers"
            )
        auth_inodes.add(identity.inode)
        account_ids.add(identity.account_id)

    return ValidationSummary(
        profiles=len(profile_ids),
        oauth_deployments=oauth_deployments,
    )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Validate generated LiteLLM config and OAuth profile secrets.",
    )
    parser.add_argument(
        "--base-config",
        type=Path,
        default=DEFAULT_BASE_CONFIG,
        help="Base LiteLLM YAML path",
    )
    parser.add_argument(
        "--profiles-file",
        type=Path,
        default=DEFAULT_PROFILES_PATH,
        help="Local OAuth profile YAML path",
    )
    parser.add_argument(
        "--generated-config",
        type=Path,
        default=DEFAULT_GENERATED_CONFIG,
        help="Generated LiteLLM YAML path",
    )
    parser.add_argument(
        "--secrets-dir",
        type=Path,
        default=DEFAULT_SECRETS_ROOT,
        help="Root directory containing profile auth files",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        summary = validate_setup(
            base_path=args.base_config,
            profiles_path=args.profiles_file,
            generated_path=args.generated_config,
            secrets_root=args.secrets_dir,
        )
    except SetupValidationError as exc:
        print(f"OAuth preflight failed: {exc}", file=sys.stderr)
        return 1

    print(
        "OAuth preflight passed: "
        f"{summary.profiles} profiles and "
        f"{summary.oauth_deployments} deployments validated"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
