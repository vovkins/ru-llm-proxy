"""Tests for safe activation of the OpenAI OAuth Compose profile."""

from __future__ import annotations

import base64
import json
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

from scripts import apply_openai_oauth_profiles as apply_script


ROOT = Path(__file__).resolve().parents[1]
EXPECTED_OAUTH_IMAGE = (
    "ghcr.io/vovkins/litellm@"
    "sha256:6710a3968450117678e816fcf3705555c0fccef2bd0adc4d6e39af6ebb44cbe7"
)
PREVIOUS_OAUTH_IMAGE = (
    "ghcr.io/vovkins/litellm@"
    "sha256:771cf8e9081b1d3cef7b85cb9066e23d17bbcfc19e9617971dda487ec3c18abd"
)


def _encode_jwt(claims: dict) -> str:
    def encode(value: dict) -> str:
        raw = json.dumps(value, separators=(",", ":")).encode("utf-8")
        return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")

    return f"{encode({'alg': 'none'})}.{encode(claims)}.signature"


def _auth_data(account_id: str) -> dict:
    expires_at = 1_700_000_000
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


def _write_profile_secret(root: Path, profile_id: str, account_id: str) -> None:
    profile_dir = root / profile_id
    profile_dir.mkdir(mode=0o700)
    profile_dir.chmod(0o700)
    lock_path = profile_dir / ".import.lock"
    lock_path.touch(mode=0o600)
    lock_path.chmod(0o600)
    auth_path = profile_dir / "auth.json"
    auth_path.write_text(json.dumps(_auth_data(account_id)), encoding="utf-8")
    auth_path.chmod(0o600)


def _setup_paths(tmp_path: Path, *, previous_config: bytes | None = b"previous"):
    base_config = tmp_path / "litellm-config.yaml"
    profiles_path = tmp_path / "profiles.local.yaml"
    generated_path = tmp_path / "generated" / "litellm-config.local.yaml"
    secrets_root = tmp_path / "secrets"
    base_compose = tmp_path / "docker-compose.yml"
    oauth_compose = tmp_path / "docker-compose.openai-oauth.yml"

    base_config.write_text(
        yaml.safe_dump(
            {
                "model_list": [
                    {
                        "model_name": "glm-5.2",
                        "litellm_params": {"model": "openai/glm-5.2"},
                        "model_info": {"id": "glm-primary"},
                    }
                ],
                "router_settings": {"routing_strategy": "simple-shuffle"},
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    profiles_path.write_text(
        yaml.safe_dump(
            {
                "schema_version": 1,
                "models": [
                    {
                        "public_name": "gpt-5.4",
                        "provider_model": "chatgpt/gpt-5.4",
                    },
                    {
                        "public_name": "gpt-5.3-codex",
                        "provider_model": "chatgpt/gpt-5.3-codex",
                    },
                ],
                "profiles": [
                    {"id": "subscription-a", "enabled": True},
                    {"id": "subscription-b", "enabled": True},
                ],
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    secrets_root.mkdir(mode=0o700)
    secrets_root.chmod(0o700)
    _write_profile_secret(secrets_root, "subscription-a", "account-a")
    _write_profile_secret(secrets_root, "subscription-b", "account-b")
    base_compose.write_text(
        "services:\n  litellm:\n    image: base\n", encoding="utf-8"
    )
    oauth_compose.write_text(
        "services:\n  litellm:\n    image: oauth\n",
        encoding="utf-8",
    )
    if previous_config is not None:
        generated_path.parent.mkdir(mode=0o755)
        generated_path.write_bytes(previous_config)
        generated_path.chmod(0o644)

    return {
        "base_config": base_config,
        "profiles_path": profiles_path,
        "generated_path": generated_path,
        "secrets_root": secrets_root,
        "base_compose_file": base_compose,
        "oauth_compose_file": oauth_compose,
    }


class FakeRunner:
    def __init__(
        self,
        *,
        previous_mode: str = "base",
        previous_image: str | None = "docker.litellm.ai/berriai/litellm:main-stable",
        fail_config: bool = False,
        fail_pull: bool = False,
        fail_activation: bool = False,
        raise_activation: bool = False,
        fail_rollback: bool = False,
    ) -> None:
        self.previous_mode = previous_mode
        self.previous_image = None if previous_mode == "absent" else previous_image
        self.fail_config = fail_config
        self.fail_pull = fail_pull
        self.fail_activation = fail_activation
        self.raise_activation = raise_activation
        self.fail_rollback = fail_rollback
        self.calls: list[list[str]] = []
        self.up_calls = 0
        self.rollback_remove_calls = 0
        self.rollback_image: str | None = None

    def __call__(
        self,
        command,
        *,
        capture_output: bool = False,
    ) -> apply_script.CommandResult:
        del capture_output
        current = list(command)
        self.calls.append(current)

        if "config" in current:
            return apply_script.CommandResult(1 if self.fail_config else 0)
        if "pull" in current:
            return apply_script.CommandResult(1 if self.fail_pull else 0)
        if "ps" in current:
            container_id = "" if self.previous_image is None else "container-id\n"
            return apply_script.CommandResult(0, container_id)
        if current[:2] == ["docker", "inspect"]:
            if "{{.Config.Image}}" in current:
                return apply_script.CommandResult(0, f"{self.previous_image}\n")
            mounts = (
                f"/app/config.yaml\n{apply_script.OAUTH_SECRET_TARGET}\n"
                if self.previous_mode == "oauth"
                else "/app/config.yaml\n"
            )
            return apply_script.CommandResult(0, mounts)
        if "up" in current:
            self.up_calls += 1
            if self.up_calls > 1:
                compose_files = [
                    Path(current[index + 1])
                    for index, value in enumerate(current)
                    if value == "-f"
                ]
                rollback_override = compose_files[-1]
                if rollback_override.name == "previous-image.compose.yml":
                    self.rollback_image = yaml.safe_load(
                        rollback_override.read_text(encoding="utf-8")
                    )["services"]["litellm"]["image"]
            if self.up_calls == 1 and self.raise_activation:
                raise apply_script.ApplyProfilesError("synthetic execution failure")
            should_fail = (
                self.fail_activation if self.up_calls == 1 else self.fail_rollback
            )
            return apply_script.CommandResult(1 if should_fail else 0)
        if "rm" in current:
            self.rollback_remove_calls += 1
            return apply_script.CommandResult(1 if self.fail_rollback else 0)
        raise AssertionError(f"unexpected command: {current}")


def _apply(paths: dict, runner: FakeRunner) -> apply_script.ApplySummary:
    return apply_script.apply_profiles(**paths, runner=runner, wait_timeout=45)


def _up_calls(runner: FakeRunner) -> list[list[str]]:
    return [call for call in runner.calls if "up" in call]


def test_success_validates_then_recreates_only_litellm(tmp_path):
    paths = _setup_paths(tmp_path)
    runner = FakeRunner()

    summary = _apply(paths, runner)

    assert summary.profiles == 2
    assert summary.oauth_deployments == 4
    assert summary.previous_mode == "base"
    assert paths["generated_path"].read_bytes() != b"previous"
    calls = runner.calls
    assert calls[0][-2:] == ["config", "--quiet"]
    assert calls[1][-3:] == ["pull", "--quiet", "litellm"]
    up_call = _up_calls(runner)[0]
    for expected in (
        "--force-recreate",
        "--no-deps",
        "--pull",
        "never",
        "--wait",
        "--wait-timeout",
        "litellm",
    ):
        assert expected in up_call
    assert "down" not in up_call
    assert up_call[-1] == "litellm"


def test_preflight_failure_does_not_call_docker_or_change_config(tmp_path):
    paths = _setup_paths(tmp_path)
    shutil.rmtree(paths["secrets_root"])
    runner = FakeRunner()

    with pytest.raises(apply_script.ApplyProfilesError, match="secrets root"):
        _apply(paths, runner)

    assert runner.calls == []
    assert paths["generated_path"].read_bytes() == b"previous"


@pytest.mark.parametrize(
    ("failure_flag", "message"),
    [
        ("fail_config", "Compose configuration is invalid"),
        ("fail_pull", "cannot pull the pinned"),
    ],
)
def test_compose_preparation_failure_preserves_running_state(
    tmp_path, failure_flag, message
):
    paths = _setup_paths(tmp_path)
    runner = FakeRunner(**{failure_flag: True})

    with pytest.raises(apply_script.ApplyProfilesError, match=message):
        _apply(paths, runner)

    assert paths["generated_path"].read_bytes() == b"previous"
    assert _up_calls(runner) == []


def test_failed_first_activation_restores_base_mode_and_previous_config(tmp_path):
    paths = _setup_paths(tmp_path)
    runner = FakeRunner(fail_activation=True)

    with pytest.raises(apply_script.ApplyProfilesError, match="state was restored"):
        _apply(paths, runner)

    assert paths["generated_path"].read_bytes() == b"previous"
    activation, rollback = _up_calls(runner)
    assert str(paths["oauth_compose_file"]) in activation
    assert str(paths["oauth_compose_file"]) not in rollback
    assert rollback[-1] == "litellm"
    assert runner.rollback_image == "docker.litellm.ai/berriai/litellm:main-stable"


def test_failed_oauth_update_restores_previous_oauth_mode(tmp_path):
    paths = _setup_paths(tmp_path)
    runner = FakeRunner(
        previous_mode="oauth",
        previous_image=PREVIOUS_OAUTH_IMAGE,
        fail_activation=True,
    )

    with pytest.raises(apply_script.ApplyProfilesError, match="state was restored"):
        _apply(paths, runner)

    assert paths["generated_path"].read_bytes() == b"previous"
    activation, rollback = _up_calls(runner)
    assert str(paths["oauth_compose_file"]) in activation
    assert str(paths["oauth_compose_file"]) in rollback
    assert runner.rollback_image == PREVIOUS_OAUTH_IMAGE


def test_failed_initial_install_removes_failed_container_and_generated_config(tmp_path):
    paths = _setup_paths(tmp_path, previous_config=None)
    runner = FakeRunner(previous_mode="absent", fail_activation=True)

    with pytest.raises(apply_script.ApplyProfilesError, match="state was restored"):
        _apply(paths, runner)

    assert not paths["generated_path"].exists()
    assert runner.rollback_remove_calls == 1
    assert len(_up_calls(runner)) == 1


def test_rollback_failure_is_reported_separately(tmp_path):
    paths = _setup_paths(tmp_path)
    runner = FakeRunner(fail_activation=True, fail_rollback=True)

    with pytest.raises(apply_script.ApplyProfilesError, match="rollback also failed"):
        _apply(paths, runner)

    assert paths["generated_path"].read_bytes() == b"previous"


def test_docker_execution_error_also_triggers_rollback(tmp_path):
    paths = _setup_paths(tmp_path)
    runner = FakeRunner(raise_activation=True)

    with pytest.raises(apply_script.ApplyProfilesError, match="state was restored"):
        _apply(paths, runner)

    assert paths["generated_path"].read_bytes() == b"previous"
    assert len(_up_calls(runner)) == 2
    assert runner.rollback_image == "docker.litellm.ai/berriai/litellm:main-stable"


def test_existing_oauth_container_requires_a_previous_generated_config(tmp_path):
    paths = _setup_paths(tmp_path, previous_config=None)
    runner = FakeRunner(
        previous_mode="oauth",
        previous_image=EXPECTED_OAUTH_IMAGE,
    )

    with pytest.raises(apply_script.ApplyProfilesError, match="without the previous"):
        _apply(paths, runner)

    assert _up_calls(runner) == []
    assert not paths["generated_path"].exists()


def test_oauth_compose_override_has_a_minimal_secret_safe_contract():
    base = yaml.safe_load((ROOT / "docker-compose.yml").read_text(encoding="utf-8"))
    override = yaml.safe_load(
        (ROOT / "docker-compose.openai-oauth.yml").read_text(encoding="utf-8")
    )

    assert base["services"]["litellm"]["image"] != EXPECTED_OAUTH_IMAGE
    assert set(override) == {"services"}
    assert set(override["services"]) == {"litellm"}
    litellm = override["services"]["litellm"]
    assert litellm["image"] == EXPECTED_OAUTH_IMAGE
    assert litellm["volumes"] == [
        "./config/generated/litellm-config.local.yaml:/app/config.yaml:ro",
        "./secrets/openai-oauth:/run/secrets/openai-oauth",
    ]
    rendered = json.dumps(override)
    for forbidden in ("access_token", "refresh_token", "account_id"):
        assert forbidden not in rendered


def test_docker_compose_renders_replaced_config_and_inherited_guardrails(tmp_path):
    if shutil.which("docker") is None:
        pytest.skip("Docker Compose is not installed")
    env_file = tmp_path / "compose.env"
    env_file.write_text(
        "\n".join(
            (
                "LITELLM_MASTER_KEY=test-master",
                "LITELLM_SALT_KEY=test-salt",
                "UI_USERNAME=admin",
                "UI_PASSWORD=test-password",
                "LITELLM_DB_URL=postgresql://litellm:test@db:5432/litellm",
                "POSTGRES_PASSWORD=test-postgres",
                "ZAI_API_KEY=test-zai-a",
                "ZAI_API_KEY_2=test-zai-b",
            )
        ),
        encoding="utf-8",
    )
    result = subprocess.run(
        [
            "docker",
            "compose",
            "--env-file",
            str(env_file),
            "-f",
            str(ROOT / "docker-compose.yml"),
            "-f",
            str(ROOT / "docker-compose.openai-oauth.yml"),
            "config",
            "--format",
            "json",
        ],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, "Docker Compose could not render OAuth override"
    litellm = json.loads(result.stdout)["services"]["litellm"]

    assert litellm["image"] == EXPECTED_OAUTH_IMAGE
    volumes = {volume["target"]: volume for volume in litellm["volumes"]}
    assert set(volumes) == {
        "/app/config.yaml",
        "/app/litellm_guardrails",
        "/run/secrets/openai-oauth",
    }
    assert volumes["/app/config.yaml"]["source"].endswith(
        "/config/generated/litellm-config.local.yaml"
    )
    assert volumes["/app/config.yaml"]["read_only"] is True
    assert volumes["/app/litellm_guardrails"]["read_only"] is True
    assert volumes["/run/secrets/openai-oauth"].get("read_only", False) is False
    assert set(litellm["depends_on"]) == {"db", "redis", "presidio-analyzer"}
    assert "healthcheck" in litellm


def test_static_suite_covers_oauth_profile_activation():
    makefile = (ROOT / "Makefile").read_text(encoding="utf-8")

    assert "tests/test_apply_openai_oauth_profiles.py" in makefile
