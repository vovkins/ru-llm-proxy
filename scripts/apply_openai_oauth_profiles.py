#!/usr/bin/env python3
"""Safely apply local OpenAI OAuth profiles by recreating only LiteLLM."""

from __future__ import annotations

import argparse
import os
import stat
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, Sequence

import yaml

if __package__:
    from . import generate_openai_oauth_config as config_generator
    from . import validate_openai_oauth_setup as setup_validator
else:
    import generate_openai_oauth_config as config_generator
    import validate_openai_oauth_setup as setup_validator


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_BASE_CONFIG = ROOT / "litellm-config.yaml"
DEFAULT_PROFILES_PATH = ROOT / "config" / "openai-oauth" / "profiles.local.yaml"
DEFAULT_GENERATED_CONFIG = ROOT / "config" / "generated" / "litellm-config.local.yaml"
DEFAULT_SECRETS_ROOT = ROOT / "secrets" / "openai-oauth"
DEFAULT_COMPOSE_FILE = ROOT / "docker-compose.yml"
DEFAULT_OAUTH_COMPOSE_FILE = ROOT / "docker-compose.openai-oauth.yml"
MAX_CONFIG_BYTES = 2 * 1024 * 1024
OAUTH_SECRET_TARGET = "/run/secrets/openai-oauth"


class ApplyProfilesError(Exception):
    """An activation error safe to show to an operator."""


class _CommandFailed(ApplyProfilesError):
    pass


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: str = ""


class CommandRunner(Protocol):
    def __call__(
        self,
        command: Sequence[str],
        *,
        capture_output: bool = False,
    ) -> CommandResult: ...


@dataclass(frozen=True)
class ApplySummary:
    profiles: int
    oauth_deployments: int
    previous_mode: str


@dataclass(frozen=True)
class _ConfigSnapshot:
    existed: bool
    content: bytes | None


@dataclass(frozen=True)
class _PreviousContainer:
    mode: str
    image: str | None


def _subprocess_runner(
    command: Sequence[str],
    *,
    capture_output: bool = False,
) -> CommandResult:
    try:
        result = subprocess.run(
            list(command),
            cwd=ROOT,
            check=False,
            capture_output=capture_output,
            text=True,
        )
    except OSError as exc:
        raise ApplyProfilesError("cannot execute Docker Compose") from exc
    return CommandResult(
        returncode=result.returncode,
        stdout=result.stdout if capture_output else "",
    )


def _compose_command(
    compose_files: Sequence[Path],
    *arguments: str,
) -> list[str]:
    command = ["docker", "compose"]
    for compose_file in compose_files:
        command.extend(("-f", str(compose_file)))
    command.extend(arguments)
    return command


def _run_checked(
    runner: CommandRunner,
    command: Sequence[str],
    error_message: str,
    *,
    capture_output: bool = False,
) -> CommandResult:
    result = runner(command, capture_output=capture_output)
    if result.returncode != 0:
        raise _CommandFailed(error_message)
    return result


def _require_compose_file(path: Path, label: str) -> None:
    try:
        path_stat = path.lstat()
    except FileNotFoundError as exc:
        raise ApplyProfilesError(f"{label} does not exist") from exc
    except OSError as exc:
        raise ApplyProfilesError(f"cannot inspect {label}") from exc
    if stat.S_ISLNK(path_stat.st_mode) or not stat.S_ISREG(path_stat.st_mode):
        raise ApplyProfilesError(f"{label} must be a regular file")


def _prepare_output_parent(path: Path) -> None:
    parent = path.parent
    try:
        parent.mkdir(mode=0o755, parents=True, exist_ok=True)
        parent_stat = parent.lstat()
    except OSError as exc:
        raise ApplyProfilesError("cannot prepare generated config directory") from exc
    if stat.S_ISLNK(parent_stat.st_mode) or not stat.S_ISDIR(parent_stat.st_mode):
        raise ApplyProfilesError("generated config parent must be a regular directory")


def _read_config_snapshot(path: Path) -> _ConfigSnapshot:
    try:
        expected_stat = path.lstat()
    except FileNotFoundError:
        return _ConfigSnapshot(existed=False, content=None)
    except OSError as exc:
        raise ApplyProfilesError("cannot inspect previous generated config") from exc

    if stat.S_ISLNK(expected_stat.st_mode) or not stat.S_ISREG(expected_stat.st_mode):
        raise ApplyProfilesError("previous generated config must be a regular file")
    if expected_stat.st_nlink != 1:
        raise ApplyProfilesError("previous generated config must not have hard links")
    if expected_stat.st_size <= 0:
        raise ApplyProfilesError("previous generated config is empty")
    if expected_stat.st_size > MAX_CONFIG_BYTES:
        raise ApplyProfilesError("previous generated config exceeds the size limit")
    if stat.S_IMODE(expected_stat.st_mode) & 0o022:
        raise ApplyProfilesError(
            "previous generated config must not be group- or world-writable"
        )

    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        file_fd = os.open(path, flags)
    except OSError as exc:
        raise ApplyProfilesError(
            "cannot open previous generated config safely"
        ) from exc
    try:
        opened_stat = os.fstat(file_fd)
        if (opened_stat.st_dev, opened_stat.st_ino) != (
            expected_stat.st_dev,
            expected_stat.st_ino,
        ):
            raise ApplyProfilesError("previous generated config changed while reading")
        with os.fdopen(file_fd, "rb") as handle:
            file_fd = -1
            content = handle.read(MAX_CONFIG_BYTES + 1)
    except OSError as exc:
        raise ApplyProfilesError("cannot read previous generated config") from exc
    finally:
        if file_fd >= 0:
            os.close(file_fd)
    if len(content) > MAX_CONFIG_BYTES:
        raise ApplyProfilesError("previous generated config exceeds the size limit")
    return _ConfigSnapshot(existed=True, content=content)


def _fsync_directory(path: Path) -> None:
    try:
        directory_fd = os.open(path, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except OSError as exc:
        raise ApplyProfilesError(
            "cannot synchronize generated config directory"
        ) from exc


def _write_config_atomic(path: Path, content: bytes) -> None:
    temp_path: Path | None = None
    try:
        temp_fd, temp_name = tempfile.mkstemp(
            prefix=f".{path.name}.restore.",
            dir=path.parent,
        )
        temp_path = Path(temp_name)
        with os.fdopen(temp_fd, "wb") as handle:
            os.fchmod(handle.fileno(), 0o644)
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
        temp_path = None
        path.chmod(0o644)
        _fsync_directory(path.parent)
    except ApplyProfilesError:
        raise
    except OSError as exc:
        raise ApplyProfilesError("cannot restore previous generated config") from exc
    finally:
        if temp_path is not None:
            try:
                temp_path.unlink(missing_ok=True)
            except OSError:
                pass


def _activate_candidate(candidate: Path, generated_path: Path) -> None:
    try:
        os.replace(candidate, generated_path)
        generated_path.chmod(0o644)
        _fsync_directory(generated_path.parent)
    except ApplyProfilesError:
        raise
    except OSError as exc:
        raise ApplyProfilesError("cannot activate generated config") from exc


def _restore_snapshot(path: Path, snapshot: _ConfigSnapshot) -> None:
    if snapshot.existed:
        if snapshot.content is None:
            raise ApplyProfilesError("previous generated config snapshot is invalid")
        _write_config_atomic(path, snapshot.content)
        return
    try:
        path.unlink(missing_ok=True)
        _fsync_directory(path.parent)
    except ApplyProfilesError:
        raise
    except OSError as exc:
        raise ApplyProfilesError("cannot remove failed generated config") from exc


def _detect_previous_container(
    runner: CommandRunner,
    base_compose_file: Path,
) -> _PreviousContainer:
    compose_files = (base_compose_file,)
    result = _run_checked(
        runner,
        _compose_command(compose_files, "ps", "--all", "--quiet", "litellm"),
        "cannot inspect the current LiteLLM container",
        capture_output=True,
    )
    container_ids = [
        line.strip() for line in result.stdout.splitlines() if line.strip()
    ]
    if not container_ids:
        return _PreviousContainer(mode="absent", image=None)
    if len(container_ids) != 1:
        raise ApplyProfilesError("expected exactly one existing LiteLLM container")

    container_id = container_ids[0]
    inspect_result = _run_checked(
        runner,
        ["docker", "inspect", "--format", "{{.Config.Image}}", container_id],
        "cannot inspect the current LiteLLM image",
        capture_output=True,
    )
    current_image = inspect_result.stdout.strip()
    if (
        not current_image
        or current_image != current_image.strip()
        or any(character.isspace() for character in current_image)
        or len(current_image) > 512
    ):
        raise ApplyProfilesError("current LiteLLM image is unknown")

    mounts_result = _run_checked(
        runner,
        [
            "docker",
            "inspect",
            "--format",
            "{{range .Mounts}}{{println .Destination}}{{end}}",
            container_id,
        ],
        "cannot inspect the current LiteLLM mounts",
        capture_output=True,
    )
    mount_targets = {
        line.strip() for line in mounts_result.stdout.splitlines() if line.strip()
    }
    mode = "oauth" if OAUTH_SECRET_TARGET in mount_targets else "base"
    return _PreviousContainer(mode=mode, image=current_image)


def _write_image_override(path: Path, image: str) -> None:
    rendered = yaml.safe_dump(
        {"services": {"litellm": {"image": image}}},
        sort_keys=False,
    ).encode("utf-8")
    _write_config_atomic(path, rendered)


def _up_command(
    compose_files: Sequence[Path],
    wait_timeout: int,
) -> list[str]:
    return _compose_command(
        compose_files,
        "up",
        "-d",
        "--force-recreate",
        "--no-deps",
        "--pull",
        "never",
        "--wait",
        "--wait-timeout",
        str(wait_timeout),
        "litellm",
    )


def _rollback_container(
    runner: CommandRunner,
    *,
    previous: _PreviousContainer,
    base_compose_file: Path,
    oauth_compose_file: Path,
    image_override_file: Path | None,
    wait_timeout: int,
) -> None:
    if previous.mode == "oauth":
        if image_override_file is None:
            raise ApplyProfilesError("previous OAuth image override is unavailable")
        command = _up_command(
            (base_compose_file, oauth_compose_file, image_override_file),
            wait_timeout,
        )
    elif previous.mode == "base":
        if image_override_file is None:
            raise ApplyProfilesError("previous base image override is unavailable")
        command = _up_command(
            (base_compose_file, image_override_file),
            wait_timeout,
        )
    elif previous.mode == "absent":
        command = _compose_command(
            (base_compose_file, oauth_compose_file),
            "rm",
            "--stop",
            "--force",
            "litellm",
        )
    else:
        raise ApplyProfilesError("previous LiteLLM mode is invalid")
    _run_checked(runner, command, "failed to restore the previous LiteLLM container")


def apply_profiles(
    *,
    base_config: Path = DEFAULT_BASE_CONFIG,
    profiles_path: Path = DEFAULT_PROFILES_PATH,
    generated_path: Path = DEFAULT_GENERATED_CONFIG,
    secrets_root: Path = DEFAULT_SECRETS_ROOT,
    base_compose_file: Path = DEFAULT_COMPOSE_FILE,
    oauth_compose_file: Path = DEFAULT_OAUTH_COMPOSE_FILE,
    wait_timeout: int = 180,
    runner: CommandRunner = _subprocess_runner,
) -> ApplySummary:
    """Validate and atomically apply OAuth profiles, rolling back on failure."""

    if wait_timeout <= 0:
        raise ApplyProfilesError("wait timeout must be a positive integer")
    _require_compose_file(base_compose_file, "base Compose file")
    _require_compose_file(oauth_compose_file, "OAuth Compose file")
    _prepare_output_parent(generated_path)

    compose_files = (base_compose_file, oauth_compose_file)
    with tempfile.TemporaryDirectory(
        prefix=".openai-oauth-apply.",
        dir=generated_path.parent,
    ) as temp_root:
        candidate = Path(temp_root) / generated_path.name
        try:
            config_generator.generate_config(
                base_path=base_config,
                profiles_path=profiles_path,
                output_path=candidate,
            )
            validation = setup_validator.validate_setup(
                base_path=base_config,
                profiles_path=profiles_path,
                generated_path=candidate,
                secrets_root=secrets_root,
            )
        except config_generator.ConfigGenerationError as exc:
            raise ApplyProfilesError(str(exc)) from exc
        except setup_validator.SetupValidationError as exc:
            raise ApplyProfilesError(str(exc)) from exc

        _run_checked(
            runner,
            _compose_command(compose_files, "config", "--quiet"),
            "OAuth Compose configuration is invalid",
        )
        _run_checked(
            runner,
            _compose_command(compose_files, "pull", "--quiet", "litellm"),
            "cannot pull the pinned OAuth LiteLLM image",
        )
        previous = _detect_previous_container(runner, base_compose_file)
        snapshot = _read_config_snapshot(generated_path)
        if previous.mode == "oauth" and not snapshot.existed:
            raise ApplyProfilesError(
                "cannot update OAuth mode without the previous generated config"
            )
        image_override_file: Path | None = None
        if previous.image is not None:
            image_override_file = Path(temp_root) / "previous-image.compose.yml"
            _write_image_override(image_override_file, previous.image)

        _activate_candidate(candidate, generated_path)
        try:
            _run_checked(
                runner,
                _up_command(compose_files, wait_timeout),
                "new OAuth LiteLLM container did not become healthy",
            )
        except ApplyProfilesError as activation_error:
            try:
                _restore_snapshot(generated_path, snapshot)
                _rollback_container(
                    runner,
                    previous=previous,
                    base_compose_file=base_compose_file,
                    oauth_compose_file=oauth_compose_file,
                    image_override_file=image_override_file,
                    wait_timeout=wait_timeout,
                )
            except ApplyProfilesError as rollback_error:
                raise ApplyProfilesError(
                    "OAuth activation failed and automatic rollback also failed"
                ) from rollback_error
            raise ApplyProfilesError(
                "OAuth activation failed; the previous LiteLLM state was restored"
            ) from activation_error

    return ApplySummary(
        profiles=validation.profiles,
        oauth_deployments=validation.oauth_deployments,
        previous_mode=previous.mode,
    )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Validate and apply OpenAI OAuth profiles to LiteLLM.",
    )
    parser.add_argument(
        "--wait-timeout",
        type=int,
        default=180,
        help="Seconds to wait for the recreated LiteLLM healthcheck",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        summary = apply_profiles(wait_timeout=args.wait_timeout)
    except ApplyProfilesError as exc:
        print(f"OAuth profile activation failed: {exc}", file=sys.stderr)
        return 1

    print(
        "OAuth profiles applied: "
        f"{summary.profiles} profiles and "
        f"{summary.oauth_deployments} deployments; only LiteLLM was recreated"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
