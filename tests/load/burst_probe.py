"""Bounded open-loop burst measurements for disposable mock-provider stands."""
from __future__ import annotations

import argparse
import asyncio
from collections import Counter
import json
import math
import os
from pathlib import Path
import time

import httpx

from load_support import KeyPool, bounded_error_token, extract_output_text, parse_context_sizes, percentile, stream_delta
from ner_capacity_probe import test_text


def build_wave(keys, *, count, sizes=(1000,), spread_seconds=2, api="mixed",
               stream=True, fresh=True, history_units=0, serial_start=0):
    """Synthetic subagent inputs; sizes are whitespace units, not LLM tokens."""
    if (not isinstance(keys, (list, tuple)) or not keys or len(keys) > 256
            or any(not isinstance(key, str) or not key for key in keys)
            or type(count) is not int or not 1 <= count <= 256
            or not sizes or any(type(size) is not int or size < 1 or size > 1_000_000 for size in sizes)
            or type(history_units) is not int or not 0 <= history_units < min(sizes)
            or type(serial_start) is not int or not 0 <= serial_start <= 1_000_000_000
            or api not in {"chat", "responses", "mixed"} or type(stream) is not bool
            or type(fresh) is not bool or type(spread_seconds) not in (int, float)
            or not math.isfinite(spread_seconds) or spread_seconds < 0):
        raise ValueError("Invalid bounded burst profile")
    if sum(sizes[index % len(sizes)] for index in range(count)) > 4_000_000:
        raise ValueError("Burst exceeds bounded local research limits")
    history = ("контекст " * history_units).rstrip()
    specs = []
    for index in range(count):
        owner = index % len(keys)
        units = sizes[index % len(sizes)]
        kind = ("chat" if index % 2 == 0 else "responses") if api == "mixed" else api
        marker = f"loaduser{owner:04d}-wave{serial_start:010d}-request{index:03d}@example.test"
        text = test_text(units - history_units, serial_start + index, fresh=fresh, marker=marker)
        messages = ([{"role": "user", "content": history}] if history_units else [])
        messages.append({"role": "user", "content": text})
        payload = {"model": "mock-chat" if kind == "chat" else "mock-responses", "stream": stream}
        payload.update({"messages": messages} if kind == "chat" else
                       {"input": messages if history_units else text})
        specs.append({"owner_index": owner, "key": keys[owner], "units": units,
                      "history_units": history_units, "fresh_segments": fresh,
                      "offset": spread_seconds * index / (count - 1) if count > 1 else 0,
                      "api": kind, "stream": stream, "payload": payload, "expected_marker": marker})
    return specs


def build_multifield_wave(keys, *, count, sizes=(8000,), serial_start=0,
                         shared_history_serial=None, spread_seconds=2):
    """Four text fields, including tool arguments/output; echo the history marker."""
    if shared_history_serial is not None and (
            type(shared_history_serial) is not int or not 0 <= shared_history_serial <= 1_000_000_000):
        raise ValueError("Invalid history serial")
    if not sizes or any(type(size) is not int or size < 16 for size in sizes):
        raise ValueError("Multi-field history requires at least 16 units")
    specs = build_wave(keys, count=count, sizes=sizes, serial_start=serial_start,
                       spread_seconds=spread_seconds)
    history_serial = serial_start if shared_history_serial is None else shared_history_serial
    for index, spec in enumerate(specs):
        marker = f"loaduser{spec['owner_index']:04d}-wave{history_serial:010d}-request{index:03d}@example.test"
        history_part = spec["units"] // 4
        fields = [test_text(history_part, history_serial + index * 16 + field,
                            fresh=True, marker=marker) for field in range(3)]
        current = test_text(spec["units"] - 3 * history_part, serial_start + index,
                            fresh=True, marker=spec["expected_marker"])
        arguments = json.dumps({"report": fields[1]})
        payload = spec["payload"]
        if spec["api"] == "chat":
            payload["messages"] = [
                {"role": "user", "content": fields[0]},
                {"role": "assistant", "content": None, "tool_calls": [{"id": "call_load",
                    "type": "function", "function": {"name": "inspect", "arguments": arguments}}]},
                {"role": "tool", "tool_call_id": "call_load", "content": fields[2]},
                {"role": "user", "content": current},
            ]
        else:
            payload["input"] = [
                {"role": "user", "content": fields[0]},
                {"type": "function_call", "call_id": "call_load", "name": "inspect", "arguments": arguments},
                {"type": "function_call_output", "call_id": "call_load", "output": fields[2]},
                {"role": "user", "content": current},
            ]
        spec.update(expected_marker=marker, history_units=3 * history_part,
                    text_fields=4, history_reused=shared_history_serial is not None)
    return specs


def error_fields(payload, headers=None):
    """Retain error identifiers, never arbitrary messages or response content."""
    result = {}
    stack = [(payload, 0)]
    visited = 0
    while stack and visited < 128:
        value, depth = stack.pop()
        visited += 1
        if not isinstance(value, dict) or depth > 6:
            continue
        for key in ("code", "type", "reason", "phase", "failure_class"):
            if key not in result and value.get(key) is not None:
                result[key] = bounded_error_token(value[key])
        if isinstance(value.get("retryable"), bool):
            result["retryable"] = value["retryable"]
        message = value.get("message")
        if isinstance(message, str):
            for text, family in (
                ("PII guardrail required analyzer unavailable", "analyzer_unavailable"),
                ("PII guardrail analyzer overloaded", "analyzer_overloaded"),
                ("PII preprocessing time budget exceeded", "preprocessing_timeout"),
            ):
                if text in message:
                    result["message_family"] = family
        for child in value.values():
            if isinstance(child, dict):
                stack.append((child, depth + 1))
            elif isinstance(child, str) and child.startswith("{") and len(child) < 8192:
                try:
                    stack.append((json.loads(child), depth + 1))
                except ValueError:
                    pass
    retry = (headers or {}).get("Retry-After")
    if retry is not None:
        try:
            result["retry_after_seconds"] = min(3600, max(0, int(retry)))
        except (TypeError, ValueError):
            result["retry_after_invalid"] = True
    return result


def summarize(rows, elapsed):
    successful = [row for row in rows if row["outcome"] == "success"]
    failed = [row for row in rows if row["outcome"] not in ("success", "cancelled")]
    def latencies(items):
        return {f"p{p}": percentile([r["seconds"] for r in items], p / 100)
                for p in (50, 95, 99)}
    return {
        "scheduled": len(rows), "completed": len(rows),
        "started": sum(row["started"] for row in rows),
        "successful": len(successful),
        "cancelled": sum(r["outcome"] == "cancelled" for r in rows),
        "failed": sum(r["outcome"] not in ("success", "cancelled") for r in rows),
        "outcome_counts": dict(Counter(r["outcome"] for r in rows)),
        "http_statuses": dict(Counter(str(r.get("http_status")) for r in rows)),
        "error_codes": dict(Counter(r.get("code", "") for r in rows if r["outcome"] == "error")),
        "error_reasons": dict(Counter(r.get("reason", "") for r in rows if r["outcome"] == "error")),
        "message_families": dict(Counter(r.get("message_family", "") for r in rows if r["outcome"] == "error")),
        "elapsed_seconds": elapsed,
        "successful_rps": len(successful) / elapsed if elapsed else None,
        "latency_seconds_success": latencies(successful),
        "latency_seconds_failure": latencies(failed),
        "latency_seconds_all": latencies(rows),
        "failure_classes": dict(Counter(r.get("failure_class", "") for r in failed)),
        "error_phases": dict(Counter(r.get("phase", "") for r in failed)),
        "sse_first_text_seconds": {f"p{p}": percentile(
            [r["first_text_seconds"] for r in rows if "first_text_seconds" in r], p / 100)
            for p in (50, 95, 99)},
        "successful_sse_within_60_seconds": sum(
            r.get("stream", False) and r.get("first_text_seconds", math.inf) <= 60 for r in successful),
        "sse_without_text": sum(r.get("stream", False) and "first_text_seconds" not in r for r in rows),
        "max_client_start_delay_seconds": max((r.get("start_delay_seconds", 0) for r in rows), default=0),
        "within_60_seconds": sum(r["seconds"] <= 60 for r in successful),
        "arrival_model": "independent_scheduled_offsets",
        "rows": rows,
    }


async def run_wave(url, specs, *, timeout=90, cancel_indices=(), cancel_after=None,
                   transport=None):
    """Specs/keys/bodies stay in memory; only explicitly constructed rows escape."""
    if not 1 <= len(specs) <= 256 or sum(s["units"] for s in specs) > 4_000_000:
        raise ValueError("Burst exceeds bounded local research limits")
    if any(s["offset"] < 0 or s["units"] < 1 for s in specs):
        raise ValueError("Offsets and sizes must be nonnegative/positive")
    epoch = time.perf_counter()
    async with httpx.AsyncClient(timeout=timeout, trust_env=False, transport=transport,
                                 limits=httpx.Limits(max_connections=256, max_keepalive_connections=64)) as client:
        async def one(index, spec):
            row = {"index": index, "owner_index": spec["owner_index"], "units": spec["units"],
                   "api": spec["api"], "stream": spec["stream"], "offset": spec["offset"],
                   "started": False, "outcome": "error"}
            if type(spec.get("history_units")) is int and 0 <= spec["history_units"] <= spec["units"]:
                row["history_units"] = spec["history_units"]
            if type(spec.get("fresh_segments")) is bool:
                row["fresh_segments"] = spec["fresh_segments"]
            if type(spec.get("text_fields")) is int:
                row["text_fields"] = spec["text_fields"]
            if type(spec.get("history_reused")) is bool:
                row["history_reused"] = spec["history_reused"]
            started = None
            try:
                await asyncio.sleep(max(0, epoch + spec["offset"] - time.perf_counter()))
                started = time.perf_counter()
                row.update(started=True, start_delay_seconds=max(0, started - epoch - spec["offset"]),
                           actual_start_offset=started - epoch)
                path = "/v1/responses" if spec["api"] == "responses" else "/v1/chat/completions"
                output, terminal, event_error = "", False, None
                async with client.stream("POST", url + path, json=spec["payload"],
                                          headers={"Authorization": "Bearer " + spec["key"]}) as response:
                    row["http_status"] = response.status_code
                    if response.status_code != 200:
                        await response.aread()
                        try:
                            payload = response.json()
                        except ValueError:
                            payload = {}
                        row.update(error_fields(payload, response.headers))
                        return row
                    if spec["stream"]:
                        async for line in response.aiter_lines():
                            if not line.startswith("data:"):
                                continue
                            data = line[5:].strip()
                            if data == "[DONE]":
                                terminal = True
                                continue
                            try:
                                event = json.loads(data)
                            except ValueError:
                                continue
                            if not isinstance(event, dict):
                                continue
                            if event.get("error") or event.get("type") in ("error", "response.failed", "response.incomplete"):
                                event_error = error_fields(event, response.headers)
                            delta = stream_delta(event, spec["api"])
                            if delta:
                                row.setdefault("first_text_seconds", time.perf_counter() - started)
                                output += delta
                            if event.get("type") == "response.completed":
                                terminal = True
                            if len(output) > 65536:
                                raise ValueError("Unexpectedly large mock response")
                    else:
                        await response.aread()
                        payload = response.json()
                        output = extract_output_text(payload, spec["api"])
                        terminal = not payload.get("error")
                        if not terminal:
                            event_error = error_fields(payload, response.headers)
                if event_error is not None:
                    row.update(event_error)
                elif terminal and output == spec["expected_marker"]:
                    row.update(outcome="success", restored=True)
                else:
                    row.update(outcome="restoration_or_protocol_mismatch", restored=False,
                               terminal_seen=terminal)
            except asyncio.CancelledError:
                row["outcome"] = "cancelled"
            except (httpx.HTTPError, ValueError) as error:
                row.update(outcome="transport_error", error_type=type(error).__name__)
            finally:
                row["seconds"] = time.perf_counter() - (started if started is not None else epoch)
                row["completed_offset"] = time.perf_counter() - epoch
            return row

        tasks = [asyncio.create_task(one(i, spec)) for i, spec in enumerate(specs)]
        async def cancel():
            await asyncio.sleep(cancel_after)
            for index in cancel_indices:
                tasks[index].cancel()
        canceller = asyncio.create_task(cancel()) if cancel_after is not None else None
        try:
            rows = await asyncio.gather(*tasks)
        finally:
            if canceller is not None:
                canceller.cancel()
                await asyncio.gather(canceller, return_exceptions=True)
    return summarize(rows, time.perf_counter() - epoch)


def main(argv=None, *, transport=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--allow-load", action="store_true", required=True,
                        help="Acknowledge load against a disposable mock-provider stand")
    parser.add_argument("--url", required=True)
    parser.add_argument("--keys-file", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--requests", type=int, default=16)
    parser.add_argument("--units", default="1000", help="Comma-separated synthetic whitespace-unit sizes")
    parser.add_argument("--spread-seconds", type=float, default=2)
    parser.add_argument("--history-units", type=int, default=0)
    parser.add_argument("--api", choices=("chat", "responses", "mixed"), default="mixed")
    parser.add_argument("--non-stream", action="store_true")
    parser.add_argument("--shared-key", action="store_true")
    parser.add_argument("--repeat-input", action="store_true")
    parser.add_argument("--serial-start", type=int, default=0)
    parser.add_argument("--timeout", type=float, default=120)
    args = parser.parse_args(argv)
    try:
        url = httpx.URL(args.url)
        if url.scheme not in {"http", "https"} or not url.host or url.userinfo or url.query or url.fragment:
            raise ValueError
    except (httpx.InvalidURL, ValueError):
        parser.error("Target must be an HTTP(S) URL without credentials, query or fragment")
    if not math.isfinite(args.timeout) or not 0 < args.timeout <= 1500:
        parser.error("Timeout must be finite and within (0, 1500] seconds")
    if args.output.exists():
        parser.error("Refusing to overwrite an existing report or key file")
    try:
        if args.keys_file.stat().st_mode & 0o077:
            parser.error("Key file must have owner-only permissions (0600)")
        pool = KeyPool(args.keys_file)
        keys = [pool.acquire()[1] for _ in range(1 if args.shared_key else pool.size)]
        sizes = parse_context_sizes(args.units)
        specs = build_wave(keys, count=args.requests, sizes=sizes,
                           spread_seconds=args.spread_seconds, history_units=args.history_units,
                           api=args.api, stream=not args.non_stream, fresh=not args.repeat_input,
                           serial_start=args.serial_start)
    except (OSError, ValueError):
        parser.error("Invalid key file or bounded load profile; values omitted")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    # Refuse overwrite and set permissions before the first byte is written.
    with os.fdopen(os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w") as output:
        result = asyncio.run(run_wave(str(url).rstrip("/"), specs, timeout=args.timeout, transport=transport))
        result["profile"] = {"sizes": sizes, "generator_unit": "whitespace_token",
                             "owner_count": len({spec["owner_index"] for spec in specs}),
                             "history_units": args.history_units,
                             "fresh_segments": not args.repeat_input, "stream": not args.non_stream}
        output.write(json.dumps(result, indent=2) + "\n")
    print(json.dumps({name: result[name] for name in ("scheduled", "successful", "failed", "cancelled")}))
    return 0 if result["successful"] == result["scheduled"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
