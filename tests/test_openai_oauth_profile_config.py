"""Contracts for installation-local OpenAI OAuth profile configuration."""

import re
import subprocess
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]
EXAMPLE_PATH = ROOT / "config" / "openai-oauth" / "profiles.example.yaml"
PROFILE_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
EXPECTED_DEFAULT_MODELS = {
    "gpt-5.6-sol": "chatgpt/gpt-5.6-sol",
    "gpt-5.6-terra": "chatgpt/gpt-5.6-terra",
    "gpt-5.6-luna": "chatgpt/gpt-5.6-luna",
}
FORBIDDEN_DEFAULT_MODELS = {"gpt-5.4", "gpt-5.6", "gpt-5.3-codex"}
FORBIDDEN_KEYS = {
    "access_token",
    "account_id",
    "auth_file",
    "email",
    "path",
    "refresh_token",
    "token",
}


def _is_ignored(path: str) -> bool:
    result = subprocess.run(
        ["git", "check-ignore", "--quiet", path],
        cwd=ROOT,
        check=False,
    )
    assert result.returncode in (0, 1), f"git check-ignore failed for {path}"
    return result.returncode == 0


def _load_example() -> dict:
    data = yaml.safe_load(EXAMPLE_PATH.read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    return data


def _iter_mapping_keys(value):
    if isinstance(value, dict):
        yield from value
        for nested in value.values():
            yield from _iter_mapping_keys(nested)
    elif isinstance(value, list):
        for nested in value:
            yield from _iter_mapping_keys(nested)


def test_local_profile_configuration_and_auth_files_are_ignored():
    assert _is_ignored("config/openai-oauth/profiles.local.yaml")
    assert _is_ignored("config/generated/litellm-config.local.yaml")
    assert _is_ignored("secrets/openai-oauth/account-a/auth.json")


def test_tracked_profile_example_path_remains_available():
    assert EXAMPLE_PATH.is_file()
    assert not _is_ignored("config/openai-oauth/profiles.example.yaml")


def test_profile_pool_does_not_expand_quick_start_environment():
    env_example = (ROOT / ".env.example").read_text(encoding="utf-8")

    assert "OPENAI_OAUTH" not in env_example


def test_configuration_reference_defines_local_profile_boundary():
    configuration = (ROOT / "docs" / "configuration.md").read_text(encoding="utf-8")

    for expected in (
        "## Локальные профили OpenAI OAuth",
        "config/openai-oauth/profiles.local.yaml",
        "secrets/openai-oauth/<profile-id>/auth.json",
        "schema_version",
        "models",
        "profiles",
        "config/openai-oauth/profiles.example.yaml",
        "--replace",
        "того же `account_id`",
        ".import.lock",
        "scripts/generate_openai_oauth_config.py",
        "scripts/validate_openai_oauth_setup.py",
        "scripts/apply_openai_oauth_profiles.py",
        "docker-compose.openai-oauth.yml",
        "config/generated/litellm-config.local.yaml",
        "модель × включённый профиль",
        "Код возврата `0` означает успех, `1` — запрет запуска",
        "относятся к одному `account_id`",
        "--force-recreate --no-deps --pull never --wait litellm",
    ):
        assert expected in configuration


def test_profile_example_has_a_strict_versioned_schema():
    data = _load_example()

    assert set(data) == {"schema_version", "models", "profiles"}
    assert data["schema_version"] == 1
    assert isinstance(data["models"], list) and data["models"]
    assert isinstance(data["profiles"], list) and data["profiles"]


def test_profile_example_defines_unique_supported_models():
    models = _load_example()["models"]

    assert all(set(model) == {"public_name", "provider_model"} for model in models)
    public_names = [model["public_name"] for model in models]
    provider_models = [model["provider_model"] for model in models]

    assert len(public_names) == len(set(public_names))
    assert len(provider_models) == len(set(provider_models))
    assert all(name and "/" not in name for name in public_names)
    assert all(model.startswith("chatgpt/") for model in provider_models)
    assert dict(zip(public_names, provider_models)) == EXPECTED_DEFAULT_MODELS
    assert FORBIDDEN_DEFAULT_MODELS.isdisjoint(public_names)


def test_profile_example_defines_two_unique_enabled_profiles():
    profiles = _load_example()["profiles"]

    assert all(set(profile) == {"id", "enabled"} for profile in profiles)
    profile_ids = [profile["id"] for profile in profiles]

    assert len(profile_ids) == len(set(profile_ids))
    assert len(profiles) >= 2
    assert all(PROFILE_ID_PATTERN.fullmatch(profile_id) for profile_id in profile_ids)
    assert all(profile["enabled"] is True for profile in profiles)


def test_every_enabled_profile_uses_the_same_global_model_set():
    data = _load_example()
    global_models = frozenset(model["public_name"] for model in data["models"])
    profile_models = {
        profile["id"]: global_models
        for profile in data["profiles"]
        if profile["enabled"]
    }

    assert profile_models
    assert set(profile_models.values()) == {global_models}
    assert all("models" not in profile for profile in data["profiles"])


def test_profile_example_cannot_contain_secrets_or_paths():
    keys = set(_iter_mapping_keys(_load_example()))

    assert keys.isdisjoint(FORBIDDEN_KEYS)
