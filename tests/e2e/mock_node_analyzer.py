"""Bounded synthetic transport fixture, never a production PII detector."""

import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading
import time

LOCK = threading.Lock()
STATE = {"mode": "ready", "delay": 0, "calls": [], "completed": 0}


class Handler(BaseHTTPRequestHandler):
    def reply(self, status, value, retry=None):
        data = json.dumps(value).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        if retry is not None:
            self.send_header("Retry-After", str(retry))
        self.end_headers()
        try:
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_GET(self):
        if self.path == "/_stats":
            with LOCK:
                snapshot = dict(STATE, calls=list(STATE["calls"]))
            self.reply(200, snapshot)
        elif self.path == "/api/v1/health":
            with LOCK:
                mode = STATE["mode"]
            if mode in {"unavailable", "boundary"}:
                self.reply(503, {"status": "error", "ner_state": "failed"}, 5 if mode == "unavailable" else None)
            else:
                self.reply(200, {"status": "ok", "ner_state": "ready", "analysis_signature": "0" * 64})
        else:
            self.reply(404, {})

    def do_POST(self):
        size = int(self.headers.get("Content-Length", "0"))
        if not 0 <= size <= 1048576:
            self.reply(400, {})
            return
        raw = self.rfile.read(size)
        if self.path == "/_control":
            data = json.loads(raw)
            if data.get("mode") not in {"ready", "overloaded", "unavailable", "boundary", "drop", "delay_first",
                                        "bad_gateway", "gateway_timeout", "rate_limited", "delayed_bad_gateway"}:
                self.reply(400, {})
                return
            with LOCK:
                STATE.update(mode=data["mode"], delay=float(data.get("delay", 0)), calls=[], completed=0)
            self.reply(200, {})
            return
        if self.path != "/api/v1/analyze":
            self.reply(404, {})
            return
        with LOCK:
            STATE["calls"].append({"id": self.headers.get("X-Ru-LLM-Request-ID"),
                "field": self.headers.get("X-Ru-LLM-Text-Field-Index"),
                "budget": self.headers.get("X-Ru-LLM-Analysis-Budget-Seconds"),
                "sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw)})
            mode = STATE["mode"]
            delay = STATE["delay"] if len(STATE["calls"]) == 1 or mode == "delayed_bad_gateway" else 0
        if mode == "drop":
            self.close_connection = True
            return
        if mode in {"delay_first", "delayed_bad_gateway"}:
            time.sleep(delay)
        if mode in {"bad_gateway", "delayed_bad_gateway"}:
            self.reply(502, {"detail": "synthetic_transport_failure"})
        elif mode == "gateway_timeout":
            self.reply(504, {"detail": "synthetic_upstream_504"})
        elif mode == "rate_limited":
            self.reply(429, {"detail": "synthetic_rate_limit"}, 11)
        elif mode == "overloaded":
            self.reply(503, {"detail": {"code": "analyzer_overloaded", "reason": "queue_timeout",
                                       "retry_after_seconds": 7}}, 7)
        elif mode in {"unavailable", "boundary"}:
            self.reply(503, {"detail": {"code": "required_ner_unavailable", "phase": "windowing",
                "failure_class": "window_boundary_unresolved" if mode == "boundary" else "not_ready"}},
                None if mode == "boundary" else 5)
        else:
            self.reply(200, {"entities": []})
        with LOCK:
            STATE["completed"] += 1

    def log_message(self, *_):
        pass


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", 5001), Handler).serve_forever()
