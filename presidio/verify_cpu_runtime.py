#!/usr/bin/env python3
"""Fail when the Analyzer image does not satisfy its CPU-only contract."""

from __future__ import annotations

import importlib.metadata
import re
import shutil

import torch


EXPECTED_TORCH_VERSION = "2.13.0+cpu"
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
        if name.startswith(("nvidia-", "cuda-", "triton")):
            forbidden.append(name)
    return sorted(set(forbidden))


def verify_cpu_runtime() -> None:
    actual_version = str(torch.__version__)
    if actual_version != EXPECTED_TORCH_VERSION:
        raise RuntimeError(
            f"expected torch {EXPECTED_TORCH_VERSION}, got {actual_version}"
        )
    if torch.version.cuda is not None:
        raise RuntimeError(f"CPU image contains CUDA build {torch.version.cuda}")
    if torch.cuda.is_available():
        raise RuntimeError("CPU image unexpectedly reports CUDA as available")

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
        f"CPU runtime verified: torch={actual_version}, "
        "CUDA packages=absent, build tools=absent",
        flush=True,
    )


if __name__ == "__main__":
    verify_cpu_runtime()
