"""Contracts for installation-local OpenAI OAuth profile configuration."""

import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _is_ignored(path: str) -> bool:
    result = subprocess.run(
        ["git", "check-ignore", "--quiet", path],
        cwd=ROOT,
        check=False,
    )
    assert result.returncode in (0, 1), f"git check-ignore failed for {path}"
    return result.returncode == 0


def test_local_profile_configuration_and_auth_files_are_ignored():
    assert _is_ignored("config/openai-oauth/profiles.local.yaml")
    assert _is_ignored("secrets/openai-oauth/account-a/auth.json")


def test_tracked_profile_example_path_remains_available():
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
    ):
        assert expected in configuration
