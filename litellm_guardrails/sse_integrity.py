"""Verify provider terminal evidence before an SDK can synthesize completion."""

import json

import httpx

# Bound one decoded event, not the full answer or request context. Large
# Responses output snapshots are allowed; a compressed event has the same bound.
MAX_SSE_EVENT_BYTES = 32 * 1024 * 1024


class IncompleteUpstreamStream(httpx.RemoteProtocolError):
    def __init__(self, reason: str):
        super().__init__(f"Upstream SSE protocol failure: {reason}")
        self.reason = reason


class SSEIntegrity:
    def __init__(self, api: str, choices: int = 1, max_event_bytes=MAX_SSE_EVENT_BYTES):
        if api not in {"chat", "responses"} or not 1 <= choices <= 128:
            raise ValueError("Unsupported upstream SSE contract")
        self.api = api
        self.expected_choices = set(range(choices))
        self.finished_choices = set()
        self.max_event_bytes = max_event_bytes
        self.line = bytearray()
        self.data = bytearray()
        self.event_bytes = 0
        self.skip_lf = False
        self.terminal = None
        self.done = False

    def _dispatch(self):
        payload = bytes(self.data).removesuffix(b"\n")
        self.data.clear()
        self.event_bytes = 0
        if not payload:
            return
        if self.done:
            raise IncompleteUpstreamStream("data_after_done")
        if payload == b"[DONE]":
            if self.terminal is None:
                raise IncompleteUpstreamStream("done_without_terminal")
            self.done = True
            return
        try:
            value = json.loads(payload)
        except (ValueError, UnicodeError, RecursionError):
            raise IncompleteUpstreamStream("invalid_event_json") from None
        if not isinstance(value, dict):
            raise IncompleteUpstreamStream("invalid_event_shape")
        if "error" in value or value.get("type") == "error":
            self.terminal = "provider_error"
        elif self.api == "chat":
            choices = value.get("choices", [])
            if not isinstance(choices, list):
                raise IncompleteUpstreamStream("invalid_choices")
            for choice in choices:
                if not isinstance(choice, dict):
                    raise IncompleteUpstreamStream("invalid_choices")
                index = choice.get("index")
                if not isinstance(index, int) or isinstance(index, bool) or index not in self.expected_choices:
                    raise IncompleteUpstreamStream("invalid_choice_index")
                reason = choice.get("finish_reason")
                if reason is not None:
                    if not isinstance(reason, str) or not reason:
                        raise IncompleteUpstreamStream("invalid_finish_reason")
                    self.finished_choices.add(index)
            if self.finished_choices == self.expected_choices:
                self.terminal = "complete"
        else:
            terminal = {"response.completed": "complete", "response.incomplete": "provider_incomplete",
                        "response.failed": "provider_error"}.get(value.get("type"))
            if terminal:
                self.terminal = terminal

    def _line(self):
        line = bytes(self.line)
        self.line.clear()
        if not line:
            self._dispatch()
        elif line.startswith(b"data:"):
            value = line[5:]
            self.data.extend(value[1:] if value.startswith(b" ") else value)
            self.data.append(10)

    def feed(self, chunk: bytes):
        offset = 0
        if self.skip_lf and chunk:
            offset = int(chunk[0] == 10)
            self.skip_lf = False
        while offset < len(chunk):
            cr, lf = chunk.find(b"\r", offset), chunk.find(b"\n", offset)
            separators = [i for i in (cr, lf) if i >= 0]
            end = min(separators) if separators else len(chunk)
            added = end - offset + int(bool(separators))
            if self.event_bytes + added > self.max_event_bytes:
                raise IncompleteUpstreamStream("event_too_large")
            self.event_bytes += added
            self.line.extend(chunk[offset:end])
            if not separators:
                break
            self._line()
            offset = end + 1
            if chunk[end] == 13:
                if offset < len(chunk) and chunk[offset] == 10:
                    offset += 1
                elif offset == len(chunk):
                    self.skip_lf = True

    def finish(self):
        if self.line or self.data:
            raise IncompleteUpstreamStream("truncated_record")
        if self.terminal is None:
            raise IncompleteUpstreamStream("missing_terminal")


class _RawFragments(httpx.AsyncByteStream):
    def __init__(self, response):
        self.response = response

    async def __aiter__(self):
        async for chunk in self.response.aiter_raw():
            # Split available bytes without waiting for a full-sized block.
            # This bounds the input of HTTPX's standard compression decoder.
            for offset in range(0, len(chunk), 4096):
                yield chunk[offset:offset + 4096]

    async def aclose(self):
        await self.response.aclose()


class VerifiedSSEStream(httpx.AsyncByteStream):
    def __init__(self, response, integrity, observe):
        self.raw = response
        self.integrity = integrity
        self.observe = observe
        self.decoder = httpx.Response(response.status_code, headers=response.headers,
                                      stream=_RawFragments(response), request=response.request)
        self.reported = False

    def report(self, outcome, reason=None):
        if not self.reported:
            self.reported = True
            self.observe(outcome, reason)

    async def __aiter__(self):
        try:
            async for chunk in self.decoder.aiter_bytes():
                self.integrity.feed(chunk)
                yield chunk
            self.integrity.finish()
            self.report(self.integrity.terminal)
        except IncompleteUpstreamStream as error:
            self.report("incomplete", error.reason)
            raise
        except Exception:
            self.report("provider_error")
            raise

    async def aclose(self):
        self.report(self.integrity.terminal or "cancelled")
        try:
            await self.raw.aclose()
        finally:
            self.integrity.line.clear()
            self.integrity.data.clear()


def verified_response(response, api, choices, observe):
    stream = VerifiedSSEStream(response, SSEIntegrity(api, choices), observe)
    headers = response.headers.copy()
    # The public view contains decoded bytes. Avoid decoding them twice.
    for header in ("content-encoding", "content-length"):
        headers.pop(header, None)
    return httpx.Response(response.status_code, headers=headers, stream=stream,
                          request=response.request, extensions=response.extensions)
