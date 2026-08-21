"""Tiny mock upstream for pre-egress proxy smoke tests."""

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
import re
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
PII_PLACEHOLDER_PATTERN = re.compile(r"<[A-Z][A-Z0-9_]*_[1-9][0-9]*>")

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


def _chat_response_content(payload) -> str:
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
    CAPTURE["provider_requests"] += 1
    CAPTURE["provider_request_paths"].append(path)
    CAPTURE["provider_saw_canary"] = (
        CAPTURE["provider_saw_canary"] or _text_contains_canary(payload)
    )
    CAPTURE["provider_saw_private_key_marker"] = (
        CAPTURE["provider_saw_private_key_marker"]
        or _text_contains(payload, PRIVATE_KEY_MARKER)
    )
    CAPTURE["provider_saw_raw_phone"] = (
        CAPTURE["provider_saw_raw_phone"] or _text_contains(payload, RAW_PHONE)
    )
    CAPTURE["provider_saw_phone_placeholder"] = (
        CAPTURE["provider_saw_phone_placeholder"]
        or _text_contains(payload, PHONE_PLACEHOLDER)
    )
    CAPTURE["provider_saw_pii_placeholder"] = (
        CAPTURE["provider_saw_pii_placeholder"]
        or _text_matches(payload, PII_PLACEHOLDER_PATTERN)
    )


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

    def _write_json(self, status, payload):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/health":
            self._write_json(200, {"status": "ok"})
            return
        if self.path == "/capture":
            self._write_json(200, dict(CAPTURE))
            return
        self._write_json(404, {"error": "not found"})

    def do_POST(self):
        if self.path == "/capture/reset":
            for key, value in CAPTURE.items():
                if type(value) is int:
                    CAPTURE[key] = 0
                elif isinstance(value, list):
                    CAPTURE[key] = []
                else:
                    CAPTURE[key] = False
            self._write_json(200, dict(CAPTURE))
            return

        if self.path == "/api/v1/analyze":
            payload = self._read_json()
            CAPTURE["analyzer_requests"] += 1
            CAPTURE["analyzer_saw_canary"] = (
                CAPTURE["analyzer_saw_canary"] or _text_contains_canary(payload)
            )
            self._write_json(200, {"entities": _analyzer_entities(payload)})
            return

        payload = self._read_json()

        if self.path == "/v1/chat/completions":
            _record_provider_payload(self.path, payload)
            response_content = _chat_response_content(payload)
            self._write_json(
                200,
                {
                    "id": "chatcmpl-mock",
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
            response_content = _responses_response_content(payload)
            self._write_json(
                200,
                {
                    "id": "resp_mock",
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
                },
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
