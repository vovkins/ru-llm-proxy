"""Content-free timing from the standard HTTPX/httpcore trace extension."""
from __future__ import annotations

import time


class RequestPoolTiming:
    """Pre-send time excludes completed TCP/TLS setup, not scheduler overhead."""

    SETUP_EVENTS = frozenset({
        "connection.connect_tcp", "connection.connect_unix_socket",
        "connection.start_tls",
    })

    def __init__(self, *, clock=time.perf_counter):
        self.clock = clock
        self.started_at = clock()
        self.first_send_at = None
        self.response_headers_at = None
        self.setup_started = {}
        self.setup_seconds = 0.0
        self.setup_count = 0
        self.setup_incomplete = False

    async def trace(self, name, info):
        # The trace payload can contain headers and exceptions. Never retain it.
        if not isinstance(name, str):
            return
        event, _, phase = name.rpartition(".")
        if event in self.SETUP_EVENTS and self.first_send_at is None:
            now = self.clock()
            if phase == "started":
                if event in self.setup_started:
                    self.setup_incomplete = True
                self.setup_started[event] = now
            elif phase == "complete":
                start = self.setup_started.pop(event, None)
                if start is None or now < start:
                    self.setup_incomplete = True
                else:
                    self.setup_seconds += now - start
                    self.setup_count += 1
            elif phase == "failed":
                self.setup_incomplete = True
        elif name == "http11.send_request_headers.started" and self.first_send_at is None:
            self.first_send_at = self.clock()
        elif name == "http11.receive_response_headers.complete" and self.response_headers_at is None:
            self.response_headers_at = self.clock()

    def summary(self):
        pre_send = None if self.first_send_at is None else self.first_send_at - self.started_at
        setup_known = not self.setup_incomplete and not self.setup_started
        queue_bound = None
        if setup_known and pre_send is not None and 0 <= self.setup_seconds <= pre_send:
            queue_bound = pre_send - self.setup_seconds
        return {
            "pre_send_seconds": pre_send,
            "connection_setup_seconds": self.setup_seconds if setup_known else None,
            "connection_setup_events": self.setup_count,
            "pool_wait_upper_seconds": queue_bound,
            "after_send_to_headers_seconds": (
                self.response_headers_at - self.first_send_at
                if self.response_headers_at is not None and self.first_send_at is not None else None
            ),
            "pure_pool_wait_measured": False,
        }
