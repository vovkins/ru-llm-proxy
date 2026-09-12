#!/usr/bin/env python3
"""Reproducible synthetic long-context benchmark for ru-llm-proxy."""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import statistics
import subprocess
import sys
import threading
import time
import uuid
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


DEFAULT_SIZES = (1_000, 8_000, 50_000)
MAX_GENERATED_TOKENS = 1_000_000
DEFAULT_REPEATED_FIELD_TOKENS = 1_000
DEFAULT_OUTPUT = Path("/tmp/ru-llm-proxy-long-context-report.json")
SYNTHETIC_PII_TOKEN = "load-test-user@example.test"
SAFE_ENTITY_TYPE = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")
SAFE_METADATA_KEY = re.compile(r"^[a-z][a-z0-9_.-]{0,63}$")
SAFE_CONTAINER_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
SAFE_ERROR_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
SENSITIVE_METADATA_KEY = re.compile(
    r"(?:auth|credential|key|password|secret|token)",
    re.IGNORECASE,
)
DOCKER_MEMORY_UNITS = {
    "B": 1,
    "kB": 1_000,
    "MB": 1_000_000,
    "GB": 1_000_000_000,
    "TB": 1_000_000_000_000,
    "KiB": 1_024,
    "MiB": 1_048_576,
    "GiB": 1_073_741_824,
    "TiB": 1_099_511_627_776,
}


def parse_sizes(value: str) -> tuple[int, ...]:
    """Parse a unique ascending comma-separated synthetic token sequence."""
    try:
        sizes = tuple(sorted({int(item.strip()) for item in value.split(",")}))
    except ValueError as exc:
        raise argparse.ArgumentTypeError("sizes must be comma-separated integers") from exc
    if not sizes or sizes[0] < 1:
        raise argparse.ArgumentTypeError("sizes must contain positive integers")
    if sizes[-1] > MAX_GENERATED_TOKENS:
        raise argparse.ArgumentTypeError(
            f"size exceeds the safety limit of {MAX_GENERATED_TOKENS}"
        )
    return sizes


def parse_metadata(values: Iterable[str]) -> dict[str, str]:
    """Parse bounded non-secret report metadata supplied by the operator."""
    metadata: dict[str, str] = {}
    for value in values:
        key, separator, raw_value = value.partition("=")
        if not separator or not SAFE_METADATA_KEY.fullmatch(key):
            raise ValueError("metadata must use lowercase key=value entries")
        if SENSITIVE_METADATA_KEY.search(key):
            raise ValueError(f"sensitive metadata key is not allowed: {key}")
        if not raw_value or len(raw_value) > 128 or not raw_value.isprintable():
            raise ValueError(f"metadata value is invalid: {key}")
        metadata[key] = raw_value
    return metadata


def parse_container_names(values: Iterable[str]) -> tuple[str, ...]:
    """Validate and deduplicate operator-supplied Docker container names."""
    names: list[str] = []
    for value in values:
        if not SAFE_CONTAINER_NAME.fullmatch(value):
            raise ValueError(f"invalid Docker container name: {value}")
        if value not in names:
            names.append(value)
    return tuple(names)


def parse_docker_bytes(value: str) -> int:
    """Parse the memory units emitted by `docker stats`."""
    match = re.fullmatch(r"([0-9]+(?:\.[0-9]+)?)\s*([A-Za-z]+)", value.strip())
    if match is None or match.group(2) not in DOCKER_MEMORY_UNITS:
        raise ValueError("unsupported Docker memory value")
    return round(float(match.group(1)) * DOCKER_MEMORY_UNITS[match.group(2)])


def _docker_process_rss_bytes(container_name: str) -> int:
    """Return summed process RSS from a container without requiring `ps`."""
    command = (
        "awk '/VmRSS:/ {sum += $2} "
        "END {printf \"%.0f\\n\", sum * 1024}' /proc/[0-9]*/status"
    )
    completed = subprocess.run(
        ["docker", "exec", container_name, "sh", "-c", command],
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    )
    return int(completed.stdout.strip())


def docker_resource_snapshot(container_names: tuple[str, ...]) -> list[dict[str, Any]]:
    """Read one bounded CPU, working-set memory, and process RSS snapshot."""
    if not container_names:
        return []
    completed = subprocess.run(
        [
            "docker",
            "stats",
            "--no-stream",
            "--format",
            "{{json .}}",
            *container_names,
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=15,
    )
    snapshots: list[dict[str, Any]] = []
    for line in completed.stdout.splitlines():
        raw = json.loads(line)
        name = str(raw.get("Name") or raw.get("Container") or "")
        if name not in container_names:
            continue
        cpu_value = str(raw.get("CPUPerc") or "").strip().removesuffix("%")
        memory_value = str(raw.get("MemUsage") or "").partition("/")[0].strip()
        snapshots.append(
            {
                "container": name,
                "cpu_percent": float(cpu_value),
                "memory_working_set_bytes": parse_docker_bytes(memory_value),
                "process_rss_bytes": _docker_process_rss_bytes(name),
            }
        )
    return snapshots


class DockerResourceSampler:
    """Collect per-container resource peaks while one measurement runs."""

    def __init__(self, container_names: tuple[str, ...], interval_seconds: float):
        self.container_names = container_names
        self.interval_seconds = interval_seconds
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._started_at: float | None = None
        self._samples: dict[str, list[dict[str, Any]]] = {
            name: [] for name in container_names
        }
        self._errors: Counter[str] = Counter()

    def __enter__(self) -> "DockerResourceSampler":
        if self.container_names:
            self._started_at = time.perf_counter()
            self._thread = threading.Thread(target=self._run, daemon=True)
            self._thread.start()
        return self

    def __exit__(self, _exc_type, _exc, _traceback) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=20)
            if self._thread.is_alive():
                self._errors["sampler_stop_timeout"] += 1

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                snapshots = docker_resource_snapshot(self.container_names)
            except Exception as exc:
                self._errors[type(exc).__name__] += 1
            else:
                sampled_at_seconds = (
                    max(0.0, time.perf_counter() - self._started_at)
                    if self._started_at is not None
                    else 0.0
                )
                for snapshot in snapshots:
                    snapshot["sampled_at_seconds"] = sampled_at_seconds
                    self._samples[snapshot["container"]].append(snapshot)
            self._stop.wait(self.interval_seconds)

    def report(self) -> dict[str, Any]:
        containers: dict[str, dict[str, Any]] = {}
        for name, samples in self._samples.items():
            peak_cpu = (
                max(samples, key=lambda sample: sample["cpu_percent"])
                if samples
                else None
            )
            peak_memory = (
                max(
                    samples,
                    key=lambda sample: sample["memory_working_set_bytes"],
                )
                if samples
                else None
            )
            peak_rss = (
                max(samples, key=lambda sample: sample["process_rss_bytes"])
                if samples
                else None
            )
            containers[name] = {
                "sample_count": len(samples),
                "peak_cpu_percent": (
                    round(peak_cpu["cpu_percent"], 3)
                    if peak_cpu is not None
                    else None
                ),
                "peak_cpu_at_seconds": (
                    round(peak_cpu["sampled_at_seconds"], 3)
                    if peak_cpu is not None
                    else None
                ),
                "peak_memory_working_set_bytes": (
                    peak_memory["memory_working_set_bytes"]
                    if peak_memory is not None
                    else None
                ),
                "peak_memory_working_set_at_seconds": (
                    round(peak_memory["sampled_at_seconds"], 3)
                    if peak_memory is not None
                    else None
                ),
                "peak_process_rss_bytes": (
                    peak_rss["process_rss_bytes"]
                    if peak_rss is not None
                    else None
                ),
                "peak_process_rss_at_seconds": (
                    round(peak_rss["sampled_at_seconds"], 3)
                    if peak_rss is not None
                    else None
                ),
            }
        return {
            "containers": containers,
            "sampling_errors": dict(sorted(self._errors.items())),
        }


def synthetic_segment(
    token_count: int,
    *,
    seed: int,
    include_canaries: bool,
    window_stride: int,
) -> tuple[str, int]:
    """Build exactly token_count whitespace units and return canary count."""
    if token_count < 1:
        return "", 0
    neutral_token = ("текст", "данные", "пример")[seed % 3]
    tokens = [neutral_token] * token_count
    positions: set[int] = set()
    if include_canaries:
        positions.update((0, token_count // 2, token_count - 1))
        if token_count > 2:
            positions.add(min(token_count - 2, max(1, window_stride)))
    for position in positions:
        tokens[position] = SYNTHETIC_PII_TOKEN
    for position in range(token_count):
        if position not in positions:
            tokens[position] = f"{neutral_token}фрагмент{_alphabetic_seed(seed)}"
            break
    return " ".join(tokens), len(positions)


def _alphabetic_seed(seed: int) -> str:
    """Encode a non-negative seed without digits that may resemble identifiers."""
    if seed < 0:
        raise ValueError("seed must be non-negative")
    letters: list[str] = []
    value = seed
    while True:
        value, remainder = divmod(value, 26)
        letters.append(chr(ord("а") + remainder))
        if value == 0:
            return "".join(reversed(letters))
        value -= 1


def build_payloads(
    sizes: tuple[int, ...],
    *,
    profile: str,
    api: str,
    include_canaries: bool,
    window_stride: int,
    repeated_field_tokens: int = DEFAULT_REPEATED_FIELD_TOKENS,
) -> list[dict[str, Any]]:
    """Build bounded synthetic request payloads for the selected profile."""
    payloads: list[dict[str, Any]] = []
    if profile == "one-shot":
        for index, size in enumerate(sizes):
            segment, canary_count = synthetic_segment(
                size,
                seed=index,
                include_canaries=include_canaries,
                window_stride=window_stride,
            )
            request_value: Any
            if api == "chat":
                request_value = [{"role": "user", "content": segment}]
            else:
                request_value = segment
            payloads.append(
                {
                    "requested_token_count": size,
                    "input_character_count": len(segment),
                    "expected_canary_count": canary_count,
                    "request_value": request_value,
                }
            )
        return payloads

    if profile == "repeated-field":
        for index, size in enumerate(sizes):
            if size % repeated_field_tokens:
                raise ValueError(
                    "repeated-field sizes must be divisible by repeated-field-tokens"
                )
            segment, canary_count = synthetic_segment(
                repeated_field_tokens,
                seed=index,
                include_canaries=include_canaries,
                window_stride=window_stride,
            )
            field_count = size // repeated_field_tokens
            request_value = [
                {"role": "user", "content": segment} for _ in range(field_count)
            ]
            payloads.append(
                {
                    "requested_token_count": size,
                    "input_character_count": (
                        len(segment) * field_count + max(0, field_count - 1)
                    ),
                    "expected_canary_count": canary_count * field_count,
                    "request_field_count": field_count,
                    "request_value": request_value,
                }
            )
        return payloads

    history: list[dict[str, str]] = []
    previous_size = 0
    cumulative_characters = 0
    cumulative_canaries = 0
    for index, size in enumerate(sizes):
        segment_size = size - previous_size
        segment, canary_count = synthetic_segment(
            segment_size,
            seed=index,
            include_canaries=include_canaries,
            window_stride=window_stride,
        )
        if history:
            cumulative_characters += 1
        history.append({"role": "user", "content": segment})
        cumulative_characters += len(segment)
        cumulative_canaries += canary_count
        payloads.append(
            {
                "requested_token_count": size,
                "input_character_count": cumulative_characters,
                "expected_canary_count": cumulative_canaries,
                "request_value": [dict(item) for item in history],
            }
        )
        previous_size = size
    return payloads


def percentile(values: list[float], quantile: float) -> float | None:
    """Return a nearest-rank percentile for a small benchmark sample."""
    if not values:
        return None
    ordered = sorted(values)
    index = max(0, math.ceil(quantile * len(ordered)) - 1)
    return round(ordered[index], 6)


def _summarize_measurements(results: list[dict[str, Any]]) -> dict[str, Any]:
    """Summarize one homogeneous group without response data."""
    durations = [
        float(result["total_seconds"])
        for result in results
        if result.get("status") == "success"
    ]
    ttft_values = [
        float(result["ttft_seconds"])
        for result in results
        if result.get("ttft_seconds") is not None
    ]
    status_counts = Counter(str(result.get("status") or "unknown") for result in results)
    return {
        "measurement_count": len(results),
        "success_count": sum(result.get("status") == "success" for result in results),
        "failure_count": sum(
            result.get("status") in {"http_error", "client_error"}
            for result in results
        ),
        "generated_count": sum(
            result.get("status") == "generated" for result in results
        ),
        "status_counts": dict(sorted(status_counts.items())),
        "latency_seconds": {
            "mean": round(statistics.fmean(durations), 6) if durations else None,
            "p50": percentile(durations, 0.50),
            "p95": percentile(durations, 0.95),
            "p99": percentile(durations, 0.99),
        },
        "ttft_seconds": {
            "p50": percentile(ttft_values, 0.50),
            "p95": percentile(ttft_values, 0.95),
            "p99": percentile(ttft_values, 0.99),
        },
    }


def summarize(results: list[dict[str, Any]]) -> dict[str, Any]:
    """Summarize measurements per requested size without mixing workloads."""
    grouped: dict[int, list[dict[str, Any]]] = {}
    for result in results:
        requested_size = int(result.get("requested_token_count") or 0)
        grouped.setdefault(requested_size, []).append(result)
    totals = _summarize_measurements(results)
    totals.pop("latency_seconds")
    totals.pop("ttft_seconds")
    totals["by_requested_token_count"] = {
        str(size): _summarize_measurements(grouped[size])
        for size in sorted(grouped)
    }
    return totals


def _safe_entity_counts(payload: object) -> dict[str, int]:
    if not isinstance(payload, dict) or not isinstance(payload.get("entities"), list):
        return {}
    counts: Counter[str] = Counter()
    for entity in payload["entities"]:
        if not isinstance(entity, dict):
            continue
        entity_type = entity.get("entity_type")
        if isinstance(entity_type, str) and SAFE_ENTITY_TYPE.fullmatch(entity_type):
            counts[entity_type] += 1
    return dict(sorted(counts.items()))


def _safe_error_fields_from_payload(payload: object) -> dict[str, str]:
    """Extract bounded identifiers from an error payload without its message."""
    if not isinstance(payload, dict):
        return {}

    fields: dict[str, str] = {}
    for section_name in ("error", "detail"):
        section = payload.get(section_name)
        if not isinstance(section, dict):
            continue
        for source_key, report_key in (("type", "error_type"), ("code", "error_code")):
            value = section.get(source_key)
            if isinstance(value, (str, int)):
                normalized = str(value)
                if (
                    normalized.lower() not in {"none", "null", "unknown"}
                    and SAFE_ERROR_IDENTIFIER.fullmatch(normalized)
                ):
                    fields.setdefault(report_key, normalized)
    return fields


def _safe_http_error_fields(response: Any) -> dict[str, str]:
    """Extract bounded error identifiers without persisting provider messages."""
    if response.status_code < 400:
        return {}
    try:
        payload = response.json()
    except Exception:
        return {}
    return _safe_error_fields_from_payload(payload)


def _analyzer_text(request_value: Any) -> str:
    """Flatten generated history for the direct Analyzer layer."""
    if isinstance(request_value, str):
        return request_value
    if isinstance(request_value, list):
        return "\n".join(
            item["content"]
            for item in request_value
            if isinstance(item, dict) and isinstance(item.get("content"), str)
        )
    raise ValueError("unsupported generated Analyzer payload")


def _analyzer_measurement(client, url: str, text: str) -> dict[str, Any]:
    started_at = time.perf_counter()
    try:
        response = client.post(
            f"{url.rstrip('/')}/api/v1/analyze",
            json={"text": text, "language": "ru", "score_threshold": 0.35},
        )
        duration = time.perf_counter() - started_at
        entity_counts = _safe_entity_counts(
            response.json() if response.status_code == 200 else None
        )
        result = {
            "status": "success" if response.status_code == 200 else "http_error",
            "http_status": response.status_code,
            "total_seconds": round(duration, 6),
            "ttft_seconds": None,
            "response_byte_count": len(response.content),
            "entity_counts": entity_counts,
            "detected_entity_count": sum(entity_counts.values()),
        }
        result.update(_safe_http_error_fields(response))
        return result
    except Exception as exc:
        return {
            "status": "client_error",
            "failure_type": type(exc).__name__,
            "total_seconds": round(time.perf_counter() - started_at, 6),
            "ttft_seconds": None,
        }


def _is_output_delta(line: str) -> bool:
    """Return whether an SSE data line contains a non-empty model text delta."""
    if not line.startswith("data:"):
        return False
    raw_data = line[5:].strip()
    if not raw_data or raw_data == "[DONE]":
        return False
    try:
        payload = json.loads(raw_data)
    except json.JSONDecodeError:
        return False
    if not isinstance(payload, dict):
        return False
    if payload.get("type") == "response.output_text.delta":
        return bool(payload.get("delta"))
    choices = payload.get("choices")
    if not isinstance(choices, list):
        return False
    return any(
        isinstance(choice, dict)
        and isinstance(choice.get("delta"), dict)
        and bool(choice["delta"].get("content"))
        for choice in choices
    )


def _proxy_measurement(
    client,
    *,
    url: str,
    api: str,
    model: str,
    api_key: str,
    request_value: Any,
    stream: bool,
) -> dict[str, Any]:
    endpoint = "chat/completions" if api == "chat" else "responses"
    body = {"model": model, "stream": stream}
    body["messages" if api == "chat" else "input"] = request_value
    headers = {"Authorization": f"Bearer {api_key}"}
    started_at = time.perf_counter()
    try:
        if not stream:
            response = client.post(
                f"{url.rstrip('/')}/v1/{endpoint}",
                headers=headers,
                json=body,
            )
            duration = time.perf_counter() - started_at
            result = {
                "status": "success" if response.status_code < 400 else "http_error",
                "http_status": response.status_code,
                "total_seconds": round(duration, 6),
                "ttft_seconds": None,
                "response_byte_count": len(response.content),
            }
            result.update(_safe_http_error_fields(response))
            return result

        first_event_at: float | None = None
        response_bytes = 0
        stream_error_fields: dict[str, str] = {}
        with client.stream(
            "POST",
            f"{url.rstrip('/')}/v1/{endpoint}",
            headers=headers,
            json=body,
        ) as response:
            for line in response.iter_lines():
                response_bytes += len(line.encode("utf-8"))
                if first_event_at is None and _is_output_delta(line):
                    first_event_at = time.perf_counter()
                if response.status_code >= 400 and not stream_error_fields:
                    try:
                        stream_error_fields = _safe_error_fields_from_payload(
                            json.loads(line)
                        )
                    except json.JSONDecodeError:
                        pass
            duration = time.perf_counter() - started_at
            result = {
                "status": "success" if response.status_code < 400 else "http_error",
                "http_status": response.status_code,
                "total_seconds": round(duration, 6),
                "ttft_seconds": (
                    round(first_event_at - started_at, 6)
                    if first_event_at is not None
                    else None
                ),
                "response_byte_count": response_bytes,
            }
            result.update(stream_error_fields)
            return result
    except Exception as exc:
        return {
            "status": "client_error",
            "failure_type": type(exc).__name__,
            "total_seconds": round(time.perf_counter() - started_at, 6),
            "ttft_seconds": None,
        }


def _discard_request_value(payload: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in payload.items() if key != "request_value"}


def build_report(
    args: argparse.Namespace,
    measurements: list[dict[str, Any]],
    metadata: dict[str, str],
) -> dict[str, Any]:
    """Build the stable content-free report document."""
    report = {
        "schema_version": 1,
        "run_id": str(uuid.uuid4()),
        "created_at": datetime.now(timezone.utc).isoformat(),
        "parameters": {
            "layer": args.layer,
            "profile": args.profile,
            "api": args.api,
            "model": args.model if args.layer != "analyzer" else None,
            "stream": args.stream,
            "sizes": list(args.sizes),
            "repetitions": args.repetitions,
            "warmup_runs": args.warmup_runs,
            "timeout_seconds": args.timeout,
            "canaries_enabled": not args.no_canaries,
            "generator_unit": "whitespace_token",
            "window_stride_hint": args.window_stride,
            "repeated_field_tokens": (
                args.repeated_field_tokens
                if args.profile == "repeated-field"
                else None
            ),
            "run_label": args.run_label,
            "resource_containers": list(args.docker_containers),
            "resource_sample_interval_seconds": args.resource_sample_interval,
        },
        "environment": metadata,
        "measurements": measurements,
        "summary": summarize(measurements),
    }
    serialized = json.dumps(report, ensure_ascii=False)
    if SYNTHETIC_PII_TOKEN in serialized:
        raise RuntimeError("benchmark report contains generated request data")
    return report


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--layer",
        choices=("analyzer", "proxy-mock", "codex-lb"),
        required=True,
    )
    parser.add_argument("--url", required=True, help="Base URL of the selected layer")
    parser.add_argument(
        "--profile",
        choices=("one-shot", "growing-history", "repeated-field"),
        required=True,
    )
    parser.add_argument("--api", choices=("responses", "chat"), default="responses")
    parser.add_argument("--model", default="")
    parser.add_argument("--api-key-env", default="LITELLM_MASTER_KEY")
    parser.add_argument("--sizes", type=parse_sizes, default=DEFAULT_SIZES)
    parser.add_argument("--repetitions", type=int, default=1)
    parser.add_argument("--warmup-runs", type=int, default=0)
    parser.add_argument("--timeout", type=float, default=180.0)
    parser.add_argument("--stream", action="store_true")
    parser.add_argument("--no-canaries", action="store_true")
    parser.add_argument("--window-stride", type=int, default=320)
    parser.add_argument(
        "--repeated-field-tokens",
        type=int,
        default=DEFAULT_REPEATED_FIELD_TOKENS,
    )
    parser.add_argument("--allow-real-provider", action="store_true")
    parser.add_argument("--run-label", choices=("cold", "warm", "custom"), default="custom")
    parser.add_argument("--metadata", action="append", default=[])
    parser.add_argument(
        "--docker-container",
        action="append",
        default=[],
        dest="docker_containers",
        help="Container to sample with docker stats; repeat for multiple containers",
    )
    parser.add_argument("--resource-sample-interval", type=float, default=0.25)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--dry-run", action="store_true")
    return parser


def validate_args(args: argparse.Namespace) -> None:
    if args.repetitions < 1 or args.warmup_runs < 0:
        raise ValueError("repetitions must be positive and warmup-runs non-negative")
    if (
        args.timeout <= 0
        or args.window_stride < 1
        or args.repeated_field_tokens < 1
    ):
        raise ValueError(
            "timeout, window-stride and repeated-field-tokens must be positive"
        )
    if args.resource_sample_interval <= 0:
        raise ValueError("resource-sample-interval must be positive")
    args.docker_containers = parse_container_names(args.docker_containers)
    if args.layer != "analyzer" and not args.model:
        raise ValueError("--model is required for proxy benchmark layers")
    if args.layer == "codex-lb" and not args.allow_real_provider:
        raise ValueError("codex-lb benchmark requires --allow-real-provider")
    if args.layer == "analyzer" and args.stream:
        raise ValueError("Analyzer does not support streaming responses")


def run(args: argparse.Namespace) -> dict[str, Any]:
    validate_args(args)
    metadata = parse_metadata(args.metadata)
    payloads = build_payloads(
        args.sizes,
        profile=args.profile,
        api=args.api,
        include_canaries=not args.no_canaries,
        window_stride=args.window_stride,
        repeated_field_tokens=args.repeated_field_tokens,
    )
    measurements: list[dict[str, Any]] = []

    if args.dry_run:
        for payload in payloads:
            measurements.append(
                {
                    **_discard_request_value(payload),
                    "repetition": 1,
                    "status": "generated",
                    "total_seconds": 0.0,
                    "ttft_seconds": None,
                }
            )
        return build_report(args, measurements, metadata)

    try:
        import httpx
    except ImportError as exc:
        raise RuntimeError("httpx is required to run the benchmark") from exc

    api_key = ""
    if args.layer != "analyzer":
        api_key = os.getenv(args.api_key_env, "")
        if not api_key:
            raise ValueError(f"environment variable {args.api_key_env} is required")

    with httpx.Client(timeout=args.timeout) as client:
        for payload in payloads:
            for _ in range(args.warmup_runs):
                if args.layer == "analyzer":
                    _analyzer_measurement(
                        client,
                        args.url,
                        _analyzer_text(payload["request_value"]),
                    )
                else:
                    _proxy_measurement(
                        client,
                        url=args.url,
                        api=args.api,
                        model=args.model,
                        api_key=api_key,
                        request_value=payload["request_value"],
                        stream=args.stream,
                    )
            for repetition in range(1, args.repetitions + 1):
                with DockerResourceSampler(
                    args.docker_containers,
                    args.resource_sample_interval,
                ) as resource_sampler:
                    if args.layer == "analyzer":
                        result = _analyzer_measurement(
                            client,
                            args.url,
                            _analyzer_text(payload["request_value"]),
                        )
                    else:
                        result = _proxy_measurement(
                            client,
                            url=args.url,
                            api=args.api,
                            model=args.model,
                            api_key=api_key,
                            request_value=payload["request_value"],
                            stream=args.stream,
                        )
                if args.docker_containers:
                    result["resources"] = resource_sampler.report()
                measurements.append(
                    {
                        **_discard_request_value(payload),
                        "repetition": repetition,
                        **result,
                    }
                )

    return build_report(args, measurements, metadata)


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        report = run(args)
    except (ValueError, RuntimeError) as exc:
        parser.error(str(exc))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(args.output)
    return 0


if __name__ == "__main__":
    sys.exit(main())
