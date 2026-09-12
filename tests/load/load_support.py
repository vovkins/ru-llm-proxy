"""Pure helpers for the ru-llm-proxy Locust load profile."""

from __future__ import annotations

import csv
import json
import math
import os
import re
import threading
import time
from collections import Counter, deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable


DEFAULT_CONTEXT_SIZES = (1_000, 8_000, 50_000, 128_000, 256_000, 512_000, 1_000_000)
MAX_CONTEXT_TOKENS = 1_000_000
SAFE_LARGE_CONTEXT_USERS = 10
SUPPORTED_APIS = frozenset({"chat", "responses", "mixed"})
SUPPORTED_CONTEXT_MODES = frozenset(
    {"full-history", "one-shot", "previous-response", "encrypted-state", "mixed"}
)
SUPPORTED_STREAM_MODES = frozenset({"true", "false", "mixed"})
PII_PLACEHOLDER = re.compile(r"^<[A-Z][A-Z0-9_]*_[1-9][0-9]*>$")
REPORT_NODE_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")


def parse_context_sizes(value: str) -> tuple[int, ...]:
    """Parse a strictly increasing list of synthetic whitespace-token counts."""
    try:
        sizes = tuple(int(item.strip()) for item in value.split(",") if item.strip())
    except ValueError as exc:
        raise ValueError("context sizes must be comma-separated integers") from exc
    if not sizes or sizes[0] < 1:
        raise ValueError("context sizes must contain positive integers")
    if tuple(sorted(set(sizes))) != sizes:
        raise ValueError("context sizes must be unique and strictly increasing")
    if sizes[-1] > MAX_CONTEXT_TOKENS:
        raise ValueError(f"context size exceeds {MAX_CONTEXT_TOKENS}")
    return sizes


def validate_large_context_safety(
    sizes: tuple[int, ...], users: int, *, allow_large_concurrent: bool
) -> None:
    """Guard against accidentally allocating hundreds of million-token payloads."""
    if (
        sizes[-1] > 128_000
        and users > SAFE_LARGE_CONTEXT_USERS
        and not allow_large_concurrent
    ):
        raise ValueError(
            "contexts above 128k are limited to 10 users; set "
            "LOAD_ALLOW_LARGE_CONCURRENT=true only for an intentional high-memory run"
        )


def choose_variant(configured: str, options: tuple[str, ...], user_index: int) -> str:
    """Choose a deterministic per-user variant for a mixed profile."""
    return options[user_index % len(options)] if configured == "mixed" else configured


def context_bucket(token_count: int) -> str:
    if token_count >= 1_000_000:
        return "1m"
    if token_count >= 1_000:
        return f"{token_count // 1_000}k"
    return str(token_count)


def synthetic_text(token_count: int, *, marker: str, seed: int) -> str:
    """Create exactly token_count whitespace units with one per-user PII marker."""
    if token_count < 1:
        return ""
    neutral = ("контекст", "пример", "данные")[seed % 3]
    if token_count == 1:
        return marker
    return f"{marker} {(neutral + ' ') * (token_count - 2)}{neutral}"


def extract_output_text(payload: object, api: str) -> str:
    """Extract text only for response validation; callers must never persist it."""
    if not isinstance(payload, dict):
        return ""
    if api == "chat":
        choices = payload.get("choices")
        if not isinstance(choices, list) or not choices:
            return ""
        choice = choices[0]
        if not isinstance(choice, dict):
            return ""
        message = choice.get("message")
        return message.get("content", "") if isinstance(message, dict) else ""

    output = payload.get("output")
    if not isinstance(output, list):
        return ""
    chunks: list[str] = []
    for item in output:
        if not isinstance(item, dict) or not isinstance(item.get("content"), list):
            continue
        for block in item["content"]:
            if isinstance(block, dict) and isinstance(block.get("text"), str):
                chunks.append(block["text"])
    return "".join(chunks)


def parse_sse_data(lines: Iterable[bytes | str]) -> Iterable[dict[str, Any]]:
    """Yield JSON payloads from OpenAI-compatible SSE data lines."""
    for raw_line in lines:
        line = raw_line.decode("utf-8") if isinstance(raw_line, bytes) else raw_line
        if not line.startswith("data:"):
            continue
        data = line[5:].strip()
        if not data or data == "[DONE]":
            continue
        try:
            value = json.loads(data)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            yield value


def stream_delta(payload: dict[str, Any], api: str) -> str:
    if api == "chat":
        choices = payload.get("choices")
        if not isinstance(choices, list) or not choices:
            return ""
        choice = choices[0]
        delta = choice.get("delta") if isinstance(choice, dict) else None
        return delta.get("content", "") if isinstance(delta, dict) else ""
    value = payload.get("delta")
    if payload.get("type") == "response.output_text.delta" and isinstance(value, str):
        return value
    return ""


def stream_response_id(payload: dict[str, Any], api: str) -> str | None:
    if api != "responses" or payload.get("type") != "response.completed":
        return None
    response = payload.get("response")
    value = response.get("id") if isinstance(response, dict) else None
    return value if isinstance(value, str) else None


@dataclass(frozen=True)
class RequestSpec:
    api: str
    context_mode: str
    stream: bool
    context_tokens: int
    path: str
    payload: dict[str, Any]
    expected_marker: str

    @property
    def metric_name(self) -> str:
        transport = "stream" if self.stream else "nonstream"
        return f"/{self.api}/{transport}/{self.context_mode}/{context_bucket(self.context_tokens)}"


class ConversationState:
    """Build deterministic OpenCode and Codex request sequences for one user."""

    def __init__(
        self,
        *,
        user_index: int,
        api: str,
        context_mode: str,
        stream: bool,
        sizes: tuple[int, ...],
        model: str,
    ) -> None:
        self.user_index = user_index
        self.api = api
        self.context_mode = context_mode
        self.stream = stream
        self.sizes = sizes
        self.model = model
        self.marker = f"loaduser{user_index:04d}@example.test"
        self._step = 0
        self._history: list[dict[str, Any]] = []
        self._previous_response_id: str | None = None

    def update_response_id(self, response_id: str | None) -> None:
        if response_id:
            self._previous_response_id = response_id

    def next_request(self) -> RequestSpec:
        if self._step >= len(self.sizes):
            self._step = 0
            self._history = []
            self._previous_response_id = None

        size = self.sizes[self._step]
        previous_size = self.sizes[self._step - 1] if self._step else 0
        delta_size = size - previous_size
        if self.context_mode == "one-shot":
            text = synthetic_text(size, marker=self.marker, seed=self._step)
        else:
            text = synthetic_text(delta_size, marker=self.marker, seed=self._step)

        payload: dict[str, Any] = {"model": self.model, "stream": self.stream}
        if self.api == "chat":
            message = {"role": "user", "content": text}
            if self.context_mode == "full-history":
                self._history.append(message)
                payload["messages"] = list(self._history)
            else:
                payload["messages"] = [message]
            path = "/v1/chat/completions"
        else:
            item: dict[str, Any] = {
                "role": "user",
                "content": [{"type": "input_text", "text": text}],
            }
            if self.context_mode == "full-history":
                self._history.append(item)
                payload["input"] = list(self._history)
            else:
                payload["input"] = [item]
            if self.context_mode in {"previous-response", "encrypted-state"}:
                if self._previous_response_id:
                    payload["previous_response_id"] = self._previous_response_id
            if self.context_mode == "encrypted-state" and self._step:
                payload["input"].insert(
                    0,
                    {
                        "type": "reasoning",
                        "id": f"reasoning-{self.user_index}-{self._step}",
                        "encrypted_content": "opaque-provider-state-not-user-content",
                        "summary": [],
                    },
                )
            path = "/v1/responses"

        self._step += 1
        return RequestSpec(
            api=self.api,
            context_mode=self.context_mode,
            stream=self.stream,
            context_tokens=size,
            path=path,
            payload=payload,
            expected_marker=self.marker,
        )


class KeyPool:
    """Allocate one temporary LiteLLM virtual key to each active Locust user."""

    def __init__(
        self,
        path: Path,
        *,
        shard_index: int = 0,
        shard_count: int = 1,
    ) -> None:
        payload = json.loads(path.read_text(encoding="utf-8"))
        keys = payload.get("keys") if isinstance(payload, dict) else payload
        if not isinstance(keys, list) or not all(isinstance(key, str) for key in keys):
            raise ValueError("load key file must contain a JSON keys list")
        if shard_count < 1 or shard_index < 0 or shard_index >= shard_count:
            raise ValueError("invalid key shard")
        selected = [
            (index, key)
            for index, key in enumerate(keys)
            if index % shard_count == shard_index
        ]
        if not selected:
            raise ValueError("selected key shard is empty")
        self._available = deque(selected)
        self._lock = threading.Lock()

    @property
    def size(self) -> int:
        with self._lock:
            return len(self._available)

    def acquire(self) -> tuple[int, str]:
        with self._lock:
            if not self._available:
                raise RuntimeError("no unused virtual keys remain in this load-generator shard")
            return self._available.popleft()

    def release(self, value: tuple[int, str]) -> None:
        with self._lock:
            self._available.append(value)


def percentile(values: list[float], quantile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = max(0, math.ceil(quantile * len(ordered)) - 1)
    return round(ordered[index], 3)


class SafeReportWriter:
    """Write bounded metrics without request, response, key or PII values."""

    FIELDNAMES = (
        "timestamp",
        "api",
        "context_mode",
        "stream",
        "context_tokens",
        "status_code",
        "total_ms",
        "ttft_ms",
        "validation_result",
        "error_kind",
    )

    def __init__(self, output_dir: Path, *, node: str, metadata: dict[str, Any]) -> None:
        if REPORT_NODE_PATTERN.fullmatch(node) is None:
            raise ValueError("report node must contain only safe filename characters")
        output_dir.mkdir(parents=True, exist_ok=True)
        self.output_dir = output_dir
        self.node = node
        self.metadata = metadata
        self.path = output_dir / f"samples-{node}.csv"
        self._file = self.path.open("w", encoding="utf-8", newline="")
        self._writer = csv.DictWriter(self._file, fieldnames=self.FIELDNAMES)
        self._writer.writeheader()
        self._lock = threading.Lock()
        self._samples: list[dict[str, Any]] = []
        self._started = time.time()

    def record(self, sample: dict[str, Any]) -> None:
        bounded = {name: sample.get(name) for name in self.FIELDNAMES}
        with self._lock:
            self._writer.writerow(bounded)
            self._file.flush()
            self._samples.append(bounded)

    def close(self) -> dict[str, Any]:
        with self._lock:
            if self._file.closed:
                return {}
            self._file.close()
            elapsed = max(time.time() - self._started, 0.001)
            samples = list(self._samples)

        successful = [item for item in samples if not item.get("error_kind")]
        latencies = [float(item["total_ms"]) for item in successful]
        ttft = [
            float(item["ttft_ms"])
            for item in successful
            if item.get("ttft_ms") not in (None, "")
        ]
        status_counts = Counter(str(item.get("status_code") or "client_error") for item in samples)
        validation_counts = Counter(
            str(item.get("validation_result") or "not_checked") for item in samples
        )
        context_units = sum(int(item.get("context_tokens") or 0) for item in samples)
        summary = {
            "schema_version": 1,
            "node": self.node,
            "parameters": self.metadata,
            "duration_seconds": round(elapsed, 3),
            "request_count": len(samples),
            "success_count": len(successful),
            "failure_count": len(samples) - len(successful),
            "requests_per_second": round(len(samples) / elapsed, 3),
            "synthetic_input_units_per_second": round(context_units / elapsed, 3),
            "status_counts": dict(sorted(status_counts.items())),
            "validation_counts": dict(sorted(validation_counts.items())),
            "latency_ms": {
                "p50": percentile(latencies, 0.50),
                "p95": percentile(latencies, 0.95),
                "p99": percentile(latencies, 0.99),
            },
            "ttft_ms": {
                "p50": percentile(ttft, 0.50),
                "p95": percentile(ttft, 0.95),
                "p99": percentile(ttft, 0.99),
            },
        }
        summary_path = self.output_dir / f"summary-{self.node}.json"
        summary_path.write_text(
            json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        return summary


def boolean_env(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}
