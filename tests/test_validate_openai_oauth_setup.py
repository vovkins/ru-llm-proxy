"""Tests for the OpenAI OAuth startup preflight."""

from __future__ import annotations

import base64
import json
import os
from pathlib import Path

import pytest
import yaml

from scripts import generate_openai_oauth_config as generator
from scripts import validate_openai_oauth_setup as validator


def _encode_jwt(claims: dict) -> str:
    def encode(value: dict) -> str:
        raw = json.dumps(value, separators=(",", ":")).encode("utf-8")
        return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")

    return f"{encode({'alg': 'none'})}.{encode(claims)}.signature"


def _auth_data(account_id: str, *, expires_at: int = 1_700_000_000) -> dict:
    claims = {
        "exp": expires_at,
        "https://api.openai.com/auth": {
            "chatgpt_account_id": account_id,
        },
    }
    return {
        "access_token": _encode_jwt(claims),
        "refresh_token": f"refresh-{account_id}",
        "id_token": _encode_jwt(claims),
        "expires_at": expires_at,
        "account_id": account_id,
    }


def _base_config() -> dict:
    return {
        "model_list": [
            {
                "model_name": "glm-5.2",
                "litellm_params": {"model": "openai/glm-5.2"},
                "model_info": {"id": "glm-primary"},
            }
        ],
        "router_settings": {"routing_strategy": "simple-shuffle"},
    }


def _profiles(*, include_disabled: bool = False) -> dict:
    profiles = [
        {"id": "subscription-a", "enabled": True},
        {"id": "subscription-b", "enabled": True},
    ]
    if include_disabled:
        profiles.append({"id": "subscription-disabled", "enabled": False})
    return {
        "schema_version": 1,
        "models": [
            {"public_name": "gpt-5.4", "provider_model": "chatgpt/gpt-5.4"},
            {
                "public_name": "gpt-5.3-codex",
                "provider_model": "chatgpt/gpt-5.3-codex",
            },
        ],
        "profiles": profiles,
    }


def _write_yaml(path: Path, data: dict) -> None:
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")


def _write_profile_secret(
    secrets_root: Path,
    profile_id: str,
    account_id: str,
    *,
    auth_data: dict | None = None,
) -> Path:
    profile_dir = secrets_root / profile_id
    profile_dir.mkdir(mode=0o700)
    profile_dir.chmod(0o700)
    lock_path = profile_dir / ".import.lock"
    lock_path.touch(mode=0o600)
    lock_path.chmod(0o600)
    auth_path = profile_dir / "auth.json"
    auth_path.write_text(
        json.dumps(auth_data or _auth_data(account_id)),
        encoding="utf-8",
    )
    auth_path.chmod(0o600)
    return auth_path


def _valid_setup(tmp_path: Path, *, include_disabled: bool = False) -> dict:
    base_path = tmp_path / "base.yaml"
    profiles_path = tmp_path / "profiles.local.yaml"
    generated_path = tmp_path / "generated" / "litellm-config.local.yaml"
    secrets_root = tmp_path / "secrets"
    secrets_root.mkdir(mode=0o700)
    secrets_root.chmod(0o700)
    _write_yaml(base_path, _base_config())
    _write_yaml(profiles_path, _profiles(include_disabled=include_disabled))
    generator.generate_config(
        base_path=base_path,
        profiles_path=profiles_path,
        output_path=generated_path,
    )
    _write_profile_secret(secrets_root, "subscription-a", "account-a")
    _write_profile_secret(secrets_root, "subscription-b", "account-b")
    return {
        "base_path": base_path,
        "profiles_path": profiles_path,
        "generated_path": generated_path,
        "secrets_root": secrets_root,
    }


def _validate(paths: dict) -> validator.ValidationSummary:
    return validator.validate_setup(
        base_path=paths["base_path"],
        profiles_path=paths["profiles_path"],
        generated_path=paths["generated_path"],
        secrets_root=paths["secrets_root"],
    )


def _run_main(paths: dict) -> int:
    return validator.main(
        [
            "--base-config",
            str(paths["base_path"]),
            "--profiles-file",
            str(paths["profiles_path"]),
            "--generated-config",
            str(paths["generated_path"]),
            "--secrets-dir",
            str(paths["secrets_root"]),
        ]
    )


def test_valid_setup_accepts_expired_access_tokens_with_refresh_tokens(tmp_path):
    paths = _valid_setup(tmp_path)

    summary = _validate(paths)

    assert summary.profiles == 2
    assert summary.oauth_deployments == 4


def test_success_output_contains_only_non_sensitive_counts(tmp_path, capsys):
    paths = _valid_setup(tmp_path)

    assert _run_main(paths) == 0

    output = capsys.readouterr()
    assert "2 profiles and 4 deployments" in output.out
    assert output.err == ""
    for secret in ("account-a", "account-b", "refresh-account-a"):
        assert secret not in output.out


def test_disabled_profile_does_not_require_a_secret_directory(tmp_path):
    paths = _valid_setup(tmp_path, include_disabled=True)

    assert _validate(paths).profiles == 2
    assert not (paths["secrets_root"] / "subscription-disabled").exists()


def test_stale_generated_config_is_rejected(tmp_path):
    paths = _valid_setup(tmp_path)
    profiles = yaml.safe_load(paths["profiles_path"].read_text(encoding="utf-8"))
    profiles["models"][0]["provider_model"] = "chatgpt/gpt-5.4-updated"
    _write_yaml(paths["profiles_path"], profiles)

    with pytest.raises(validator.SetupValidationError, match="config is stale"):
        _validate(paths)


@pytest.mark.parametrize(
    ("missing_path", "message"),
    [
        ("generated_path", "generated LiteLLM config does not exist"),
        ("secrets_root", "OAuth secrets root does not exist"),
    ],
)
def test_required_setup_path_must_exist(tmp_path, missing_path, message):
    paths = _valid_setup(tmp_path)
    target = paths[missing_path]
    if target.is_dir():
        for child in sorted(target.rglob("*"), reverse=True):
            child.unlink() if child.is_file() else child.rmdir()
        target.rmdir()
    else:
        target.unlink()

    with pytest.raises(validator.SetupValidationError, match=message):
        _validate(paths)


@pytest.mark.parametrize(
    ("relative_path", "message"),
    [
        ("subscription-a", "OAuth profile subscription-a directory does not exist"),
        ("subscription-a/auth.json", "auth file does not exist"),
        ("subscription-a/.import.lock", "lock file does not exist"),
    ],
)
def test_every_enabled_profile_requires_complete_secret_files(
    tmp_path, relative_path, message
):
    paths = _valid_setup(tmp_path)
    target = paths["secrets_root"] / relative_path
    if target.is_dir():
        for child in target.iterdir():
            child.unlink()
        target.rmdir()
    else:
        target.unlink()

    with pytest.raises(validator.SetupValidationError, match=message):
        _validate(paths)


@pytest.mark.parametrize(
    ("relative_path", "message"),
    [
        ("subscription-a", "directory must have mode 0700"),
        ("subscription-a/auth.json", "auth file must have mode 0600"),
        ("subscription-a/.import.lock", "lock file must have mode 0600"),
    ],
)
def test_profile_secret_permissions_are_strict(tmp_path, relative_path, message):
    paths = _valid_setup(tmp_path)
    (paths["secrets_root"] / relative_path).chmod(
        0o755 if "." not in relative_path else 0o644
    )

    with pytest.raises(validator.SetupValidationError, match=message):
        _validate(paths)


def test_secrets_root_permissions_are_strict(tmp_path):
    paths = _valid_setup(tmp_path)
    paths["secrets_root"].chmod(0o755)

    with pytest.raises(
        validator.SetupValidationError,
        match="OAuth secrets root must have mode 0700",
    ):
        _validate(paths)


def test_generated_config_must_not_be_group_writable(tmp_path):
    paths = _valid_setup(tmp_path)
    paths["generated_path"].chmod(0o664)

    with pytest.raises(
        validator.SetupValidationError,
        match="must not be group- or world-writable",
    ):
        _validate(paths)


def test_generated_config_symbolic_link_is_rejected(tmp_path):
    paths = _valid_setup(tmp_path)
    generated_path = paths["generated_path"]
    target = tmp_path / "external-generated.yaml"
    generated_path.replace(target)
    generated_path.symlink_to(target)

    with pytest.raises(validator.SetupValidationError, match="symbolic link"):
        _validate(paths)


def test_auth_file_symbolic_link_is_rejected(tmp_path):
    paths = _valid_setup(tmp_path)
    auth_path = paths["secrets_root"] / "subscription-a" / "auth.json"
    target = tmp_path / "external-auth.json"
    auth_path.replace(target)
    auth_path.symlink_to(target)

    with pytest.raises(validator.SetupValidationError, match="symbolic link"):
        _validate(paths)


def test_auth_file_hard_link_is_rejected(tmp_path):
    paths = _valid_setup(tmp_path)
    auth_path = paths["secrets_root"] / "subscription-a" / "auth.json"
    os.link(auth_path, tmp_path / "auth-copy.json")

    with pytest.raises(validator.SetupValidationError, match="hard links"):
        _validate(paths)


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda data: data.update(unexpected="value"), "expected fields"),
        (
            lambda data: data.update(access_token="not-a-jwt"),
            "invalid access token JWT",
        ),
        (lambda data: data.update(expires_at=0), "invalid expires_at"),
        (lambda data: data.update(expires_at=1.5), "invalid expires_at"),
        (
            lambda data: data.update(expires_at=data["expires_at"] + 1),
            "expiry does not match",
        ),
        (
            lambda data: data.update(account_id="different-account"),
            "inconsistent account",
        ),
        (lambda data: data.update(refresh_token=""), "invalid refresh_token"),
    ],
)
def test_invalid_auth_content_is_rejected(tmp_path, mutate, message):
    paths = _valid_setup(tmp_path)
    auth_path = paths["secrets_root"] / "subscription-a" / "auth.json"
    data = json.loads(auth_path.read_text(encoding="utf-8"))
    mutate(data)
    auth_path.write_text(json.dumps(data), encoding="utf-8")
    auth_path.chmod(0o600)

    with pytest.raises(validator.SetupValidationError, match=message):
        _validate(paths)


def test_malformed_auth_json_is_rejected(tmp_path):
    paths = _valid_setup(tmp_path)
    auth_path = paths["secrets_root"] / "subscription-a" / "auth.json"
    auth_path.write_text("{", encoding="utf-8")

    with pytest.raises(validator.SetupValidationError, match="not valid JSON"):
        _validate(paths)


def test_profiles_must_represent_distinct_accounts_without_leaking_id(tmp_path):
    paths = _valid_setup(tmp_path)
    duplicate_account = "private-duplicate-account"
    for profile_id in ("subscription-a", "subscription-b"):
        auth_path = paths["secrets_root"] / profile_id / "auth.json"
        auth_path.write_text(
            json.dumps(_auth_data(duplicate_account)),
            encoding="utf-8",
        )
        auth_path.chmod(0o600)

    with pytest.raises(validator.SetupValidationError) as exc_info:
        _validate(paths)
    assert "distinct account identifiers" in str(exc_info.value)
    assert duplicate_account not in str(exc_info.value)


def test_failure_output_does_not_echo_secret_values(tmp_path, capsys):
    paths = _valid_setup(tmp_path)
    auth_path = paths["secrets_root"] / "subscription-a" / "auth.json"
    secret_value = "very-sensitive-refresh-token"
    data = json.loads(auth_path.read_text(encoding="utf-8"))
    data["refresh_token"] = f" {secret_value} "
    auth_path.write_text(json.dumps(data), encoding="utf-8")

    assert _run_main(paths) == 1

    output = capsys.readouterr()
    assert "invalid refresh_token" in output.err
    assert secret_value not in output.err
    assert output.out == ""


def test_static_suite_covers_oauth_preflight():
    makefile = (validator.ROOT / "Makefile").read_text(encoding="utf-8")

    assert "tests/test_validate_openai_oauth_setup.py" in makefile
