#!/usr/bin/env python3
"""Fail when the Analyzer image does not satisfy its CPU-only contract."""

from __future__ import annotations

import importlib.metadata
import importlib.util
import re
import shutil
from pathlib import Path


FORBIDDEN_MODULES = ("torch", "transformers", "onnx", "onnxruntime", "safetensors")
FORBIDDEN_EXECUTABLES = ("curl", "gcc", "g++")


def _canonical_package_name(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _forbidden_packages() -> list[str]:
    forbidden: list[str] = []
    for distribution in importlib.metadata.distributions():
        raw_name = distribution.metadata.get("Name")
        if not raw_name:
            continue
        name = _canonical_package_name(raw_name)
        if name in FORBIDDEN_MODULES or name.startswith(("nvidia-", "cuda-", "triton")):
            forbidden.append(name)
    return sorted(set(forbidden))


def verify_cpu_runtime() -> None:
    for name in FORBIDDEN_MODULES:
        if importlib.util.find_spec(name) is not None:
            raise RuntimeError("Analyzer contains forbidden execution module: " + name)
    if any(Path("/opt/models").rglob("*.safetensors")) or any(Path("/opt/models").rglob("*.onnx")):
        raise RuntimeError("Analyzer contains model execution weights")

    forbidden_packages = _forbidden_packages()
    if forbidden_packages:
        raise RuntimeError(
            "CPU image contains forbidden accelerator packages: "
            + ", ".join(forbidden_packages)
        )

    forbidden_executables = [
        executable
        for executable in FORBIDDEN_EXECUTABLES
        if shutil.which(executable) is not None
    ]
    if forbidden_executables:
        raise RuntimeError(
            "CPU image contains build or healthcheck tools: "
            + ", ".join(forbidden_executables)
        )

    print(
        "CPU Analyzer verified: local execution libraries/weights=absent, "
        "CUDA packages=absent, build tools=absent",
        flush=True,
    )


if __name__ == "__main__":
    verify_cpu_runtime()
