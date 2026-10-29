"""Bounded open-loop burst measurements for disposable mock-provider stands."""
from __future__ import annotations

import asyncio
from collections import Counter
import json
import time

import httpx

from load_support import bounded_error_token, extract_output_text, percentile, stream_delta


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
        "latency_seconds_all": latencies(rows),
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
