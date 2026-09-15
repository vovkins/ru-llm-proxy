"""Verify the fixed CUDA PyTorch image and, at runtime, its selected GPU."""

from __future__ import annotations

import argparse

import torch


EXPECTED_TORCH_VERSION = "2.13.0+cu126"
EXPECTED_CUDA_VERSION = "12.6"
MINIMUM_COMPUTE_CAPABILITY = (7, 5)


def verify_build() -> None:
    actual_version = str(torch.__version__)
    if actual_version != EXPECTED_TORCH_VERSION:
        raise RuntimeError(
            f"expected torch {EXPECTED_TORCH_VERSION}, got {actual_version}"
        )
    if str(torch.version.cuda) != EXPECTED_CUDA_VERSION:
        raise RuntimeError(
            f"expected CUDA build {EXPECTED_CUDA_VERSION}, got {torch.version.cuda}"
        )


def verify_device() -> None:
    verify_build()
    if not torch.cuda.is_available() or torch.cuda.device_count() < 1:
        raise RuntimeError("CUDA profile requires an available NVIDIA GPU")
    capability = tuple(int(value) for value in torch.cuda.get_device_capability(0))
    if capability < MINIMUM_COMPUTE_CAPABILITY:
        raise RuntimeError(
            "CUDA profile requires compute capability 7.5 or newer"
        )
    device = torch.device("cuda:0")
    probe = torch.ones((2, 2), dtype=torch.float32, device=device)
    if float((probe @ probe).sum().cpu().item()) != 8.0:
        raise RuntimeError("CUDA probe returned an unexpected result")
    properties = torch.cuda.get_device_properties(0)
    print(
        "GPU runtime verified: "
        f"torch={torch.__version__}, cuda={torch.version.cuda}, "
        f"device={properties.name}, capability={capability[0]}.{capability[1]}, "
        f"memory_bytes={properties.total_memory}"
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--build-only",
        action="store_true",
        help="verify the CUDA wheel without requiring a GPU",
    )
    args = parser.parse_args()
    if args.build_only:
        verify_build()
        print(
            "GPU image build verified: "
            f"torch={torch.__version__}, cuda={torch.version.cuda}"
        )
        return
    verify_device()


if __name__ == "__main__":
    main()
