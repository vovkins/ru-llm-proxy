#!/usr/bin/env python3
"""Build one bounded summary from ru-llm-proxy load-test artifacts."""

from __future__ import annotations

import argparse
import json
import re
from collections import defaultdict
from pathlib import Path
from typing import Iterable


REDIS_COUNTERS = (
    "total_connections_received",
    "total_commands_processed",
    "rejected_connections",
    "expired_keys",
    "evicted_keys",
    "keyspace_hits",
    "keyspace_misses",
)
POSTGRES_COUNTERS = (
    "xact_commit",
    "xact_rollback",
    "blks_read",
    "blks_hit",
    "tup_returned",
    "tup_fetched",
    "tup_inserted",
    "tup_updated",
    "tup_deleted",
    "temp_files",
    "temp_bytes",
    "deadlocks",
    "sessions",
)
PROMETHEUS_COUNTERS = frozenset(
    {
        "ru_presidio_analyzer_requests_total",
        "ru_presidio_analyzer_capacity_rejections_total",
        "ru_presidio_analyzer_failures_total",
        "ru_pii_guardrail_pre_calls_total",
        "ru_pii_guardrail_post_calls_total",
        "ru_pii_guardrail_fail_closed_total",
        "ru_pii_guardrail_analysis_cache_requests_total",
    }
)
PROMETHEUS_LINE = re.compile(
    r"^(?P<name>[a-zA-Z_:][a-zA-Z0-9_:]*)(?P<labels>\{[^}]*\})?\s+"
    r"(?P<value>[0-9.eE+-]+)$"
)


def read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    values: list[dict] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            value = json.loads(line)
            if isinstance(value, dict):
                values.append(value)
    return values


def maximum(values: Iterable[float | int | None]) -> float | int | None:
    available = [value for value in values if isinstance(value, (int, float))]
    return max(available) if available else None


def counter_deltas(samples: list[dict], names: tuple[str, ...]) -> dict[str, int | float]:
    if len(samples) < 2:
        return {}
    first, last = samples[0], samples[-1]
    result = {}
    for name in names:
        if isinstance(first.get(name), (int, float)) and isinstance(
            last.get(name), (int, float)
        ):
            result[name] = max(last[name] - first[name], 0)
    return result


def prometheus_counters(paths: Iterable[Path]) -> dict[str, float]:
    result: dict[str, float] = defaultdict(float)
    for path in paths:
        for line in path.read_text(encoding="utf-8").splitlines():
            match = PROMETHEUS_LINE.fullmatch(line.strip())
            if match is None or match.group("name") not in PROMETHEUS_COUNTERS:
                continue
            key = match.group("name") + (match.group("labels") or "")
            result[key] += float(match.group("value"))
    return dict(sorted(result.items()))


def resource_summary(samples: list[dict]) -> dict[str, dict]:
    grouped: dict[str, list[dict]] = defaultdict(list)
    for sample in samples:
        grouped[str(sample.get("service", "unknown"))].append(sample)

    result: dict[str, dict] = {}
    for service, service_samples in sorted(grouped.items()):
        by_timestamp: dict[int, list[dict]] = defaultdict(list)
        for sample in service_samples:
            by_timestamp[int(sample.get("timestamp", 0))].append(sample)
        total_cpu = [
            sum(float(item.get("cpu_percent") or 0) for item in instant)
            for instant in by_timestamp.values()
        ]
        total_memory = [
            sum(int(item.get("memory_used_bytes") or 0) for item in instant)
            for instant in by_timestamp.values()
        ]
        result[service] = {
            "replicas_seen": len(
                {str(item.get("container_id")) for item in service_samples}
            ),
            "peak_total_cpu_percent": round(max(total_cpu), 3) if total_cpu else None,
            "peak_total_memory_bytes": max(total_memory) if total_memory else None,
            "peak_single_replica_memory_bytes": maximum(
                item.get("memory_used_bytes") for item in service_samples
            ),
            "peak_pids_per_replica": maximum(
                item.get("pids") for item in service_samples
            ),
        }
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("results_dir", type=Path)
    args = parser.parse_args()
    summaries = sorted(args.results_dir.glob("summary-*.json"))
    request_summary = (
        json.loads(summaries[0].read_text(encoding="utf-8")) if summaries else {}
    )
    redis = read_jsonl(args.results_dir / "redis-stats.jsonl")
    postgres = read_jsonl(args.results_dir / "postgres-stats.jsonl")
    sampler_status = read_jsonl(args.results_dir / "sampler-status.jsonl")

    result = {
        "schema_version": 1,
        "request_summary": request_summary,
        "resources": resource_summary(
            read_jsonl(args.results_dir / "docker-stats.jsonl")
        ),
        "redis": {
            "peak_connected_clients": maximum(
                item.get("connected_clients") for item in redis
            ),
            "peak_blocked_clients": maximum(
                item.get("blocked_clients") for item in redis
            ),
            "peak_used_memory_bytes": maximum(item.get("used_memory") for item in redis),
            "counter_deltas": counter_deltas(redis, REDIS_COUNTERS),
        },
        "postgres": {
            "peak_connections": maximum(item.get("numbackends") for item in postgres),
            "peak_active_connections": maximum(
                item.get("active_connections") for item in postgres
            ),
            "peak_idle_in_transaction_connections": maximum(
                item.get("idle_in_transaction_connections") for item in postgres
            ),
            "counter_deltas": counter_deltas(postgres, POSTGRES_COUNTERS),
        },
        "prometheus_counters": {
            "analyzer": prometheus_counters(
                sorted(args.results_dir.glob("analyzer-metrics-*.prom"))
            ),
            "guardrail": prometheus_counters(
                sorted(args.results_dir.glob("guardrail-metrics-*.prom"))
            ),
        },
        "sampler_failed_samples": len(sampler_status),
    }
    (args.results_dir / "run-summary.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
