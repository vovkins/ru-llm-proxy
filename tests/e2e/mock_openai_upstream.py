"""Tiny mock upstream for pre-egress proxy smoke tests."""

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
import time


CAPTURE = {
    "analyzer_requests": 0,
    "provider_requests": 0,
    "provider_request_paths": [],
}


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
            self._write_json(200, dict(CAPTURE))
            return

        if self.path == "/api/v1/analyze":
            self._read_json()
            CAPTURE["analyzer_requests"] += 1
            self._write_json(200, {"entities": []})
            return

        payload = self._read_json()

        if self.path == "/v1/chat/completions":
            CAPTURE["provider_requests"] += 1
            CAPTURE["provider_request_paths"].append(self.path)
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
                            "message": {"role": "assistant", "content": "ok"},
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
            CAPTURE["provider_requests"] += 1
            CAPTURE["provider_request_paths"].append(self.path)
            self._write_json(
                200,
                {
                    "id": "resp_mock",
                    "object": "response",
                    "created_at": int(time.time()),
                    "status": "completed",
                    "model": payload.get("model", "mock-chat"),
                    "output": [
                        {
                            "id": "msg_mock",
                            "type": "message",
                            "status": "completed",
                            "role": "assistant",
                            "content": [
                                {
                                    "type": "output_text",
                                    "text": "ok",
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
            CAPTURE["provider_requests"] += 1
            CAPTURE["provider_request_paths"].append(self.path)
            self._write_json(
                200,
                {
                    "id": "msg_mock",
                    "type": "message",
                    "role": "assistant",
                    "model": payload.get("model", "mock-claude"),
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

        self._write_json(404, {"error": "not found"})


def main():
    port = int(os.getenv("PORT", "8080"))
    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    server.serve_forever()


if __name__ == "__main__":
    main()
