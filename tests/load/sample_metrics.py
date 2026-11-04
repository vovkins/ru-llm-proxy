#!/usr/bin/env python3
"""Collect bounded resource and state metrics from the isolated load contour."""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import signal
import subprocess
import threading
import time
from pathlib import Path


PROJECT_NAME = os.getenv("COMPOSE_PROJECT_NAME", "ru-llm-proxy-load")
BYTE_UNITS = {
    "B": 1,
    "kB": 1_000,
    "MB": 1_000_000,
    "GB": 1_000_000_000,
    "KiB": 1_024,
    "MiB": 1_024**2,
    "GiB": 1_024**3,
}
REDIS_FIELDS = frozenset(
    {
        "connected_clients",
        "blocked_clients",
        "used_memory",
        "used_memory_rss",
        "total_connections_received",
        "total_commands_processed",
        "instantaneous_ops_per_sec",
        "rejected_connections",
        "expired_keys",
        "evicted_keys",
        "keyspace_hits",
        "keyspace_misses",
    }
)
POSTGRES_QUERY = """
SELECT json_build_object(
  'numbackends', d.numbackends,
  'xact_commit', d.xact_commit,
  'xact_rollback', d.xact_rollback,
  'blks_read', d.blks_read,
  'blks_hit', d.blks_hit,
  'tup_returned', d.tup_returned,
  'tup_fetched', d.tup_fetched,
  'tup_inserted', d.tup_inserted,
  'tup_updated', d.tup_updated,
  'tup_deleted', d.tup_deleted,
  'temp_files', d.temp_files,
  'temp_bytes', d.temp_bytes,
  'deadlocks', d.deadlocks,
  'sessions', d.sessions,
  'active_connections', (
    SELECT count(*) FROM pg_stat_activity
    WHERE datname = current_database() AND state = 'active'
  ),
  'idle_in_transaction_connections', (
    SELECT count(*) FROM pg_stat_activity
    WHERE datname = current_database() AND state = 'idle in transaction'
  )
)
FROM pg_stat_database d
WHERE d.datname = current_database();
"""


def run(command: list[str], *, timeout: float = 10) -> str:
    return subprocess.run(
        command,
        check=True,
        capture_output=True,
        text=True,
        timeout=timeout,
    ).stdout.strip()


def parse_bytes(value: str) -> int | None:
    match = re.fullmatch(r"\s*([0-9.]+)\s*([A-Za-z]+)\s*", value)
    if match is None or match.group(2) not in BYTE_UNITS:
        return None
    return round(float(match.group(1)) * BYTE_UNITS[match.group(2)])


def parse_io(value: str) -> tuple[int | None, int | None]:
    parts = value.split("/")
    if len(parts) != 2:
        return None, None
    return parse_bytes(parts[0]), parse_bytes(parts[1])


def measured_memory_bytes(value: object) -> int | None:
    """A transitional zero is unavailable, not a positive configuration limit."""
    if not isinstance(value, str):
        return None
    try:
        used, _ = parse_io(value)
    except (ValueError, OverflowError):
        return None
    return used if used is not None and used > 0 else None


def memory_observation(values: list[int | None], vm_available_bytes: int | None, *,
                       at: float, container_limit_bytes: int,
                       vm_min_available_bytes: int) -> dict:
    known = [value for value in values if type(value) is int and value > 0]
    complete = bool(values) and len(known) == len(values)
    vm_known = type(vm_available_bytes) is int and vm_available_bytes >= 0
    total = sum(known)
    failure = None
    if total > container_limit_bytes:
        failure = "container_memory_guard"
    elif vm_known and vm_available_bytes < vm_min_available_bytes:
        failure = "vm_memory_guard"
    return {"at": at, "memory_complete": complete,
            "known_container_bytes": total if known else None,
            "container_total_bytes": total if complete else None,
            "vm_available_bytes": vm_available_bytes if vm_known else None,
            "protected": complete or vm_known, "failure": failure}


def observer_failure(observation: dict, *, now: float, alive: bool,
                     max_age_seconds: float = 20) -> str | None:
    if not alive:
        return "observer_stopped"
    if observation.get("failure"):
        return observation["failure"]
    protected_at = observation.get("protected_at")
    if (type(protected_at) not in (int, float) or not math.isfinite(protected_at)
            or not 0 <= now - protected_at <= max_age_seconds):
        return "memory_observation_stale"
    return None


def discover_containers() -> dict[str, list[str]]:
    ids = run(
        [
            "docker",
            "ps",
            "--filter",
            f"label=com.docker.compose.project={PROJECT_NAME}",
            "--format",
            "{{.ID}}",
        ]
    ).splitlines()
    services: dict[str, list[str]] = {}
    for container_id in ids:
        if not container_id:
            continue
        service = run(
            [
                "docker",
                "inspect",
                "--format",
                '{{ index .Config.Labels "com.docker.compose.service" }}',
                container_id,
            ]
        )
        services.setdefault(service, []).append(container_id)
    return services


def integer(value: object) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def percentage(value: object) -> float:
    try:
        return float(str(value or "0").rstrip("%"))
    except ValueError:
        return 0.0


def parse_cpu_stats(stat: str, maximum: str) -> dict:
    """Allowlisted cgroup v2 counters; missing data is not zero utilisation."""
    fields = {"usage_usec", "nr_periods", "nr_throttled", "throttled_usec"}
    result = {}
    for line in stat.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[0] in fields and parts[1].isascii() and parts[1].isdecimal():
            result[parts[0]] = int(parts[1])
    values = maximum.split()
    if len(values) == 2 and values[1].isascii() and values[1].isdecimal() and int(values[1]) > 0:
        if values[0] == "max":
            result["quota_cores"] = None
        elif values[0].isascii() and values[0].isdecimal() and int(values[0]) > 0:
            result["quota_cores"] = int(values[0]) / int(values[1])
    return result


def cpu_interval(before: dict, after: dict, seconds: float) -> dict:
    fields = ("usage_usec", "nr_periods", "nr_throttled", "throttled_usec")
    if not math.isfinite(seconds) or seconds <= 0 or not all(name in before and name in after for name in fields):
        return {"available": False}
    if any(after[name] < before[name] for name in fields):
        return {"available": False, "counter_reset": True}
    periods = after["nr_periods"] - before["nr_periods"]
    throttled = after["nr_throttled"] - before["nr_throttled"]
    result = {"available": True, "usage_cores": (after["usage_usec"] - before["usage_usec"]) / seconds / 1e6,
            "throttled_seconds": (after["throttled_usec"] - before["throttled_usec"]) / 1e6,
            "throttled_period_fraction": throttled / periods if periods else None,
            "quota_cores": after.get("quota_cores")}
    if before.get("quota_cores") != after.get("quota_cores"):
        result.update(quota_changed=True, quota_cores=None)
    return result


def cpu_stat_sample(container_id: str) -> dict:
    try:
        output = run(["docker", "exec", container_id, "sh", "-c",
                      "cat /sys/fs/cgroup/cpu.stat && printf '\\n--cpu-max--\\n' && cat /sys/fs/cgroup/cpu.max"])
    except subprocess.SubprocessError:
        return {}
    stat, separator, maximum = output.partition("\n--cpu-max--\n")
    return parse_cpu_stats(stat, maximum) if separator else {}


def parse_memory_stats(events: str, current: str, maximum: str) -> dict:
    """Read cgroup v2 memory pressure without retaining arbitrary file content."""
    counters = {}
    for line in events.splitlines():
        parts = line.split()
        if (len(parts) == 2 and parts[0] in {"low", "high", "max", "oom", "oom_kill", "oom_group_kill"}
                and parts[1].isascii() and parts[1].isdecimal()):
            counters[parts[0]] = int(parts[1])
    result = {"events": counters} if counters else {}
    for name, raw in (("current_bytes", current.strip()), ("limit_bytes", maximum.strip())):
        if raw.isascii() and raw.isdecimal():
            result[name] = int(raw)
        elif name == "limit_bytes" and raw == "max":
            result[name] = None
    return result


def memory_stat_sample(container_id: str) -> dict:
    try:
        output = run(["docker", "exec", container_id, "sh", "-c",
                      "cat /sys/fs/cgroup/memory.events && printf '\\n--memory-current--\\n' "
                      "&& cat /sys/fs/cgroup/memory.current && printf '\\n--memory-max--\\n' "
                      "&& cat /sys/fs/cgroup/memory.max"])
    except subprocess.SubprocessError:
        return {}
    events, separator, rest = output.partition("\n--memory-current--\n")
    current, second, maximum = rest.partition("\n--memory-max--\n")
    return parse_memory_stats(events, current, maximum) if separator and second else {}


def docker_samples(container_map: dict[str, list[str]], timestamp: int, *, collect_cpu_stats=False) -> list[dict]:
    reverse = {
        container_id: service
        for service, container_ids in container_map.items()
        for container_id in container_ids
    }
    if not reverse:
        return []
    output = run(
        [
            "docker",
            "stats",
            "--no-stream",
            "--format",
            "{{json .}}",
            *reverse,
        ],
        timeout=30,
    )
    samples: list[dict] = []
    for line in output.splitlines():
        raw = json.loads(line)
        container_id = str(raw.get("ID", ""))[:12]
        memory_used, memory_limit = parse_io(str(raw.get("MemUsage", "")))
        network_rx, network_tx = parse_io(str(raw.get("NetIO", "")))
        block_read, block_write = parse_io(str(raw.get("BlockIO", "")))
        service = next(
            (
                value
                for known_id, value in reverse.items()
                if known_id.startswith(container_id) or container_id.startswith(known_id)
            ),
            "unknown",
        )
        samples.append(
            {
                "timestamp": timestamp,
                "service": service,
                "container_id": container_id,
                "cpu_percent": percentage(raw.get("CPUPerc")),
                "memory_used_bytes": memory_used,
                "memory_limit_bytes": memory_limit,
                "network_rx_bytes": network_rx,
                "network_tx_bytes": network_tx,
                "block_read_bytes": block_read,
                "block_write_bytes": block_write,
                "pids": integer(raw.get("PIDs")),
            }
        )
        if collect_cpu_stats:
            samples[-1]["cpu_stat"] = cpu_stat_sample(container_id)
            samples[-1]["cpu_stat_monotonic_seconds"] = time.monotonic()
    return samples


def redis_sample(container_id: str, timestamp: int) -> dict:
    output = run(["docker", "exec", container_id, "redis-cli", "--raw", "INFO"])
    metrics: dict[str, int | float] = {}
    for line in output.splitlines():
        if ":" not in line or line.startswith("#"):
            continue
        name, raw_value = line.split(":", 1)
        if name not in REDIS_FIELDS:
            continue
        try:
            metrics[name] = int(raw_value)
        except ValueError:
            try:
                metrics[name] = float(raw_value)
            except ValueError:
                continue
    return {"timestamp": timestamp, **metrics}


def postgres_sample(container_id: str, timestamp: int) -> dict:
    output = run(
        [
            "docker",
            "exec",
            container_id,
            "psql",
            "-U",
            "load",
            "-d",
            "litellm",
            "-At",
            "-c",
            POSTGRES_QUERY,
        ]
    )
    payload = json.loads(output)
    return {"timestamp": timestamp, **payload}


def append_jsonl(path: Path, values: list[dict]) -> None:
    if not values:
        return
    with path.open("a", encoding="utf-8") as output:
        for value in values:
            output.write(json.dumps(value, sort_keys=True) + "\n")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--interval", type=float, default=1.0)
    parser.add_argument("--cpu-stats", action="store_true", help="Collect optional cgroup v2 CPU quota/throttling counters")
    args = parser.parse_args()
    if args.interval < 0.2:
        raise SystemExit("interval must be at least 0.2 seconds")
    args.output_dir.mkdir(parents=True, exist_ok=True)

    stopped = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stopped.set())
    signal.signal(signal.SIGINT, lambda *_: stopped.set())

    while not stopped.is_set():
        started = time.monotonic()
        timestamp = int(time.time())
        try:
            containers = discover_containers()
            try:
                resource_samples = docker_samples(containers, timestamp, collect_cpu_stats=args.cpu_stats)
            except subprocess.SubprocessError:
                # A short-lived generator can disappear between discovery and
                # docker stats. Refresh once so normal teardown is not an error.
                containers = discover_containers()
                resource_samples = docker_samples(containers, timestamp, collect_cpu_stats=args.cpu_stats)
            append_jsonl(args.output_dir / "docker-stats.jsonl", resource_samples)
            redis_ids = containers.get("load-redis", [])
            if redis_ids:
                append_jsonl(
                    args.output_dir / "redis-stats.jsonl",
                    [redis_sample(redis_ids[0], timestamp)],
                )
            database_ids = containers.get("load-db", [])
            if database_ids:
                append_jsonl(
                    args.output_dir / "postgres-stats.jsonl",
                    [postgres_sample(database_ids[0], timestamp)],
                )
        except (OSError, ValueError, json.JSONDecodeError, subprocess.SubprocessError):
            append_jsonl(
                args.output_dir / "sampler-status.jsonl",
                [{"timestamp": timestamp, "status": "sample_failed"}],
            )
        remaining = args.interval - (time.monotonic() - started)
        stopped.wait(max(remaining, 0))


if __name__ == "__main__":
    main()
