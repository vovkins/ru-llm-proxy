"""Mock Responses upstream for validating the ChatGPT Codex bridge."""

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
import time


ORIGINAL = "Kdir"
REPLACEMENT = "Companynameabc"
CAPTURE = {
    "provider_requests": 0,
    "authorization_is_bearer": False,
    "account_id_present": False,
    "proxy_key_absent": True,
    "original_absent": True,
    "replacement_present": False,
}


def _contains(value, needle: str) -> bool:
    if isinstance(value, str):
        return needle.casefold() in value.casefold()
    if isinstance(value, list):
        return any(_contains(item, needle) for item in value)
    if isinstance(value, dict):
        return any(_contains(item, needle) for item in value.values())
    return False


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, format, *args):
        return

    def _read_json(self):
        length = int(self.headers.get("content-length", "0") or "0")
        return json.loads(self.rfile.read(length)) if length else {}

    def _write_json(self, status, payload):
        body = json.dumps(payload, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _write_sse(self, events):
        self.send_response(200)
        self.send_header("content-type", "text/event-stream")
        self.send_header("cache-control", "no-cache")
        self.send_header("connection", "close")
        self.end_headers()
        for event in events:
            body = json.dumps(event, ensure_ascii=False)
            self.wfile.write(f"data: {body}\n\n".encode())
        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()

    def do_GET(self):
        if self.path == "/health":
            self._write_json(200, {"status": "ok"})
        elif self.path == "/capture":
            self._write_json(200, CAPTURE)
        else:
            self._write_json(404, {"error": "not found"})

    def do_POST(self):
        payload = self._read_json()
        if self.path != "/v1/responses":
            self._write_json(404, {"error": "not found"})
            return

        CAPTURE["provider_requests"] += 1
        CAPTURE["authorization_is_bearer"] = str(
            self.headers.get("Authorization", "")
        ).startswith("Bearer ")
        CAPTURE["account_id_present"] = bool(
            self.headers.get("ChatGPT-Account-Id")
        )
        CAPTURE["proxy_key_absent"] = not bool(
            self.headers.get("X-LiteLLM-API-Key")
        )
        CAPTURE["original_absent"] = not _contains(payload, ORIGINAL)
        CAPTURE["replacement_present"] = _contains(payload, REPLACEMENT)

        response = {
            "id": "resp_mock",
            "object": "response",
            "created_at": int(time.time()),
            "status": "completed",
            "model": payload.get("model", "mock"),
            "parallel_tool_calls": False,
            "output": [
                {
                    "id": "msg_mock",
                    "type": "message",
                    "status": "completed",
                    "role": "assistant",
                    "content": [
                        {
                            "type": "output_text",
                            "text": "Создан CompanynameabcService",
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
            self._write_sse(
                [
                    {
                        "type": "response.output_text.delta",
                        "item_id": "msg_mock",
                        "output_index": 0,
                        "content_index": 0,
                        "delta": "Создан Companyname",
                    },
                    {
                        "type": "response.output_text.delta",
                        "item_id": "msg_mock",
                        "output_index": 0,
                        "content_index": 0,
                        "delta": "abcService",
                    },
                    {
                        "type": "response.output_text.done",
                        "item_id": "msg_mock",
                        "output_index": 0,
                        "content_index": 0,
                        "text": "Создан CompanynameabcService",
                    },
                    {"type": "response.completed", "response": response},
                ]
            )
            return
        self._write_json(200, response)


def main():
    port = int(os.getenv("PORT", "17892"))
    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()


if __name__ == "__main__":
    main()
