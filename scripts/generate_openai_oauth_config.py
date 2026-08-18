#!/usr/bin/env python3
"""Generate a local LiteLLM config with one deployment per OAuth profile."""

from __future__ import annotations

import argparse
import copy
import hashlib
import os
import re
import stat
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Sequence

import yaml


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_BASE_CONFIG = ROOT / "litellm-config.yaml"
DEFAULT_PROFILES_PATH = ROOT / "config" / "openai-oauth" / "profiles.local.yaml"
DEFAULT_OUTPUT_PATH = ROOT / "config" / "generated" / "litellm-config.local.yaml"
CONTAINER_AUTH_ROOT = PurePosixPath("/run/secrets/openai-oauth")
PROFILE_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
PUBLIC_MODEL_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
PROVIDER_MODEL_PATTERN = re.compile(r"^chatgpt/[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
MAX_BASE_CONFIG_BYTES = 2 * 1024 * 1024
MAX_PROFILE_CONFIG_BYTES = 128 * 1024


class ConfigGenerationError(Exception):
    """A validation or filesystem error safe to show to an operator."""


@dataclass(frozen=True)
class GenerationSummary:
    """Non-sensitive facts about a completed generation."""

    output_path: Path
    base_deployments: int
    oauth_deployments: int


class _StrictSafeLoader(yaml.SafeLoader):
    """Safe YAML loader that rejects duplicate mapping keys."""


class _NoAliasSafeDumper(yaml.SafeDumper):
    def ignore_aliases(self, data: Any) -> bool:
        return True


def _construct_unique_mapping(
    loader: _StrictSafeLoader,
    node: yaml.nodes.MappingNode,
    deep: bool = False,
) -> dict[Any, Any]:
    loader.flatten_mapping(node)
    mapping: dict[Any, Any] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        try:
            duplicate = key in mapping
        except TypeError as exc:
            raise yaml.constructor.ConstructorError(
                "while constructing a mapping",
                node.start_mark,
                "found an unhashable mapping key",
                key_node.start_mark,
            ) from exc
        if duplicate:
            raise yaml.constructor.ConstructorError(
                "while constructing a mapping",
                node.start_mark,
                f"found duplicate key {key!r}",
                key_node.start_mark,
            )
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


_StrictSafeLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
    _construct_unique_mapping,
)


def _load_yaml(path: Path, label: str, size_limit: int) -> Any:
    try:
        file_stat = path.stat()
    except FileNotFoundError as exc:
        raise ConfigGenerationError(f"{label} does not exist") from exc
    except OSError as exc:
        raise ConfigGenerationError(f"cannot inspect {label}") from exc

    if not stat.S_ISREG(file_stat.st_mode):
        raise ConfigGenerationError(f"{label} must be a regular file")
    if file_stat.st_size <= 0:
        raise ConfigGenerationError(f"{label} is empty")
    if file_stat.st_size > size_limit:
        raise ConfigGenerationError(f"{label} exceeds the size limit")

    try:
        raw = path.read_text(encoding="utf-8")
    except UnicodeError as exc:
        raise ConfigGenerationError(f"{label} is not valid UTF-8") from exc
    except OSError as exc:
        raise ConfigGenerationError(f"cannot read {label}") from exc

    try:
        return yaml.load(raw, Loader=_StrictSafeLoader)
    except yaml.YAMLError as exc:
        raise ConfigGenerationError(f"{label} is not valid strict YAML") from exc


def _validate_base_config(data: Any) -> tuple[dict[str, Any], set[str], set[str]]:
    if not isinstance(data, dict):
        raise ConfigGenerationError("base LiteLLM config must contain a mapping")

    model_list = data.get("model_list")
    if not isinstance(model_list, list):
        raise ConfigGenerationError("base LiteLLM config must contain model_list")

    model_names: set[str] = set()
    deployment_ids: set[str] = set()
    for deployment in model_list:
        if not isinstance(deployment, dict):
            raise ConfigGenerationError("base model_list entries must be mappings")
        model_name = deployment.get("model_name")
        if (
            not isinstance(model_name, str)
            or not model_name
            or model_name != model_name.strip()
        ):
            raise ConfigGenerationError("base deployment has an invalid model_name")
        if not isinstance(deployment.get("litellm_params"), dict):
            raise ConfigGenerationError("base deployment must contain litellm_params")
        model_info = deployment.get("model_info")
        if not isinstance(model_info, dict):
            raise ConfigGenerationError("base deployment must contain model_info")
        deployment_id = model_info.get("id")
        if (
            not isinstance(deployment_id, str)
            or not deployment_id
            or deployment_id != deployment_id.strip()
        ):
            raise ConfigGenerationError("base deployment has an invalid model_info.id")
        if deployment_id in deployment_ids:
            raise ConfigGenerationError("base config contains duplicate deployment ids")
        model_names.add(model_name)
        deployment_ids.add(deployment_id)

    return data, model_names, deployment_ids


def _validate_profile_config(
    data: Any,
) -> tuple[list[dict[str, str]], list[str]]:
    if not isinstance(data, dict) or set(data) != {
        "schema_version",
        "models",
        "profiles",
    }:
        raise ConfigGenerationError(
            "profile configuration must contain only schema_version, models and profiles"
        )
    if data.get("schema_version") != 1:
        raise ConfigGenerationError("profile configuration must use schema version 1")

    raw_models = data.get("models")
    if not isinstance(raw_models, list) or not raw_models:
        raise ConfigGenerationError("profile configuration must contain models")
    models: list[dict[str, str]] = []
    public_names: set[str] = set()
    provider_models: set[str] = set()
    for model in raw_models:
        if not isinstance(model, dict) or set(model) != {
            "public_name",
            "provider_model",
        }:
            raise ConfigGenerationError(
                "each OAuth model must contain only public_name and provider_model"
            )
        public_name = model.get("public_name")
        provider_model = model.get("provider_model")
        if not isinstance(public_name, str) or not PUBLIC_MODEL_PATTERN.fullmatch(
            public_name
        ):
            raise ConfigGenerationError(
                "profile configuration has an invalid public_name"
            )
        if not isinstance(provider_model, str) or not PROVIDER_MODEL_PATTERN.fullmatch(
            provider_model
        ):
            raise ConfigGenerationError(
                "profile configuration has an invalid provider_model"
            )
        if public_name in public_names:
            raise ConfigGenerationError(
                "profile configuration contains duplicate public model names"
            )
        if provider_model in provider_models:
            raise ConfigGenerationError(
                "profile configuration contains duplicate provider models"
            )
        public_names.add(public_name)
        provider_models.add(provider_model)
        models.append({"public_name": public_name, "provider_model": provider_model})

    raw_profiles = data.get("profiles")
    if not isinstance(raw_profiles, list) or not raw_profiles:
        raise ConfigGenerationError("profile configuration must contain profiles")
    enabled_profiles: list[str] = []
    profile_ids: set[str] = set()
    for profile in raw_profiles:
        if not isinstance(profile, dict) or set(profile) != {"id", "enabled"}:
            raise ConfigGenerationError(
                "each OAuth profile must contain only id and enabled"
            )
        profile_id = profile.get("id")
        enabled = profile.get("enabled")
        if not isinstance(profile_id, str) or not PROFILE_ID_PATTERN.fullmatch(
            profile_id
        ):
            raise ConfigGenerationError("profile configuration contains an invalid id")
        if not isinstance(enabled, bool):
            raise ConfigGenerationError("profile enabled must be a boolean")
        if profile_id in profile_ids:
            raise ConfigGenerationError("profile configuration contains duplicate ids")
        profile_ids.add(profile_id)
        if enabled:
            enabled_profiles.append(profile_id)

    if not enabled_profiles:
        raise ConfigGenerationError(
            "profile configuration must enable at least one profile"
        )
    return models, enabled_profiles


def _deployment_id(model: dict[str, str], profile_id: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", model["public_name"].lower()).strip("-")
    fingerprint_source = "\0".join(
        (model["public_name"], model["provider_model"], profile_id)
    )
    fingerprint = hashlib.sha256(fingerprint_source.encode("utf-8")).hexdigest()[:12]
    return f"openai-oauth-{slug[:48]}-{profile_id}-{fingerprint}"


def _build_oauth_deployments(
    models: list[dict[str, str]],
    enabled_profiles: list[str],
    reserved_ids: set[str],
) -> list[dict[str, Any]]:
    generated: list[dict[str, Any]] = []
    generated_ids: set[str] = set()
    for model in models:
        for profile_id in enabled_profiles:
            deployment_id = _deployment_id(model, profile_id)
            if deployment_id in reserved_ids or deployment_id in generated_ids:
                raise ConfigGenerationError("generated deployment id collision")
            generated_ids.add(deployment_id)
            auth_file = CONTAINER_AUTH_ROOT / profile_id / "auth.json"
            generated.append(
                {
                    "model_name": model["public_name"],
                    "litellm_params": {
                        "model": model["provider_model"],
                        "chatgpt_auth_file": str(auth_file),
                    },
                    "model_info": {
                        "id": deployment_id,
                        "base_model": model["provider_model"].removeprefix("chatgpt/"),
                        "openai_oauth_profile": profile_id,
                    },
                }
            )
    return generated


def build_generated_config(base_data: Any, profile_data: Any) -> dict[str, Any]:
    """Validate both inputs and return a complete generated LiteLLM config."""

    base_config, base_model_names, base_ids = _validate_base_config(base_data)
    models, enabled_profiles = _validate_profile_config(profile_data)
    oauth_model_names = {model["public_name"] for model in models}
    collisions = sorted(base_model_names & oauth_model_names)
    if collisions:
        raise ConfigGenerationError(
            "OAuth public model names collide with base model groups: "
            + ", ".join(collisions)
        )

    result = copy.deepcopy(base_config)
    generated = _build_oauth_deployments(models, enabled_profiles, base_ids)
    result["model_list"].extend(generated)
    return result


def _render_config(config: dict[str, Any]) -> str:
    body = yaml.dump(
        config,
        Dumper=_NoAliasSafeDumper,
        allow_unicode=True,
        default_flow_style=False,
        sort_keys=False,
    )
    return (
        "# Generated by scripts/generate_openai_oauth_config.py. "
        "Do not edit manually.\n" + body
    )


def _same_resolved_path(left: Path, right: Path) -> bool:
    try:
        return left.resolve() == right.resolve()
    except OSError as exc:
        raise ConfigGenerationError("cannot resolve configuration paths") from exc


def _fsync_directory(path: Path) -> None:
    directory_fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def _write_atomic(output_path: Path, rendered: str) -> None:
    parent = output_path.parent
    temp_path: Path | None = None
    try:
        parent.mkdir(mode=0o755, parents=True, exist_ok=True)
        if parent.is_symlink() or not parent.is_dir():
            raise ConfigGenerationError(
                "generated configuration parent must be a regular directory"
            )
        if output_path.is_symlink():
            raise ConfigGenerationError(
                "generated configuration must not be a symbolic link"
            )
        if output_path.exists() and not output_path.is_file():
            raise ConfigGenerationError(
                "generated configuration must be a regular file"
            )

        temp_fd, temp_name = tempfile.mkstemp(
            prefix=f".{output_path.name}.",
            dir=parent,
        )
        temp_path = Path(temp_name)
        with os.fdopen(temp_fd, "w", encoding="utf-8") as handle:
            os.fchmod(handle.fileno(), 0o644)
            handle.write(rendered)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, output_path)
        temp_path = None
        output_path.chmod(0o644)
        _fsync_directory(parent)
    except ConfigGenerationError:
        raise
    except OSError as exc:
        raise ConfigGenerationError("cannot write generated configuration") from exc
    finally:
        if temp_path is not None:
            try:
                temp_path.unlink(missing_ok=True)
            except OSError:
                pass


def generate_config(
    *,
    base_path: Path = DEFAULT_BASE_CONFIG,
    profiles_path: Path = DEFAULT_PROFILES_PATH,
    output_path: Path = DEFAULT_OUTPUT_PATH,
) -> GenerationSummary:
    """Generate a complete config without modifying either input file."""

    if _same_resolved_path(output_path, base_path) or _same_resolved_path(
        output_path, profiles_path
    ):
        raise ConfigGenerationError("generated configuration must not replace an input")

    base_data = _load_yaml(base_path, "base LiteLLM config", MAX_BASE_CONFIG_BYTES)
    profile_data = _load_yaml(
        profiles_path,
        "profile configuration",
        MAX_PROFILE_CONFIG_BYTES,
    )
    generated = build_generated_config(base_data, profile_data)
    rendered = _render_config(generated)
    _write_atomic(output_path, rendered)
    return GenerationSummary(
        output_path=output_path,
        base_deployments=len(base_data["model_list"]),
        oauth_deployments=len(generated["model_list"]) - len(base_data["model_list"]),
    )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Generate a LiteLLM config from base settings and OAuth profiles.",
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
        "--output",
        type=Path,
        default=DEFAULT_OUTPUT_PATH,
        help="Generated LiteLLM YAML path",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        summary = generate_config(
            base_path=args.base_config,
            profiles_path=args.profiles_file,
            output_path=args.output,
        )
    except ConfigGenerationError as exc:
        print(f"Generation failed: {exc}", file=sys.stderr)
        return 1

    print(
        f"Generated {summary.oauth_deployments} OAuth deployments in "
        f"{summary.output_path}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
