"""Disposable error-response fixture, never a real PII detector."""
from http.server import BaseHTTPRequestHandler, HTTPServer
import json


STATE = {"mode": "overloaded", "calls": 0, "health": {
    "status": "ok", "ner_state": "ready", "ner_warmed_up": True,
    "analysis_signature": "0" * 64,
}}


class Handler(BaseHTTPRequestHandler):
    def _json(self, status, body, retry_after=None):
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        if retry_after is not None:
            self.send_header("Retry-After", str(retry_after))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path == "/_stats":
            self._json(200, {"mode": STATE["mode"], "calls": STATE["calls"]})
        elif self.path == "/api/v1/health":
            health = dict(STATE["health"])
            if STATE["mode"] == "unavailable":
                health.update(status="error", ner_state="failed", ner_failure_phase="readiness",
                              ner_failure_class="not_ready")
            self._json(503 if STATE["mode"] == "unavailable" else 200, health)
        else:
            self._json(404, {})

    def do_POST(self):
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 <= length <= 65536:
                raise ValueError
            body = self.rfile.read(length)
            if self.path == "/_control":
                control = json.loads(body)
                if not isinstance(control, dict) or control.get("mode") not in {"ready", "overloaded", "unavailable"}:
                    raise ValueError
                if "health" in control and not isinstance(control["health"], dict):
                    raise ValueError
                STATE.update(mode=control["mode"], calls=0)
                if "health" in control:
                    STATE["health"] = control["health"]
                self._json(200, {"mode": STATE["mode"]})
                return
        except (TypeError, ValueError):
            self._json(400, {})
            return
        if self.path != "/api/v1/analyze":
            self._json(404, {})
            return
        STATE["calls"] += 1
        if STATE["mode"] == "overloaded":
            self._json(503, {"detail": {"code": "analyzer_overloaded", "reason": "queue_timeout",
                                       "retry_after_seconds": 7}}, 7)
        elif STATE["mode"] == "unavailable":
            self._json(503, {"detail": {"code": "required_ner_unavailable", "phase": "readiness",
                                       "failure_class": "not_ready"}}, 5)
        else:
            self._json(200, {"entities": []})

    def log_message(self, *args):
        pass


if __name__ == "__main__":
    HTTPServer(("0.0.0.0", 5001), Handler).serve_forever()
