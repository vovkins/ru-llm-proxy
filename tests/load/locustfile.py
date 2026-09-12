"""Locust profile for 400-user ru-llm-proxy capacity experiments."""

from __future__ import annotations

import os
import threading
import time
from pathlib import Path

import gevent
from locust import HttpUser, LoadTestShape, between, constant_pacing, events, task

from load_support import (
    ConversationState,
    KeyPool,
    PII_PLACEHOLDER,
    SafeReportWriter,
    SUPPORTED_APIS,
    SUPPORTED_CONTEXT_MODES,
    SUPPORTED_STREAM_MODES,
    boolean_env,
    choose_variant,
    extract_output_text,
    parse_context_sizes,
    parse_sse_data,
    stream_delta,
    stream_response_id,
    validate_large_context_safety,
)


PROFILE = os.getenv("LOAD_PROFILE", "smoke")
EXPECTED_USERS = int(os.getenv("LOAD_EXPECTED_USERS", "4"))
PACE_SECONDS = float(os.getenv("LOAD_PACE_SECONDS", "15"))
CONTEXT_SIZES = parse_context_sizes(os.getenv("LOAD_CONTEXT_SIZES", "1000,8000"))
API_MODE = os.getenv("LOAD_API", "mixed")
CONTEXT_MODE = os.getenv("LOAD_CONTEXT_MODE", "mixed")
STREAM_MODE = os.getenv("LOAD_STREAM", "mixed").lower()
MODEL = os.getenv("LOAD_MODEL", "mock-chat")
KEY_FILE = Path(os.getenv("LOAD_KEY_FILE", "/state/keys.json"))
KEY_SHARD_INDEX = int(os.getenv("LOAD_KEY_SHARD_INDEX", "0"))
KEY_SHARD_COUNT = int(os.getenv("LOAD_KEY_SHARD_COUNT", "1"))
CONNECT_TIMEOUT_SECONDS = float(os.getenv("LOAD_CONNECT_TIMEOUT_SECONDS", "5"))
READ_TIMEOUT_SECONDS = float(os.getenv("LOAD_READ_TIMEOUT_SECONDS", "180"))
VALIDATE_MAPPING = boolean_env("LOAD_VALIDATE_MAPPING", True)
REQUIRE_STREAM_RESTORATION = boolean_env("LOAD_REQUIRE_STREAM_RESTORATION")
ALLOW_LARGE_CONCURRENT = boolean_env("LOAD_ALLOW_LARGE_CONCURRENT")
REPORT_DIR = Path(os.getenv("LOAD_REPORT_DIR", "/results"))
REPORT_NODE = os.getenv("LOAD_REPORT_NODE", "local")

for value, allowed, label in (
    (API_MODE, SUPPORTED_APIS, "LOAD_API"),
    (CONTEXT_MODE, SUPPORTED_CONTEXT_MODES, "LOAD_CONTEXT_MODE"),
    (STREAM_MODE, SUPPORTED_STREAM_MODES, "LOAD_STREAM"),
):
    if value not in allowed:
        raise ValueError(f"{label} has unsupported value: {value}")
if PROFILE not in {"smoke", "steady", "stages", "burst", "streams", "context"}:
    raise ValueError(f"LOAD_PROFILE has unsupported value: {PROFILE}")

validate_large_context_safety(
    CONTEXT_SIZES,
    EXPECTED_USERS,
    allow_large_concurrent=ALLOW_LARGE_CONCURRENT,
)
KEY_POOL = KeyPool(
    KEY_FILE,
    shard_index=KEY_SHARD_INDEX,
    shard_count=KEY_SHARD_COUNT,
)
if KEY_POOL.size < EXPECTED_USERS:
    raise ValueError(
        f"key shard contains {KEY_POOL.size} keys, but {EXPECTED_USERS} users are expected"
    )

REPORTER: SafeReportWriter | None = None
CONTEXT_COMPLETED_USERS = 0
CONTEXT_COMPLETED_LOCK = threading.Lock()


@events.init.add_listener
def initialize_reporter(environment, **_kwargs) -> None:
    global REPORTER
    REPORTER = SafeReportWriter(
        REPORT_DIR,
        node=REPORT_NODE,
        metadata={
            "profile": PROFILE,
            "expected_users": EXPECTED_USERS,
            "pace_seconds": PACE_SECONDS,
            "api": API_MODE,
            "context_mode": CONTEXT_MODE,
            "stream": STREAM_MODE,
            "context_sizes": list(CONTEXT_SIZES),
            "model": MODEL,
            "generator_unit": "whitespace_token",
            "key_shard_index": KEY_SHARD_INDEX,
            "key_shard_count": KEY_SHARD_COUNT,
        },
    )


@events.test_stop.add_listener
def finalize_reporter(environment, **_kwargs) -> None:
    if REPORTER is not None:
        REPORTER.close()


class ProxyUser(HttpUser):
    """One independently authenticated OpenCode- or Codex-like client."""

    wait_time = constant_pacing(PACE_SECONDS) if PROFILE != "smoke" else between(1, 2)

    def on_start(self) -> None:
        self._key_lease = KEY_POOL.acquire()
        self.user_index, self.virtual_key = self._key_lease
        api = choose_variant(API_MODE, ("chat", "responses"), self.user_index)
        context_mode = choose_variant(
            CONTEXT_MODE,
            ("full-history", "one-shot", "previous-response", "encrypted-state"),
            self.user_index // 2,
        )
        if api == "chat" and context_mode in {"previous-response", "encrypted-state"}:
            context_mode = "full-history"
        stream_value = choose_variant(
            STREAM_MODE,
            ("false", "true"),
            self.user_index // 2,
        )
        if PROFILE == "streams":
            stream_value = "true"
        self.conversation = ConversationState(
            user_index=self.user_index,
            api=api,
            context_mode=context_mode,
            stream=stream_value == "true",
            sizes=CONTEXT_SIZES,
            model=MODEL,
        )
        self._single_request_sent = False
        self._request_count = 0
        self._context_completion_recorded = False
        if PROFILE in {"steady", "stages"}:
            fraction = (self.user_index * 0.61803398875) % 1
            gevent.sleep(fraction * PACE_SECONDS)

    def on_stop(self) -> None:
        if hasattr(self, "_key_lease"):
            KEY_POOL.release(self._key_lease)

    @task
    def model_request(self) -> None:
        if PROFILE in {"burst", "streams"} and self._single_request_sent:
            gevent.sleep(3600)
            return
        if PROFILE == "context" and self._request_count >= len(CONTEXT_SIZES):
            self._finish_context_profile()
            gevent.sleep(3600)
            return
        self._single_request_sent = True
        self._request_count += 1
        spec = self.conversation.next_request()
        headers = {"Authorization": f"Bearer {self.virtual_key}"}
        started_at = time.perf_counter()
        status_code: int | None = None
        ttft_ms: float | None = None
        error_kind = ""
        validation_result = "not_checked"
        response_id: str | None = None
        output_text = ""

        try:
            with self.client.post(
                spec.path,
                json=spec.payload,
                headers=headers,
                name=spec.metric_name,
                stream=spec.stream,
                catch_response=True,
                timeout=(CONNECT_TIMEOUT_SECONDS, READ_TIMEOUT_SECONDS),
            ) as response:
                status_code = response.status_code
                if response.status_code >= 400:
                    error_kind = f"http_{response.status_code}"
                    response.failure(error_kind)
                elif spec.stream:
                    chunks: list[str] = []
                    for event_payload in parse_sse_data(response.iter_lines()):
                        delta = stream_delta(event_payload, spec.api)
                        if delta:
                            if ttft_ms is None:
                                ttft_ms = (time.perf_counter() - started_at) * 1000
                            chunks.append(delta)
                        response_id = stream_response_id(event_payload, spec.api) or response_id
                    output_text = "".join(chunks)
                else:
                    payload = response.json()
                    output_text = extract_output_text(payload, spec.api)
                    value = payload.get("id") if isinstance(payload, dict) else None
                    response_id = value if isinstance(value, str) else None

                if not error_kind and VALIDATE_MAPPING:
                    if output_text == spec.expected_marker:
                        validation_result = "restored"
                    elif (
                        spec.api == "responses"
                        and spec.stream
                        and PII_PLACEHOLDER.fullmatch(output_text)
                    ):
                        validation_result = "masked_only"
                        if REQUIRE_STREAM_RESTORATION:
                            error_kind = "stream_restoration_missing"
                            response.failure(error_kind)
                    else:
                        validation_result = "mismatch"
                        error_kind = "mapping_isolation_mismatch"
                        response.failure(error_kind)
                total_ms = (time.perf_counter() - started_at) * 1000
                response.request_meta["response_time"] = total_ms
                response.request_meta["response_length"] = 0
        except Exception as exc:  # Locust records the bounded exception class separately.
            total_ms = (time.perf_counter() - started_at) * 1000
            error_kind = type(exc).__name__

        self.conversation.update_response_id(response_id)
        if REPORTER is not None:
            REPORTER.record(
                {
                    "timestamp": int(time.time()),
                    "api": spec.api,
                    "context_mode": spec.context_mode,
                    "stream": str(spec.stream).lower(),
                    "context_tokens": spec.context_tokens,
                    "status_code": status_code,
                    "total_ms": round(total_ms, 3),
                    "ttft_ms": round(ttft_ms, 3) if ttft_ms is not None else "",
                    "validation_result": validation_result,
                    "error_kind": error_kind,
                }
            )

    def _finish_context_profile(self) -> None:
        global CONTEXT_COMPLETED_USERS
        if self._context_completion_recorded:
            return
        self._context_completion_recorded = True
        with CONTEXT_COMPLETED_LOCK:
            CONTEXT_COMPLETED_USERS += 1
            all_completed = CONTEXT_COMPLETED_USERS >= EXPECTED_USERS
        if all_completed and self.environment.runner is not None:
            gevent.spawn_later(0.1, self.environment.runner.quit)


if PROFILE == "stages":

    class ProgressiveLoadShape(LoadTestShape):
        """Grow from 3.3 to 26.7 RPS at one request per user per 15 seconds."""

        stage_seconds = int(os.getenv("LOAD_STAGE_DURATION_SECONDS", "120"))
        stages = (50, 100, 200, 400)

        def tick(self) -> tuple[int, float] | None:
            stage_index = int(self.get_run_time() // self.stage_seconds)
            if stage_index >= len(self.stages):
                return None
            users = self.stages[stage_index]
            return users, float(max(10, users // 4))
