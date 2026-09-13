#!/usr/bin/env python3
"""Validate state isolation, affinity and key administration under load."""

from __future__ import annotations

import argparse
import concurrent.futures
import csv
import json
import re
import subprocess
import threading
import time
import urllib.error
import urllib.request
import uuid
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from load_support import PII_PLACEHOLDER, extract_output_text, parse_sse_data, stream_delta


KNOWN_DEPLOYMENTS = frozenset({"load-mock-a", "load-mock-b"})
REDIS_PATTERNS = {
    "pii_mappings": "pii_mapping:*",
    "analysis_cache_entries": "pii_analysis_cache:v1:*",
    "deployment_affinity": "deployment_affinity:v1:*",
}
REDIS_SCAN_SCRIPT = """
local cursor = '0'
local count = 0
local min_ttl = nil
local max_ttl = nil
local invalid_ttl = 0
repeat
  local result = redis.call('SCAN', cursor, 'MATCH', ARGV[1], 'COUNT', 1000)
  cursor = result[1]
  for _, key in ipairs(result[2]) do
    count = count + 1
    local ttl = redis.call('PTTL', key)
    if ttl <= 0 then invalid_ttl = invalid_ttl + 1 end
    if min_ttl == nil or ttl < min_ttl then min_ttl = ttl end
    if max_ttl == nil or ttl > max_ttl then max_ttl = ttl end
  end
until cursor == '0'
return cjson.encode({
  count=count,
  min_ttl_ms=min_ttl,
  max_ttl_ms=max_ttl,
  invalid_ttl_count=invalid_ttl
})
""".strip()
LOG_FAILURE_PATTERNS = {
    "http_500": re.compile(r'\b(?:status(?:_code)?[=: ]+|HTTP/1\.[01][" ]+)500\b', re.I),
    "http_502": re.compile(r'\b(?:status(?:_code)?[=: ]+|HTTP/1\.[01][" ]+)502\b', re.I),
    "database_deadlock": re.compile(r"deadlock detected", re.I),
    "database_connection_exhausted": re.compile(
        r"too many (?:database )?connections|remaining connection slots", re.I
    ),
}


@dataclass(frozen=True)
class HTTPResult:
    status: int
    headers: dict[str, str]
    body: bytes
    error_kind: str = ""


def write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def http_request(
    base_url: str,
    path: str,
    *,
    token: str,
    payload: dict[str, Any] | None = None,
    timeout: float = 15,
) -> HTTPResult:
    body = json.dumps(payload).encode("utf-8") if payload is not None else None
    request = urllib.request.Request(
        f"{base_url.rstrip('/')}{path}",
        data=body,
        method="POST" if payload is not None else "GET",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return HTTPResult(
                status=response.status,
                headers={key.lower(): value for key, value in response.headers.items()},
                body=response.read(),
            )
    except urllib.error.HTTPError as exc:
        return HTTPResult(
            status=exc.code,
            headers={key.lower(): value for key, value in exc.headers.items()},
            body=exc.read(),
        )
    except (OSError, TimeoutError, urllib.error.URLError) as exc:
        return HTTPResult(0, {}, b"", type(exc).__name__)


def response_text(result: HTTPResult, api: str, stream: bool) -> str:
    if stream:
        return "".join(
            stream_delta(payload, api)
            for payload in parse_sse_data(result.body.splitlines())
        )
    try:
        payload = json.loads(result.body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return ""
    return extract_output_text(payload, api)


def request_payload(
    api: str,
    marker: str,
    *,
    stream: bool,
    model: str = "mock-chat",
    trigger: str = "",
) -> dict[str, Any]:
    text = " ".join(item for item in (marker, trigger) if item)
    payload: dict[str, Any] = {"model": model, "stream": stream}
    if api == "chat":
        payload["messages"] = [{"role": "user", "content": text}]
    else:
        payload["input"] = text
    return payload


def compose_command(compose_file: Path, *args: str) -> list[str]:
    return ["docker", "compose", "-f", str(compose_file), *args]


def redis_container_id(compose_file: Path) -> str:
    result = subprocess.run(
        compose_command(compose_file, "ps", "-q", "load-redis"),
        check=True,
        capture_output=True,
        text=True,
        timeout=15,
    )
    container_id = result.stdout.strip()
    if not container_id:
        raise RuntimeError("load Redis container is not running")
    return container_id


def redis_snapshot(compose_file: Path) -> dict[str, Any]:
    container_id = redis_container_id(compose_file)
    snapshot: dict[str, Any] = {"timestamp": int(time.time())}
    for name, pattern in REDIS_PATTERNS.items():
        result = subprocess.run(
            [
                "docker",
                "exec",
                container_id,
                "redis-cli",
                "--raw",
                "EVAL",
                REDIS_SCAN_SCRIPT,
                "0",
                pattern,
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        )
        snapshot[name] = json.loads(result.stdout)
    return snapshot


def wait_for_no_mappings(compose_file: Path, timeout: float = 10) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while True:
        snapshot = redis_snapshot(compose_file)
        if snapshot["pii_mappings"]["count"] == 0:
            return snapshot
        if time.monotonic() >= deadline:
            raise AssertionError("request-scoped PII mappings were not cleaned up")
        time.sleep(0.25)


def provider_capture(compose_file: Path, service: str) -> dict[str, Any]:
    container_id = subprocess.run(
        compose_command(compose_file, "ps", "-q", service),
        check=True,
        capture_output=True,
        text=True,
        timeout=15,
    ).stdout.strip()
    result = subprocess.run(
        [
            "docker",
            "exec",
            container_id,
            "python",
            "-c",
            (
                "import urllib.request; print(urllib.request.urlopen("
                "'http://127.0.0.1:8080/capture', timeout=3).read().decode())"
            ),
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=15,
    )
    return json.loads(result.stdout)


def reset_provider_capture(compose_file: Path, service: str) -> None:
    container_id = subprocess.run(
        compose_command(compose_file, "ps", "-q", service),
        check=True,
        capture_output=True,
        text=True,
        timeout=15,
    ).stdout.strip()
    subprocess.run(
        [
            "docker",
            "exec",
            container_id,
            "python",
            "-c",
            (
                "import urllib.request; request=urllib.request.Request("
                "'http://127.0.0.1:8080/capture/reset', data=b'{}', method='POST', "
                "headers={'Content-Type':'application/json'}); "
                "urllib.request.urlopen(request, timeout=3).read()"
            ),
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=15,
    )


def safe_provider_capture(capture: dict[str, Any]) -> dict[str, Any]:
    """Keep only bounded counters and booleans from a provider capture."""
    paths = capture.get("provider_request_paths", [])
    path_counts = Counter(
        path for path in paths if isinstance(path, str) and len(path) <= 64
    )
    return {
        "deployment_id": capture.get("deployment_id", ""),
        "provider_requests": int(capture.get("provider_requests", 0)),
        "provider_request_path_counts": dict(sorted(path_counts.items())),
        "provider_saw_pii_placeholder": bool(
            capture.get("provider_saw_pii_placeholder")
        ),
        "provider_saw_synthetic_marker": bool(
            capture.get("provider_saw_synthetic_marker")
        ),
    }


def run_protocol_case(
    *,
    base_url: str,
    master_key: str,
    api: str,
    stream: bool,
    marker: str,
    trigger: str = "",
    model: str = "mock-chat",
    timeout: float = 15,
    expect_success: bool,
) -> dict[str, Any]:
    path = "/v1/chat/completions" if api == "chat" else "/v1/responses"
    result = http_request(
        base_url,
        path,
        token=master_key,
        payload=request_payload(
            api,
            marker,
            stream=stream,
            model=model,
            trigger=trigger,
        ),
        timeout=timeout,
    )
    output_text = response_text(result, api, stream)
    restored = output_text == marker
    masked_only = bool(PII_PLACEHOLDER.fullmatch(output_text))
    if expect_success:
        safe_result = restored or (api == "responses" and stream and masked_only)
        if result.status != 200 or not safe_result:
            raise AssertionError(
                f"{api} stream={stream} success path failed: status={result.status}"
            )
    elif result.status == 200 and not trigger.endswith("STREAM_DISCONNECT"):
        raise AssertionError(f"{api} failure path unexpectedly succeeded")
    return {
        "api": api,
        "stream": stream,
        "status": result.status,
        "error_kind": result.error_kind,
        "validation_result": (
            "restored" if restored else "masked_only" if masked_only else "mismatch"
        ),
        "deployment_id": result.headers.get("x-litellm-model-id", ""),
    }


def preflight(args: argparse.Namespace) -> None:
    before = wait_for_no_mappings(args.compose_file)
    for service in ("load-mock-upstream-a", "load-mock-upstream-b"):
        reset_provider_capture(args.compose_file, service)
    cases: list[dict[str, Any]] = []
    for api in ("chat", "responses"):
        for stream in (False, True):
            cases.append(
                run_protocol_case(
                    base_url=args.base_url,
                    master_key=args.master_key,
                    api=api,
                    stream=stream,
                    marker=f"state{api}{'stream' if stream else ''}@example.test",
                    expect_success=True,
                )
            )
            wait_for_no_mappings(args.compose_file)

    failure_cases = (
        ("chat", False, "LOAD_UPSTREAM_FAIL_429", "mock-chat", 15),
        ("responses", False, "LOAD_UPSTREAM_FAIL_500", "mock-chat", 15),
        ("chat", False, "LOAD_UPSTREAM_FAIL_TIMEOUT", "mock-timeout", 8),
        ("responses", True, "LOAD_UPSTREAM_FAIL_STREAM_DISCONNECT", "mock-chat", 15),
    )
    for api, stream, trigger, model, timeout in failure_cases:
        cases.append(
            run_protocol_case(
                base_url=args.base_url,
                master_key=args.master_key,
                api=api,
                stream=stream,
                marker=f"statefailure{len(cases)}@example.test",
                trigger=trigger,
                model=model,
                timeout=timeout,
                expect_success=False,
            )
        )
        wait_for_no_mappings(args.compose_file)

    captures = {
        name: safe_provider_capture(provider_capture(args.compose_file, name))
        for name in ("load-mock-upstream-a", "load-mock-upstream-b")
    }
    if any(value.get("provider_saw_synthetic_marker") for value in captures.values()):
        raise AssertionError("a mock provider received an unmasked synthetic marker")

    write_json(
        args.results_dir / "stateful-preflight.json",
        {
            "schema_version": 1,
            "before": before,
            "after": wait_for_no_mappings(args.compose_file),
            "cases": cases,
            "provider_captures": captures,
        },
    )


def churn_worker(args: argparse.Namespace, deadline: float, counters: Counter) -> None:
    while time.monotonic() < deadline:
        alias = f"load-churn-{uuid.uuid4().hex[:16]}"
        key = ""
        deleted = False
        try:
            created = http_request(
                args.base_url,
                "/key/generate",
                token=args.master_key,
                payload={
                    "key_alias": alias,
                    "models": ["mock-chat"],
                    "duration": "5m",
                    "metadata": {"purpose": "stateful-load-admin-churn"},
                },
            )
            if created.status != 200:
                counters[f"create_http_{created.status}"] += 1
                continue
            payload = json.loads(created.body.decode("utf-8"))
            key = str(payload.get("key") or payload.get("token") or "")
            if not key:
                counters["create_invalid_response"] += 1
                continue
            counters["created"] += 1

            info = http_request(
                args.base_url,
                "/v2/key/info",
                token=args.master_key,
                payload={"key_aliases": [alias]},
            )
            if info.status != 200:
                counters[f"read_http_{info.status}"] += 1
                continue
            counters["read"] += 1

            model_call = http_request(
                args.base_url,
                "/v1/chat/completions",
                token=key,
                payload={
                    "model": "mock-chat",
                    "messages": [
                        {"role": "user", "content": "Нейтральная проверка"}
                    ],
                },
            )
            if model_call.status != 200:
                counters[f"use_http_{model_call.status}"] += 1
                continue
            counters["used"] += 1

            removed = http_request(
                args.base_url,
                "/key/delete",
                token=args.master_key,
                payload={"key_aliases": [alias]},
            )
            if removed.status != 200:
                counters[f"delete_http_{removed.status}"] += 1
                continue
            deleted = True
            counters["deleted"] += 1

            revocation_deadline = time.monotonic() + args.revocation_timeout_seconds
            consecutive_denials = 0
            while time.monotonic() < revocation_deadline:
                revoked = http_request(
                    args.base_url,
                    "/models",
                    token=key,
                    timeout=5,
                )
                if revoked.status in {401, 403}:
                    consecutive_denials += 1
                    if consecutive_denials >= args.revocation_required_denials:
                        counters["revoked"] += 1
                        break
                else:
                    consecutive_denials = 0
                time.sleep(args.revocation_poll_seconds)
            else:
                counters["revocation_not_enforced"] += 1
        except (ValueError, json.JSONDecodeError):
            counters["invalid_response"] += 1
        finally:
            if key and not deleted:
                cleanup = http_request(
                    args.base_url,
                    "/key/delete",
                    token=args.master_key,
                    payload={"key_aliases": [alias]},
                )
                if cleanup.status == 200:
                    counters["cleanup_deleted"] += 1
                else:
                    counters[f"cleanup_http_{cleanup.status}"] += 1
        if args.churn_interval_seconds > 0:
            time.sleep(args.churn_interval_seconds)


def churn(args: argparse.Namespace) -> None:
    deadline = time.monotonic() + args.churn_duration_seconds
    counters: Counter[str] = Counter()
    lock = threading.Lock()

    def synchronized_worker() -> None:
        local: Counter[str] = Counter()
        churn_worker(args, deadline, local)
        with lock:
            counters.update(local)

    with concurrent.futures.ThreadPoolExecutor(
        max_workers=args.churn_concurrency
    ) as executor:
        futures = [executor.submit(synchronized_worker) for _ in range(args.churn_concurrency)]
        for future in futures:
            future.result()

    write_json(
        args.results_dir / "admin-churn.json",
        {"schema_version": 1, "counters": dict(sorted(counters.items()))},
    )
    failures = {
        name: count
        for name, count in counters.items()
        if name not in {"created", "read", "used", "deleted", "revoked", "cleanup_deleted"}
        and count
    }
    if failures or counters["created"] == 0:
        raise AssertionError(f"administrative churn failed: {failures}")


def log_failure_counts(compose_file: Path, since: str) -> dict[str, int]:
    output = subprocess.run(
        compose_command(
            compose_file,
            "logs",
            "--no-color",
            "--since",
            since,
            "load-litellm",
            "load-db",
        ),
        check=True,
        capture_output=True,
        text=True,
        timeout=60,
    ).stdout
    return {
        name: len(pattern.findall(output))
        for name, pattern in LOG_FAILURE_PATTERNS.items()
    }


def validate(args: argparse.Namespace) -> None:
    summaries = sorted(args.results_dir.glob("summary-*.json"))
    if not summaries:
        raise AssertionError("Locust summary is missing")
    summary = json.loads(summaries[0].read_text(encoding="utf-8"))
    samples = list(csv.DictReader((args.results_dir / f"samples-{summary['node']}.csv").open()))

    failures: list[str] = []
    if summary.get("failure_count") != 0:
        failures.append("request_failures")
    if summary.get("deployment_transition_count") != 0:
        failures.append("deployment_affinity_changed")
    if summary.get("validation_counts", {}).get("mismatch", 0):
        failures.append("mapping_isolation_mismatch")

    initial_by_user: dict[str, str] = {}
    successful_requests_by_user: Counter[str] = Counter()
    for sample in samples:
        if sample.get("error_kind"):
            continue
        deployment_id = sample.get("deployment_id", "")
        if deployment_id not in KNOWN_DEPLOYMENTS:
            failures.append("unknown_or_missing_deployment")
            break
        user_index = sample.get("user_index", "")
        initial_by_user.setdefault(user_index, deployment_id)
        successful_requests_by_user[user_index] += 1
    if len(initial_by_user) != args.expected_users:
        failures.append("not_all_users_observed")
    minimum_requests_per_user = min(successful_requests_by_user.values(), default=0)
    if minimum_requests_per_user < 2:
        failures.append("insufficient_requests_per_user")
    distribution = Counter(initial_by_user.values())
    if set(distribution) != KNOWN_DEPLOYMENTS:
        failures.append("deployments_not_distributed")
    elif initial_by_user:
        min_share = min(distribution.values()) / len(initial_by_user)
        if min_share < args.minimum_deployment_share:
            failures.append("deployment_distribution_unbalanced")

    final_redis = wait_for_no_mappings(args.compose_file)
    if final_redis["deployment_affinity"]["count"] < args.expected_users:
        failures.append("missing_deployment_affinity_entries")
    for name in ("analysis_cache_entries", "deployment_affinity"):
        if final_redis[name]["invalid_ttl_count"]:
            failures.append(f"invalid_ttl_{name}")

    captures = {
        name: safe_provider_capture(provider_capture(args.compose_file, name))
        for name in ("load-mock-upstream-a", "load-mock-upstream-b")
    }
    if any(value.get("provider_saw_synthetic_marker") for value in captures.values()):
        failures.append("provider_received_unmasked_marker")

    churn_report = json.loads(
        (args.results_dir / "admin-churn.json").read_text(encoding="utf-8")
    )
    churn_failures = {
        name: count
        for name, count in churn_report.get("counters", {}).items()
        if name not in {"created", "read", "used", "deleted", "revoked", "cleanup_deleted"}
        and count
    }
    if churn_failures:
        failures.append("administrative_churn_failures")

    log_counts = log_failure_counts(args.compose_file, args.logs_since)
    if any(log_counts.values()):
        failures.append("fatal_log_signals")

    report = {
        "schema_version": 1,
        "passed": not failures,
        "failures": sorted(set(failures)),
        "observed_user_count": len(initial_by_user),
        "minimum_successful_requests_per_user": minimum_requests_per_user,
        "initial_deployment_counts": dict(sorted(distribution.items())),
        "deployment_transition_count": summary.get("deployment_transition_count"),
        "redis": final_redis,
        "provider_captures": captures,
        "admin_churn_counters": churn_report.get("counters", {}),
        "fatal_log_signal_counts": log_counts,
    }
    write_json(args.results_dir / "stateful-validation.json", report)
    if failures:
        raise AssertionError(f"stateful load gates failed: {sorted(set(failures))}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("preflight", "churn", "validate"))
    parser.add_argument("--base-url", default="http://127.0.0.1:14020")
    parser.add_argument("--master-key", default="sk-load-test-master")
    parser.add_argument("--compose-file", type=Path, required=True)
    parser.add_argument("--results-dir", type=Path, required=True)
    parser.add_argument("--expected-users", type=int, default=400)
    parser.add_argument("--minimum-deployment-share", type=float, default=0.2)
    parser.add_argument("--churn-duration-seconds", type=float, default=120)
    parser.add_argument("--churn-concurrency", type=int, default=4)
    parser.add_argument("--churn-interval-seconds", type=float, default=0.5)
    parser.add_argument("--revocation-timeout-seconds", type=float, default=8)
    parser.add_argument("--revocation-poll-seconds", type=float, default=0.25)
    parser.add_argument("--revocation-required-denials", type=int, default=4)
    parser.add_argument("--logs-since", default="1m")
    return parser


def main() -> None:
    args = build_parser().parse_args()
    args.results_dir.mkdir(parents=True, exist_ok=True)
    if args.action == "preflight":
        preflight(args)
    elif args.action == "churn":
        churn(args)
    else:
        validate(args)


if __name__ == "__main__":
    main()
