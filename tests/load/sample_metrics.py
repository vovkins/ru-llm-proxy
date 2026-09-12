#!/usr/bin/env python3
"""Collect bounded resource and state metrics from the isolated load contour."""

from __future__ import annotations

import argparse
import json
import re
import signal
import subprocess
import threading
import time
from pathlib import Path


PROJECT_NAME = "ru-llm-proxy-load"
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


def docker_samples(container_map: dict[str, list[str]], timestamp: int) -> list[dict]:
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
                resource_samples = docker_samples(containers, timestamp)
            except subprocess.SubprocessError:
                # A short-lived generator can disappear between discovery and
                # docker stats. Refresh once so normal teardown is not an error.
                containers = discover_containers()
                resource_samples = docker_samples(containers, timestamp)
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
