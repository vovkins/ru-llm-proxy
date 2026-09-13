#!/usr/bin/env python3
"""Inject component failures and validate recovery without exposing request data."""

from __future__ import annotations

import argparse
import csv
import http.client
import json
import socket
import subprocess
import time
import urllib.parse
import uuid
from collections import Counter
from pathlib import Path
from typing import Any

from stateful_checks import (
    KNOWN_DEPLOYMENTS,
    http_request,
    provider_capture,
    redis_snapshot,
    safe_provider_capture,
    wait_for_no_mappings,
    write_json,
)


SCENARIO_SERVICES = {
    "analyzer": "load-presidio-analyzer",
    "litellm": "load-litellm",
    "redis": "load-redis",
    "postgres": "load-db",
}


def compose_command(compose_file: Path, *args: str) -> list[str]:
    return ["docker", "compose", "-f", str(compose_file), *args]


def run_command(args: list[str], *, timeout: float = 60) -> str:
    result = subprocess.run(
        args,
        check=True,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    return result.stdout.strip()


def service_container_ids(compose_file: Path, service: str) -> list[str]:
    output = run_command(compose_command(compose_file, "ps", "-q", service))
    return sorted(line for line in output.splitlines() if line)


def container_state(container_id: str) -> dict[str, Any]:
    output = run_command(
        ["docker", "inspect", "--format", "{{json .State}}", container_id],
        timeout=15,
    )
    state = json.loads(output)
    return state if isinstance(state, dict) else {}


def wait_container_healthy(container_id: str, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        state = container_state(container_id)
        health = state.get("Health")
        health_status = health.get("Status") if isinstance(health, dict) else None
        if state.get("Running") and health_status in {None, "healthy"}:
            return
        if state.get("Dead"):
            raise RuntimeError("restarted container entered dead state")
        time.sleep(1)
    raise TimeoutError("container did not become healthy before recovery timeout")


def wait_proxy_ready(base_url: str, master_key: str, timeout: float) -> int:
    deadline = time.monotonic() + timeout
    last_status = 0
    while time.monotonic() < deadline:
        result = http_request(base_url, "/models", token=master_key, timeout=5)
        last_status = result.status
        if result.status == 200:
            return result.status
        time.sleep(1)
    raise TimeoutError(f"LiteLLM did not recover; last status={last_status}")


def admin_round_trip(
    base_url: str,
    master_key: str,
    *,
    delete_timeout: float = 15,
) -> dict[str, int]:
    alias = f"resilience-probe-{uuid.uuid4().hex[:16]}"
    created = http_request(
        base_url,
        "/key/generate",
        token=master_key,
        payload={
            "key_alias": alias,
            "models": ["mock-chat"],
            "duration": "5m",
            "metadata": {"purpose": "resilience-recovery-probe"},
        },
        timeout=15,
    )
    deleted_status = 0
    if created.status == 200:
        deadline = time.monotonic() + delete_timeout
        while time.monotonic() < deadline:
            deleted = http_request(
                base_url,
                "/key/delete",
                token=master_key,
                payload={"key_aliases": [alias]},
                timeout=5,
            )
            deleted_status = deleted.status
            if deleted_status == 200:
                break
            time.sleep(1)
    return {"create_status": created.status, "delete_status": deleted_status}


def wait_admin_ready(
    base_url: str,
    master_key: str,
    timeout: float,
) -> dict[str, int]:
    deadline = time.monotonic() + timeout
    last_result = {"create_status": 0, "delete_status": 0}
    while time.monotonic() < deadline:
        remaining = max(1.0, deadline - time.monotonic())
        last_result = admin_round_trip(
            base_url,
            master_key,
            delete_timeout=min(15, remaining),
        )
        if last_result == {"create_status": 200, "delete_status": 200}:
            return last_result
        time.sleep(1)
    raise TimeoutError(f"LiteLLM administration did not recover: {last_result}")


def outage_probe(
    scenario: str,
    base_url: str,
    master_key: str,
) -> dict[str, int | str]:
    if scenario == "postgres":
        alias = f"resilience-outage-{uuid.uuid4().hex[:16]}"
        result = http_request(
            base_url,
            "/key/generate",
            token=master_key,
            payload={
                "key_alias": alias,
                "models": ["mock-chat"],
                "duration": "1m",
                "metadata": {"purpose": "resilience-outage-probe"},
            },
            timeout=3,
        )
        return {"operation": "key_generate", "status": result.status}
    result = http_request(base_url, "/models", token=master_key, timeout=3)
    return {"operation": "models", "status": result.status}


def select_fault_target(
    compose_file: Path,
    scenario: str,
) -> tuple[str, int]:
    service = SCENARIO_SERVICES[scenario]
    container_ids = service_container_ids(compose_file, service)
    minimum = 2 if scenario in {"analyzer", "litellm"} else 1
    if len(container_ids) < minimum:
        raise RuntimeError(
            f"scenario {scenario} requires at least {minimum} running {service} containers"
        )
    return container_ids[-1], len(container_ids)


def wait_for_user_successes(
    results_dir: Path,
    expected_users: int,
    *,
    since: int | None,
    timeout: float,
) -> dict[str, int]:
    """Wait until every expected virtual user records a successful response."""
    expected_user_ids = {str(index) for index in range(expected_users)}
    deadline = time.monotonic() + timeout
    successful_users: set[str] = set()
    while time.monotonic() < deadline:
        successful_users.clear()
        for sample_path in sorted(results_dir.glob("samples-*.csv")):
            with sample_path.open(encoding="utf-8", newline="") as handle:
                for sample in csv.DictReader(handle):
                    if sample.get("error_kind"):
                        continue
                    timestamp = int(float(sample.get("timestamp") or 0))
                    if since is not None and timestamp < since:
                        continue
                    user_index = sample.get("user_index", "")
                    if user_index in expected_user_ids:
                        successful_users.add(user_index)
        if successful_users == expected_user_ids:
            return {
                "completed_at": int(time.time()),
                "successful_user_count": len(successful_users),
            }
        time.sleep(1)
    raise TimeoutError(
        "not all virtual users recorded a successful response before timeout; "
        f"recovered={len(successful_users)}/{expected_users}"
    )


def inject(args: argparse.Namespace) -> None:
    scenarios = tuple(item.strip() for item in args.scenarios.split(",") if item.strip())
    unknown = sorted(set(scenarios) - set(SCENARIO_SERVICES))
    if not scenarios or unknown:
        raise ValueError(f"unsupported resilience scenarios: {unknown}")

    report: dict[str, Any] = {
        "schema_version": 1,
        "scenarios": list(scenarios),
        "events": [],
    }
    report_path = args.results_dir / "fault-events.json"
    write_json(report_path, report)
    time.sleep(args.initial_delay_seconds)
    warmup_started_at = int(time.time())
    report["warmup"] = {"started_at": warmup_started_at, "outcome": "waiting"}
    write_json(report_path, report)
    try:
        warmup = wait_for_user_successes(
            args.results_dir,
            args.expected_users,
            since=None,
            timeout=args.user_recovery_timeout_seconds,
        )
    except Exception as exc:
        report["warmup"].update(
            {
                "outcome": "failed",
                "error_type": type(exc).__name__,
            }
        )
        write_json(report_path, report)
        raise
    report["warmup"].update(
        {
            "outcome": "completed",
            **warmup,
            "duration_seconds": warmup["completed_at"] - warmup_started_at,
        }
    )
    write_json(report_path, report)

    for index, scenario in enumerate(scenarios):
        service = SCENARIO_SERVICES[scenario]
        event: dict[str, Any] = {
            "scenario": scenario,
            "service": service,
            "replicas_before": 0,
            "fault_started_at": int(time.time()),
            "outcome": "injecting",
        }
        report["events"].append(event)
        write_json(report_path, report)

        container_id = ""
        try:
            container_id, replica_count = select_fault_target(
                args.compose_file,
                scenario,
            )
            event["replicas_before"] = replica_count
            run_command(["docker", "kill", container_id], timeout=30)
            event["container_stopped_at"] = int(time.time())
            write_json(report_path, report)

            probe_started = time.monotonic()
            event["outage_probe"] = outage_probe(
                scenario,
                args.base_url,
                args.master_key,
            )
            elapsed = time.monotonic() - probe_started
            remaining = max(0.0, args.downtime_seconds - elapsed)
            if remaining:
                time.sleep(remaining)

            run_command(["docker", "start", container_id], timeout=30)
            event["container_started_at"] = int(time.time())
            wait_container_healthy(container_id, args.recovery_timeout_seconds)
            event["container_healthy_at"] = int(time.time())
            event["proxy_probe_status"] = wait_proxy_ready(
                args.base_url,
                args.master_key,
                args.recovery_timeout_seconds,
            )
            event["admin_probe"] = wait_admin_ready(
                args.base_url,
                args.master_key,
                args.recovery_timeout_seconds,
            )
            event["recovered_at"] = int(time.time())
            user_recovery = wait_for_user_successes(
                args.results_dir,
                args.expected_users,
                since=event["recovered_at"],
                timeout=args.user_recovery_timeout_seconds,
            )
            event["application_recovered_at"] = user_recovery["completed_at"]
            event["application_recovery_seconds"] = (
                event["application_recovered_at"] - event["recovered_at"]
            )
            event["recovered_user_count"] = user_recovery[
                "successful_user_count"
            ]
            event["outcome"] = "recovered"
        except Exception as exc:
            event["outcome"] = "failed"
            event["error_type"] = type(exc).__name__
            if container_id:
                try:
                    run_command(["docker", "start", container_id], timeout=30)
                except Exception:
                    event["emergency_restart_failed"] = True
            write_json(report_path, report)
            raise
        finally:
            write_json(report_path, report)

        if index + 1 < len(scenarios):
            time.sleep(args.between_seconds)


def _close_connection(connection: http.client.HTTPConnection) -> None:
    sock = connection.sock
    if sock is not None:
        try:
            sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
    connection.close()


def shorten_mapping_ttls(compose_file: Path, ttl_seconds: float) -> None:
    """Bound cancellation cleanup time without shortening the analysis cache."""
    container_ids = service_container_ids(compose_file, "load-redis")
    if len(container_ids) != 1:
        raise RuntimeError("cancellation probe requires exactly one load Redis")
    ttl_ms = max(1, int(ttl_seconds * 1_000))
    script = """
local cursor = '0'
repeat
  local result = redis.call('SCAN', cursor, 'MATCH', 'pii_mapping:*', 'COUNT', 1000)
  cursor = result[1]
  for _, key in ipairs(result[2]) do
    redis.call('PEXPIRE', key, ARGV[1])
  end
until cursor == '0'
return 1
""".strip()
    run_command(
        [
            "docker",
            "exec",
            container_ids[0],
            "redis-cli",
            "--raw",
            "EVAL",
            script,
            "0",
            str(ttl_ms),
        ],
        timeout=30,
    )


def finalize_mapping_state(
    compose_file: Path,
    *,
    mapping_ttl_seconds: float,
    cleanup_ttl_seconds: float,
    cleanup_grace_seconds: float,
    allow_ttl_bounded_residual: bool,
) -> tuple[dict[str, Any], dict[str, Any], list[str]]:
    """Record explainable orphan mappings, then clean the isolated test store."""
    observed = redis_snapshot(compose_file)
    mapping_state = observed["pii_mappings"]
    mapping_count = int(mapping_state.get("count") or 0)
    invalid_ttl_count = int(mapping_state.get("invalid_ttl_count") or 0)
    max_ttl_ms = mapping_state.get("max_ttl_ms")
    configured_ttl_ms = int(mapping_ttl_seconds * 1_000)
    ttl_is_bounded = (
        mapping_count > 0
        and invalid_ttl_count == 0
        and isinstance(max_ttl_ms, int)
        and 0 < max_ttl_ms <= configured_ttl_ms
    )
    failures: list[str] = []
    accepted = mapping_count > 0 and allow_ttl_bounded_residual and ttl_is_bounded
    if mapping_count > 0 and not accepted:
        failures.append("unexplained_or_unbounded_residual_mappings")

    cleanup_shortened = False
    final = observed
    if mapping_count > 0:
        cleanup_shortened = True
        shorten_mapping_ttls(compose_file, cleanup_ttl_seconds)
        try:
            final = wait_for_no_mappings(
                compose_file,
                timeout=cleanup_ttl_seconds + cleanup_grace_seconds,
            )
        except AssertionError:
            final = redis_snapshot(compose_file)
            failures.append("residual_mapping_cleanup_failed")

    report = {
        "observed": mapping_state,
        "ttl_bounded": ttl_is_bounded,
        "accepted_after_redis_outage": accepted,
        "test_ttl_shortened": cleanup_shortened,
        "cleanup_ttl_seconds": cleanup_ttl_seconds,
        "final_pii_mapping_count": final["pii_mappings"]["count"],
    }
    return final, report, failures


def cancel_request(args: argparse.Namespace) -> None:
    before = redis_snapshot(args.compose_file)
    baseline_count = before["pii_mappings"]["count"]
    marker = "statecancel@example.test"
    payload = json.dumps(
        {
            "model": "mock-chat",
            "stream": False,
            "messages": [
                {
                    "role": "user",
                    "content": f"{marker} LOAD_UPSTREAM_FAIL_TIMEOUT",
                }
            ],
        }
    ).encode("utf-8")
    url = urllib.parse.urlsplit(args.base_url)
    connection = http.client.HTTPConnection(
        url.hostname,
        url.port or 80,
        timeout=args.cancellation_observe_timeout_seconds,
    )
    connection.request(
        "POST",
        "/v1/chat/completions",
        body=payload,
        headers={
            "Authorization": f"Bearer {args.master_key}",
            "Content-Type": "application/json",
            "Content-Length": str(len(payload)),
        },
    )

    observed_at: int | None = None
    deadline = time.monotonic() + args.cancellation_observe_timeout_seconds
    while time.monotonic() < deadline:
        snapshot = redis_snapshot(args.compose_file)
        if snapshot["pii_mappings"]["count"] > baseline_count:
            observed_at = int(time.time())
            break
        time.sleep(0.1)
    if observed_at is None:
        _close_connection(connection)
        raise AssertionError("cancellation probe did not observe a request mapping")

    observed_mapping = redis_snapshot(args.compose_file)["pii_mappings"]
    observed_ttl_ms = observed_mapping.get("max_ttl_ms")
    if not isinstance(observed_ttl_ms, int) or observed_ttl_ms <= 0:
        _close_connection(connection)
        raise AssertionError("cancellation mapping does not have a positive TTL")
    configured_ttl_ms = int(args.mapping_ttl_seconds * 1_000)
    if observed_ttl_ms > configured_ttl_ms:
        _close_connection(connection)
        raise AssertionError("cancellation mapping TTL exceeds configured bound")

    closed_at = int(time.time())
    _close_connection(connection)

    immediate_cleanup = False
    immediate_deadline = time.monotonic() + args.cancellation_immediate_timeout_seconds
    while time.monotonic() < immediate_deadline:
        snapshot = redis_snapshot(args.compose_file)
        if snapshot["pii_mappings"]["count"] <= baseline_count:
            immediate_cleanup = True
            break
        time.sleep(0.25)

    if not immediate_cleanup:
        shorten_mapping_ttls(
            args.compose_file,
            args.cancellation_test_ttl_seconds,
        )
    eventual_started = time.monotonic()
    eventual = wait_for_no_mappings(
        args.compose_file,
        timeout=(
            args.cancellation_test_ttl_seconds
            + args.cancellation_expiry_grace_seconds
        ),
    )
    eventual_cleanup_seconds = round(time.monotonic() - eventual_started, 3)
    captures = {
        name: safe_provider_capture(provider_capture(args.compose_file, name))
        for name in ("load-mock-upstream-a", "load-mock-upstream-b")
    }
    provider_received_raw = any(
        value.get("provider_saw_synthetic_marker") for value in captures.values()
    )
    report = {
        "schema_version": 1,
        "mapping_observed_at": observed_at,
        "connection_closed_at": closed_at,
        "configured_mapping_ttl_seconds": args.mapping_ttl_seconds,
        "observed_mapping_ttl_ms": observed_ttl_ms,
        "immediate_cleanup": immediate_cleanup,
        "test_ttl_shortened": not immediate_cleanup,
        "cancellation_test_ttl_seconds": args.cancellation_test_ttl_seconds,
        "eventual_cleanup_seconds": eventual_cleanup_seconds,
        "final_pii_mapping_count": eventual["pii_mappings"]["count"],
        "provider_received_unmasked_marker": provider_received_raw,
    }
    write_json(args.results_dir / "client-cancellation.json", report)
    if provider_received_raw:
        raise AssertionError("provider received an unmasked cancellation marker")


def fault_windows(events: list[dict[str, Any]], grace_seconds: float) -> list[tuple[int, int]]:
    windows = []
    for event in events:
        started = int(event.get("fault_started_at", 0))
        recovered = int(event.get("recovered_at", 0))
        if started and recovered:
            windows.append((started - 1, recovered + int(grace_seconds)))
    return windows


def application_recovery(
    events: list[dict[str, Any]],
    samples: list[dict[str, str]],
    expected_users: int,
    grace_seconds: float,
) -> list[dict[str, Any]]:
    """Measure when every virtual user succeeds after each component recovery."""
    expected_user_ids = {str(index) for index in range(expected_users)}
    recovery: list[dict[str, Any]] = []
    for index, event in enumerate(events):
        component_recovered_at = int(event.get("recovered_at", 0))
        next_fault_at = (
            int(events[index + 1].get("fault_started_at", 0))
            if index + 1 < len(events)
            else None
        )
        first_success_by_user: dict[str, int] = {}
        for sample in samples:
            if sample.get("error_kind"):
                continue
            timestamp = int(float(sample.get("timestamp") or 0))
            user_index = sample.get("user_index", "")
            if timestamp < component_recovered_at:
                continue
            if next_fault_at is not None and timestamp >= next_fault_at:
                continue
            if user_index in expected_user_ids:
                first_success_by_user.setdefault(user_index, timestamp)

        application_recovered_at = (
            max(first_success_by_user.values())
            if len(first_success_by_user) == expected_users
            else None
        )
        window_end = (
            application_recovered_at + int(grace_seconds)
            if application_recovered_at is not None
            else (next_fault_at - 1 if next_fault_at is not None else 2**63 - 1)
        )
        recovery.append(
            {
                "scenario": event.get("scenario"),
                "component_recovered_at": component_recovered_at,
                "application_recovered_at": application_recovered_at,
                "application_recovery_seconds": (
                    application_recovered_at - component_recovered_at
                    if application_recovered_at is not None
                    else None
                ),
                "recovered_user_count": len(first_success_by_user),
                "expected_user_count": expected_users,
                "fault_window": [
                    int(event.get("fault_started_at", 0)) - 1,
                    window_end,
                ],
            }
        )
    return recovery


def timestamp_in_windows(timestamp: int, windows: list[tuple[int, int]]) -> bool:
    return any(start <= timestamp <= end for start, end in windows)


def scenario_for_timestamp(
    timestamp: int,
    recovery: list[dict[str, Any]],
) -> str | None:
    for item in recovery:
        start, end = item["fault_window"]
        if start <= timestamp <= end:
            return str(item.get("scenario") or "unknown")
    return None


def affinity_transitions(
    samples: list[dict[str, str]],
    redis_fault_started_at: int | None,
) -> dict[str, int]:
    last_by_user: dict[str, str] = {}
    counts: Counter[str] = Counter()
    for sample in samples:
        if sample.get("error_kind"):
            continue
        deployment = sample.get("deployment_id", "")
        user_index = sample.get("user_index", "")
        if not user_index or deployment not in KNOWN_DEPLOYMENTS:
            continue
        previous = last_by_user.get(user_index)
        if previous is not None and previous != deployment:
            timestamp = int(float(sample.get("timestamp") or 0))
            key = (
                "after_redis_fault"
                if redis_fault_started_at is not None
                and timestamp >= redis_fault_started_at
                else "before_redis_fault"
            )
            counts[key] += 1
        last_by_user[user_index] = deployment
    return dict(sorted(counts.items()))


def validate(args: argparse.Namespace) -> None:
    fault_report = json.loads(
        (args.results_dir / "fault-events.json").read_text(encoding="utf-8")
    )
    cancellation_report = json.loads(
        (args.results_dir / "client-cancellation.json").read_text(encoding="utf-8")
    )
    summaries = sorted(args.results_dir.glob("summary-*.json"))
    if not summaries:
        raise AssertionError("Locust summary is missing")
    summary = json.loads(summaries[0].read_text(encoding="utf-8"))
    samples = list(
        csv.DictReader(
            (args.results_dir / f"samples-{summary['node']}.csv").open(
                encoding="utf-8"
            )
        )
    )

    expected_scenarios = [
        item.strip() for item in args.scenarios.split(",") if item.strip()
    ]
    events = fault_report.get("events", [])
    failures: list[str] = []
    warmup = fault_report.get("warmup", {})
    if (
        warmup.get("outcome") != "completed"
        or warmup.get("successful_user_count") != args.expected_users
    ):
        failures.append("warmup_incomplete")
    if [event.get("scenario") for event in events] != expected_scenarios:
        failures.append("fault_scenarios_incomplete")
    if any(event.get("outcome") != "recovered" for event in events):
        failures.append("component_recovery_failed")
    for event in events:
        admin_probe = event.get("admin_probe", {})
        if admin_probe.get("create_status") != 200 or admin_probe.get(
            "delete_status"
        ) != 200:
            failures.append("post_recovery_admin_probe_failed")

    recovery = application_recovery(
        events,
        samples,
        args.expected_users,
        args.recovery_grace_seconds,
    )
    for item in recovery:
        if item["application_recovered_at"] is None:
            failures.append(f"users_not_recovered_after_{item['scenario']}")

    unexpected_error_counts: Counter[str] = Counter()
    fault_error_counts: dict[str, Counter[str]] = {
        str(item["scenario"]): Counter() for item in recovery
    }
    fault_error_type_counts: dict[str, Counter[str]] = {
        str(item["scenario"]): Counter() for item in recovery
    }
    successful_users: Counter[str] = Counter()
    deployments = set()
    for sample in samples:
        timestamp = int(float(sample.get("timestamp") or 0))
        error_kind = sample.get("error_kind", "")
        validation_result = sample.get("validation_result", "")
        if validation_result == "mismatch":
            failures.append("mapping_isolation_mismatch")
        if error_kind:
            scenario = scenario_for_timestamp(timestamp, recovery)
            if scenario is None:
                unexpected_error_counts[error_kind] += 1
            else:
                fault_error_counts[scenario][error_kind] += 1
                error_type = sample.get("error_type", "")
                if error_type:
                    fault_error_type_counts[scenario][error_type] += 1
            continue
        user_index = sample.get("user_index", "")
        deployment = sample.get("deployment_id", "")
        successful_users[user_index] += 1
        if deployment not in KNOWN_DEPLOYMENTS:
            failures.append("unknown_or_missing_deployment")
        else:
            deployments.add(deployment)
    if unexpected_error_counts:
        failures.append("errors_outside_fault_windows")
    if len(successful_users) != args.expected_users:
        failures.append("not_all_users_recovered")
    if min(successful_users.values(), default=0) < 2:
        failures.append("insufficient_successes_per_user")
    if deployments != KNOWN_DEPLOYMENTS:
        failures.append("deployments_not_distributed")

    if events:
        first_fault = int(events[0].get("fault_started_at", 0))
        last_recovery = int(events[-1].get("recovered_at", 0))
        users_before = {
            sample.get("user_index", "")
            for sample in samples
            if not sample.get("error_kind")
            and int(float(sample.get("timestamp") or 0)) <= first_fault
        }
        users_after = {
            sample.get("user_index", "")
            for sample in samples
            if not sample.get("error_kind")
            and int(float(sample.get("timestamp") or 0))
            > last_recovery + args.recovery_grace_seconds
        }
        if len(users_before) != args.expected_users:
            failures.append("users_not_warmed_before_faults")
        if len(users_after) != args.expected_users:
            failures.append("users_not_recovered_after_faults")

    redis_fault_started_at = next(
        (
            int(event["fault_started_at"])
            for event in events
            if event.get("scenario") == "redis"
        ),
        None,
    )
    transitions = affinity_transitions(samples, redis_fault_started_at)
    if transitions.get("before_redis_fault", 0):
        failures.append("affinity_changed_before_redis_fault")

    allow_ttl_bounded_residual = (
        any(
            event.get("scenario") == "redis" and event.get("outcome") == "recovered"
            for event in events
        )
        and fault_error_type_counts.get("redis", {}).get(
            "guardrail_dependency_unavailable", 0
        )
        > 0
    )
    final_redis, residual_mapping_cleanup, mapping_failures = (
        finalize_mapping_state(
            args.compose_file,
            mapping_ttl_seconds=args.mapping_ttl_seconds,
            cleanup_ttl_seconds=args.cancellation_test_ttl_seconds,
            cleanup_grace_seconds=args.cancellation_expiry_grace_seconds,
            allow_ttl_bounded_residual=allow_ttl_bounded_residual,
        )
    )
    failures.extend(mapping_failures)
    for name in ("analysis_cache_entries", "deployment_affinity"):
        if final_redis[name]["invalid_ttl_count"]:
            failures.append(f"invalid_ttl_{name}")

    captures = {
        name: safe_provider_capture(provider_capture(args.compose_file, name))
        for name in ("load-mock-upstream-a", "load-mock-upstream-b")
    }
    if any(value.get("provider_saw_synthetic_marker") for value in captures.values()):
        failures.append("provider_received_unmasked_marker")
    if cancellation_report.get("provider_received_unmasked_marker"):
        failures.append("cancellation_leaked_unmasked_marker")
    if cancellation_report.get("final_pii_mapping_count") != 0:
        failures.append("cancellation_mapping_did_not_expire")

    report = {
        "schema_version": 1,
        "passed": not failures,
        "failures": sorted(set(failures)),
        "observed_user_count": len(successful_users),
        "minimum_successful_requests_per_user": min(
            successful_users.values(), default=0
        ),
        "request_failure_count": summary.get("failure_count"),
        "unexpected_error_counts": dict(sorted(unexpected_error_counts.items())),
        "fault_error_counts": {
            scenario: dict(sorted(counts.items()))
            for scenario, counts in fault_error_counts.items()
        },
        "fault_error_type_counts": {
            scenario: dict(sorted(counts.items()))
            for scenario, counts in fault_error_type_counts.items()
        },
        "application_recovery": recovery,
        "affinity_transitions": transitions,
        "fault_events": events,
        "client_cancellation": cancellation_report,
        "residual_mapping_cleanup": residual_mapping_cleanup,
        "redis": final_redis,
        "provider_captures": captures,
    }
    write_json(args.results_dir / "resilience-validation.json", report)
    if failures:
        raise AssertionError(f"resilience gates failed: {sorted(set(failures))}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("inject", "cancel", "validate"))
    parser.add_argument("--base-url", default="http://127.0.0.1:14020")
    parser.add_argument("--master-key", default="sk-load-test-master")
    parser.add_argument("--compose-file", type=Path, required=True)
    parser.add_argument("--results-dir", type=Path, required=True)
    parser.add_argument("--expected-users", type=int, default=400)
    parser.add_argument(
        "--scenarios",
        default="analyzer,litellm,redis,postgres",
    )
    parser.add_argument("--initial-delay-seconds", type=float, default=430)
    parser.add_argument("--downtime-seconds", type=float, default=8)
    parser.add_argument("--between-seconds", type=float, default=150)
    parser.add_argument("--recovery-timeout-seconds", type=float, default=120)
    parser.add_argument("--user-recovery-timeout-seconds", type=float, default=1_200)
    parser.add_argument("--recovery-grace-seconds", type=float, default=15)
    parser.add_argument("--mapping-ttl-seconds", type=float, default=7_200)
    parser.add_argument("--cancellation-test-ttl-seconds", type=float, default=15)
    parser.add_argument("--cancellation-observe-timeout-seconds", type=float, default=15)
    parser.add_argument("--cancellation-immediate-timeout-seconds", type=float, default=5)
    parser.add_argument("--cancellation-expiry-grace-seconds", type=float, default=10)
    return parser


def main() -> None:
    args = build_parser().parse_args()
    args.results_dir.mkdir(parents=True, exist_ok=True)
    if args.action == "inject":
        inject(args)
    elif args.action == "cancel":
        cancel_request(args)
    else:
        validate(args)


if __name__ == "__main__":
    main()
