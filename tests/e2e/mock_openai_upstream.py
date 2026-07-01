"""Tiny OpenAI-compatible mock upstream for pre-egress proxy smoke tests."""

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
import time


CAPTURE = {
    "analyzer_requests": 0,
    "provider_requests": 0,
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
            CAPTURE["analyzer_requests"] = 0
            CAPTURE["provider_requests"] = 0
            self._write_json(200, dict(CAPTURE))
            return

        if self.path == "/api/v1/analyze":
            self._read_json()
            CAPTURE["analyzer_requests"] += 1
            self._write_json(200, {"entities": []})
            return

        if self.path == "/v1/chat/completions":
            self._read_json()
            CAPTURE["provider_requests"] += 1
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

        self._write_json(404, {"error": "not found"})


def main():
    port = int(os.getenv("PORT", "8080"))
    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    server.serve_forever()


if __name__ == "__main__":
    main()
