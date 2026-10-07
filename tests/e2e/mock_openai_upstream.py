"""Tiny mock upstream for pre-egress proxy smoke tests."""

from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
import re
import select
import socket
import threading
import time


RAW_PHONE = "+79031234567"
PHONE_PLACEHOLDER = "<PHONE_NUMBER_1>"
PRIVATE_KEY_MARKER = "-----BEGIN PRIVATE KEY-----"
CANARIES = tuple(
    token.strip()
    for token in re.split(r"[\n,]", os.getenv("FINAL_PAYLOAD_LEAK_CHECK_CANARIES", ""))
    if token.strip()
)
ECHO_CHAT_CONTENT = os.getenv("MOCK_ECHO_CHAT_CONTENT", "false").lower() in {
    "1",
    "true",
    "yes",
}
ECHO_RESPONSES_CONTENT = os.getenv(
    "MOCK_ECHO_RESPONSES_CONTENT",
    "false",
).lower() in {
    "1",
    "true",
    "yes",
}
ECHO_FIRST_PII_PLACEHOLDER = os.getenv(
    "MOCK_ECHO_FIRST_PII_PLACEHOLDER",
    "false",
).lower() in {"1", "true", "yes"}
RESPONSE_DELAY_SECONDS = float(os.getenv("MOCK_RESPONSE_DELAY_SECONDS", "0"))
STREAM_HOLD_SECONDS = float(os.getenv("MOCK_STREAM_HOLD_SECONDS", "0"))
FAILURE_DELAY_SECONDS = float(os.getenv("MOCK_FAILURE_DELAY_SECONDS", "3"))
DEPLOYMENT_ID = os.getenv("MOCK_DEPLOYMENT_ID", "load-mock-default")
PII_PLACEHOLDER_PATTERN = re.compile(r"<[A-Z][A-Z0-9_]*_[1-9][0-9]*>")
SYNTHETIC_MARKER_PATTERN = re.compile(
    r"(?:loaduser[0-9]{4}|state(?:chat|responses|failure|timeout|stream|cancel))[a-z0-9-]*@example\.test"
)
ANALYZER_SIGNATURE = "0" * 64

CAPTURE = {
    "analyzer_requests": 0,
    "provider_requests": 0,
    "provider_request_paths": [],
    "analyzer_saw_canary": False,
    "provider_saw_canary": False,
    "provider_saw_private_key_marker": False,
    "provider_saw_raw_phone": False,
    "provider_saw_phone_placeholder": False,
    "provider_saw_pii_placeholder": False,
    "provider_saw_synthetic_marker": False,
}
CAPTURE_LOCK = threading.Lock()
CONTROL_PATTERN = re.compile(r"DISCONNECT_CASE_([a-f0-9]{16})")
CONTROL_REQUESTS = {}
CONTROL_RELEASES = {}
CONTROL_ONCE_RELEASES = set()


def _control_id(payload):
    marker = _first_match(payload, CONTROL_PATTERN)
    return marker.rsplit("_", 1)[-1] if marker else None
ANALYZER_OVERLOAD = {
    "reason": None,
    "retry_after_seconds": 1,
}


def _iter_strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, list):
        for item in value:
            yield from _iter_strings(item)
    elif isinstance(value, dict):
        for key, item in value.items():
            if isinstance(key, str):
                yield key
            yield from _iter_strings(item)


def _text_contains(value, needle: str) -> bool:
    return any(needle in text for text in _iter_strings(value))


def _text_contains_canary(value) -> bool:
    return any(_text_contains(value, canary) for canary in CANARIES)


def _text_matches(value, pattern: re.Pattern) -> bool:
    return any(pattern.search(text) is not None for text in _iter_strings(value))


def _first_match(value, pattern: re.Pattern) -> str | None:
    for text in _iter_strings(value):
        match = pattern.search(text)
        if match is not None:
            return match.group(0)
    return None


def _chat_response_content(payload) -> str:
    if ECHO_FIRST_PII_PLACEHOLDER:
        return _first_match(payload, PII_PLACEHOLDER_PATTERN) or "ok"
    if not ECHO_CHAT_CONTENT:
        return "ok"
    messages = payload.get("messages")
    if not isinstance(messages, list):
        return "ok"
    for message in reversed(messages):
        if not isinstance(message, dict):
            continue
        content = message.get("content")
        if isinstance(content, str):
            return content
    return "ok"


def _responses_response_content(payload) -> str:
    if ECHO_FIRST_PII_PLACEHOLDER:
        return _first_match(payload, PII_PLACEHOLDER_PATTERN) or "ok"
    if not ECHO_RESPONSES_CONTENT:
        return "ok"

    input_items = payload.get("input")
    if isinstance(input_items, str):
        return input_items
    if not isinstance(input_items, list):
        return "ok"

    for item in reversed(input_items):
        if not isinstance(item, dict):
            continue
        content = item.get("content")
        if isinstance(content, str):
            return content
        if not isinstance(content, list):
            continue
        for block in reversed(content):
            if not isinstance(block, dict):
                continue
            text = block.get("text")
            if isinstance(text, str):
                return text
    return "ok"


def _analyzer_entities(payload):
    text = payload.get("text")
    if not isinstance(text, str):
        return []
    if RAW_PHONE not in text:
        return []
    start = text.index(RAW_PHONE)
    return [
        {
            "entity_type": "PHONE_NUMBER",
            "start": start,
            "end": start + len(RAW_PHONE),
            "score": 1.0,
        }
    ]


def _record_provider_payload(path, payload):
    saw_canary = _text_contains_canary(payload)
    saw_private_key = _text_contains(payload, PRIVATE_KEY_MARKER)
    saw_raw_phone = _text_contains(payload, RAW_PHONE)
    saw_phone_placeholder = _text_contains(payload, PHONE_PLACEHOLDER)
    saw_pii_placeholder = _text_matches(payload, PII_PLACEHOLDER_PATTERN)
    saw_synthetic_marker = _text_matches(payload, SYNTHETIC_MARKER_PATTERN)
    with CAPTURE_LOCK:
        CAPTURE["provider_requests"] += 1
        CAPTURE["provider_request_paths"].append(path)
        CAPTURE["provider_saw_canary"] |= saw_canary
        CAPTURE["provider_saw_private_key_marker"] |= saw_private_key
        CAPTURE["provider_saw_raw_phone"] |= saw_raw_phone
        CAPTURE["provider_saw_phone_placeholder"] |= saw_phone_placeholder
        CAPTURE["provider_saw_pii_placeholder"] |= saw_pii_placeholder
        CAPTURE["provider_saw_synthetic_marker"] |= saw_synthetic_marker


def _failure_mode(payload) -> str | None:
    for mode in ("429", "500", "timeout", "stream_disconnect"):
        if _text_contains(payload, f"LOAD_UPSTREAM_FAIL_{mode.upper()}"):
            return mode
    return None


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, format, *args):
        return

    def _read_json(self):
        length = int(self.headers.get("content-length", "0") or "0")
        if length <= 0:
            return {}
        raw = self.rfile.read(length)
        try:
            return json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError:
            return {}

    def _write_json(self, status, payload, headers=None):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        for name, value in (headers or {}).items():
            self.send_header(name, str(value))
        self.end_headers()
        self.wfile.write(body)

    def _controlled_wait(self, payload, phase="provider"):
        case_id = _control_id(payload)
        if case_id is None:
            return True
        key = f"{phase}:{case_id}"
        with CAPTURE_LOCK:
            release = CONTROL_RELEASES.setdefault(key, threading.Event())
            state = CONTROL_REQUESTS.setdefault(key, {"attempts": 0})
            state.update(
                attempts=state["attempts"] + 1,
                accepted_at=time.monotonic(),
                closed_at=None,
                released_at=None,
                saw_raw_phone=_text_contains(payload, RAW_PHONE),
                saw_placeholder=_text_matches(payload, PII_PLACEHOLDER_PATTERN),
            )
        # Watch EOF while a deterministic test holds the response, not a sleep.
        deadline = time.monotonic() + 45
        while not release.is_set() and time.monotonic() < deadline:
            readable, _, _ = select.select([self.connection], [], [], 0.01)
            if readable:
                try:
                    disconnected = not self.connection.recv(1, socket.MSG_PEEK)
                except OSError:
                    disconnected = True
                if disconnected:
                    with CAPTURE_LOCK:
                        state["closed_at"] = time.monotonic()
                    self.close_connection = True
                    return False
        with CAPTURE_LOCK:
            if key in CONTROL_ONCE_RELEASES:
                release.clear()
                CONTROL_ONCE_RELEASES.remove(key)
            state["released_at"] = time.monotonic()
        return True

    def _write_sse(self, events, *, hold_after_first=0.0):
        self.send_response(200)
        self.send_header("content-type", "text/event-stream")
        self.send_header("cache-control", "no-cache")
        self.send_header("connection", "close")
        self.end_headers()
        for index, (event_name, payload) in enumerate(events):
            if event_name:
                self.wfile.write(f"event: {event_name}\n".encode("utf-8"))
            data = payload if isinstance(payload, str) else json.dumps(payload)
            self.wfile.write(f"data: {data}\n\n".encode("utf-8"))
            self.wfile.flush()
            if index == 0 and hold_after_first > 0:
                time.sleep(hold_after_first)
        self.close_connection = True

    def do_GET(self):
        if self.path == "/control/requests":
            with CAPTURE_LOCK:
                states = {key: dict(value) for key, value in CONTROL_REQUESTS.items()}
            self._write_json(200, states)
            return
        if self.path == "/health":
            self._write_json(200, {"status": "ok"})
            return
        if self.path == "/api/v1/health":
            self._write_json(
                200,
                {
                    "status": "ok",
                    "ner_state": "ready",
                    "analysis_signature": ANALYZER_SIGNATURE,
                },
            )
            return
        if self.path == "/capture":
            with CAPTURE_LOCK:
                capture = {
                    **CAPTURE,
                    "provider_request_paths": list(CAPTURE["provider_request_paths"]),
                }
            self._write_json(200, {**capture, "deployment_id": DEPLOYMENT_ID})
            return
        self._write_json(404, {"error": "not found"})

    def do_POST(self):
        if self.path == "/control/release":
            payload = self._read_json()
            case_id = payload.get("id", "")
            phase = payload.get("phase", "provider")
            if not re.fullmatch(r"[a-f0-9]{16}", case_id) or phase not in {"provider", "analyzer"}:
                self._write_json(400, {"error": "invalid synthetic control id"})
                return
            with CAPTURE_LOCK:
                key = f"{phase}:{case_id}"
                if payload.get("once") is True:
                    CONTROL_ONCE_RELEASES.add(key)
                CONTROL_RELEASES.setdefault(key, threading.Event()).set()
            self._write_json(200, {"released": True})
            return
        if self.path == "/analyzer/overload":
            payload = self._read_json()
            reason = payload.get("reason")
            retry_after_seconds = payload.get("retry_after_seconds", 1)
            if reason not in {"queue_full", "queue_timeout"}:
                self._write_json(400, {"error": "invalid overload reason"})
                return
            if (
                isinstance(retry_after_seconds, bool)
                or not isinstance(retry_after_seconds, int)
                or not 1 <= retry_after_seconds <= 3600
            ):
                self._write_json(400, {"error": "invalid retry delay"})
                return
            ANALYZER_OVERLOAD.update(
                reason=reason,
                retry_after_seconds=retry_after_seconds,
            )
            self._write_json(200, dict(ANALYZER_OVERLOAD))
            return

        if self.path == "/analyzer/recover":
            ANALYZER_OVERLOAD.update(reason=None, retry_after_seconds=1)
            self._write_json(200, dict(ANALYZER_OVERLOAD))
            return

        if self.path == "/capture/reset":
            with CAPTURE_LOCK:
                for key, value in CAPTURE.items():
                    if type(value) is int:
                        CAPTURE[key] = 0
                    elif isinstance(value, list):
                        CAPTURE[key] = []
                    else:
                        CAPTURE[key] = False
                capture = dict(CAPTURE)
            self._write_json(200, capture)
            return

        if self.path == "/api/v1/analyze":
            payload = self._read_json()
            if _text_contains(payload, "DISCONNECT_HOLD_ANALYZER"):
                if not self._controlled_wait(payload, "analyzer"):
                    return
            saw_canary = _text_contains_canary(payload)
            with CAPTURE_LOCK:
                CAPTURE["analyzer_requests"] += 1
                CAPTURE["analyzer_saw_canary"] |= saw_canary
            if ANALYZER_OVERLOAD["reason"] is not None:
                reason = ANALYZER_OVERLOAD["reason"]
                retry_after_seconds = ANALYZER_OVERLOAD["retry_after_seconds"]
                self._write_json(
                    503,
                    {
                        "detail": {
                            "code": "analyzer_overloaded",
                            "reason": reason,
                            "message": "Presidio Analyzer capacity is exhausted.",
                            "retry_after_seconds": retry_after_seconds,
                        }
                    },
                    headers={"Retry-After": retry_after_seconds},
                )
                return
            self._write_json(200, {"entities": _analyzer_entities(payload)})
            return

        payload = self._read_json()

        if self.path == "/v1/chat/completions":
            _record_provider_payload(self.path, payload)
            if not self._controlled_wait(payload):
                return
            failure_mode = _failure_mode(payload)
            if failure_mode == "429":
                self._write_json(
                    429,
                    {
                        "error": {
                            "code": "rate_limit_exceeded",
                            "type": "rate_limit_error",
                            "message": "Synthetic load-test rate limit.",
                        }
                    },
                    headers={"Retry-After": "2"},
                )
                return
            if failure_mode == "500":
                self._write_json(
                    500,
                    {
                        "error": {
                            "code": "upstream_error",
                            "type": "server_error",
                            "message": "Synthetic load-test provider failure.",
                        }
                    },
                )
                return
            if failure_mode == "timeout":
                time.sleep(FAILURE_DELAY_SECONDS)
            response_content = _chat_response_content(payload)
            if RESPONSE_DELAY_SECONDS > 0:
                time.sleep(RESPONSE_DELAY_SECONDS)
            if payload.get("stream") is True:
                created = int(time.time())
                if failure_mode == "stream_disconnect":
                    self._write_sse(
                        [
                            (
                                "",
                                {
                                    "id": f"chatcmpl-{DEPLOYMENT_ID}",
                                    "object": "chat.completion.chunk",
                                    "created": created,
                                    "model": "mock-chat",
                                    "choices": [
                                        {
                                            "index": 0,
                                            "delta": {
                                                "role": "assistant",
                                                "content": response_content,
                                            },
                                            "finish_reason": None,
                                        }
                                    ],
                                },
                            )
                        ]
                    )
                    return
                self._write_sse(
                    [
                        (
                            "",
                            {
                                "id": f"chatcmpl-{DEPLOYMENT_ID}",
                                "object": "chat.completion.chunk",
                                "created": created,
                                "model": "mock-chat",
                                "choices": [
                                    {
                                        "index": 0,
                                        "delta": {
                                            "role": "assistant",
                                            "content": response_content,
                                        },
                                        "finish_reason": None,
                                    }
                                ],
                            },
                        ),
                        (
                            "",
                            {
                                "id": f"chatcmpl-{DEPLOYMENT_ID}",
                                "object": "chat.completion.chunk",
                                "created": created,
                                "model": "mock-chat",
                                "choices": [
                                    {
                                        "index": 0,
                                        "delta": {},
                                        "finish_reason": "stop",
                                    }
                                ],
                            },
                        ),
                        ("", "[DONE]"),
                    ],
                    hold_after_first=STREAM_HOLD_SECONDS,
                )
                return
            self._write_json(
                200,
                {
                    "id": f"chatcmpl-{DEPLOYMENT_ID}",
                    "object": "chat.completion",
                    "created": int(time.time()),
                    "model": "mock-chat",
                    "choices": [
                        {
                            "index": 0,
                            "message": {
                                "role": "assistant",
                                "content": response_content,
                            },
                            "finish_reason": "stop",
                        }
                    ],
                    "usage": {
                        "prompt_tokens": 1,
                        "completion_tokens": 1,
                        "total_tokens": 2,
                    },
                },
            )
            return

        if self.path == "/v1/responses":
            _record_provider_payload(self.path, payload)
            if not self._controlled_wait(payload):
                return
            failure_mode = _failure_mode(payload)
            if failure_mode == "429":
                self._write_json(
                    429,
                    {
                        "error": {
                            "code": "rate_limit_exceeded",
                            "type": "rate_limit_error",
                            "message": "Synthetic load-test rate limit.",
                        }
                    },
                    headers={"Retry-After": "2"},
                )
                return
            if failure_mode == "500":
                self._write_json(
                    500,
                    {
                        "error": {
                            "code": "upstream_error",
                            "type": "server_error",
                            "message": "Synthetic load-test provider failure.",
                        }
                    },
                )
                return
            if failure_mode == "timeout":
                time.sleep(FAILURE_DELAY_SECONDS)
            response_content = _responses_response_content(payload)
            if RESPONSE_DELAY_SECONDS > 0:
                time.sleep(RESPONSE_DELAY_SECONDS)
            response = {
                "id": f"resp_{DEPLOYMENT_ID}",
                "object": "response",
                "created_at": int(time.time()),
                "status": "completed",
                "model": "mock-chat",
                "output": [
                    {
                        "id": "msg_mock",
                        "type": "message",
                        "status": "completed",
                        "role": "assistant",
                        "content": [
                            {
                                "type": "output_text",
                                "text": response_content,
                                "annotations": [],
                            }
                        ],
                    }
                ],
                "usage": {
                    "input_tokens": 1,
                    "output_tokens": 1,
                    "total_tokens": 2,
                },
            }
            if payload.get("stream") is True:
                if failure_mode == "stream_disconnect":
                    self._write_sse(
                        [
                            (
                                "response.output_text.delta",
                                {
                                    "type": "response.output_text.delta",
                                    "sequence_number": 0,
                                    "item_id": "msg_mock",
                                    "output_index": 0,
                                    "content_index": 0,
                                    "delta": response_content,
                                    "logprobs": [],
                                },
                            )
                        ]
                    )
                    return
                self._write_sse(
                    [
                        (
                            "response.output_text.delta",
                            {
                                "type": "response.output_text.delta",
                                "sequence_number": 0,
                                "item_id": "msg_mock",
                                "output_index": 0,
                                "content_index": 0,
                                "delta": response_content,
                                "logprobs": [],
                            },
                        ),
                        (
                            "response.completed",
                            {
                                "type": "response.completed",
                                "sequence_number": 1,
                                "response": response,
                            },
                        ),
                    ],
                    hold_after_first=STREAM_HOLD_SECONDS,
                )
                return
            self._write_json(
                200,
                response,
            )
            return

        if self.path == "/v1/messages":
            _record_provider_payload(self.path, payload)
            self._write_json(
                200,
                {
                    "id": "msg_mock",
                    "type": "message",
                    "role": "assistant",
                    "model": "mock-claude",
                    "content": [{"type": "text", "text": "ok"}],
                    "stop_reason": "end_turn",
                    "stop_sequence": None,
                    "usage": {
                        "input_tokens": 1,
                        "output_tokens": 1,
                    },
                },
            )
            return

        _record_provider_payload(self.path, payload)
        self._write_json(404, {"error": "not found"})


def main():
    port = int(os.getenv("PORT", "8080"))
    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    server.serve_forever()


if __name__ == "__main__":
    main()
