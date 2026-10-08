"""Deterministic SSE boundaries for the existing mock provider, not production."""

import json
import time


PLACEHOLDER = "<PHONE_NUMBER_1>"
OPAQUE = "opaque_<PHONE_NUMBER_1>_unchanged"
TEXT = f"safe {PLACEHOLDER} tail"
ARGUMENTS = '{"phone":"<PHONE_NUMBER_1>","note":"quote\\\" slash\\u002f"}'
PHASES_TEXT = ("stream_first", "stream_text", "stream_placeholder",
               "stream_partial", "stream_terminal")
PHASES_TOOL = ("stream_first", "stream_text", "stream_arguments",
               "stream_escape", "stream_partial", "stream_terminal")


class Events:
    def __init__(self, responses, tool):
        self.responses = responses
        self.tool = tool
        self.sequence = 0
        self.created = int(time.time())

    def event(self, kind, **fields):
        value = {"type": kind, "sequence_number": self.sequence, **fields}
        self.sequence += 1
        return value

    def delta(self, text):
        if self.responses:
            kind = "response.function_call_arguments.delta" if self.tool else "response.output_text.delta"
            fields = {"item_id": "fc_test" if self.tool else "msg_test",
                      "output_index": 0, "delta": text}
            if not self.tool:
                fields.update(content_index=0, logprobs=[])
            return self.event(kind, **fields)
        delta = {"content": text}
        if self.tool:
            delta = {"tool_calls": [{"index": 0, "id": "call_test", "type": "function",
                                      "function": {"name": "lookup", "arguments": text}}]}
        return self.chat(delta)

    def chat(self, delta, finish=None):
        return {"id": "chatcmpl_sse_test", "object": "chat.completion.chunk",
                "created": self.created, "model": "mock-chat",
                "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}

    def item(self, completed=False):
        status = "completed" if completed else "in_progress"
        if self.tool:
            return {"id": "fc_test", "type": "function_call", "call_id": "call_test",
                    "name": "lookup", "arguments": ARGUMENTS if completed else "", "status": status}
        return {"id": "msg_test", "type": "message", "role": "assistant", "status": status,
                "content": [{"type": "output_text", "text": TEXT, "annotations": [], "logprobs": []}]
                if completed else []}

    def response(self, completed=False):
        return {"id": "resp_sse_test", "object": "response", "created_at": self.created,
                "model": "mock-chat", "status": "completed" if completed else "in_progress",
                "output": [self.item(True), {"id": "rs_test", "type": "reasoning",
                           "summary": [], "encrypted_content": OPAQUE}] if completed else [],
                "usage": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2}
                if completed else None}


def frame(value):
    name = value.get("type") if isinstance(value, dict) else None
    prefix = f"event: {name}\n" if name else ""
    data = json.dumps(value) if not isinstance(value, str) else value
    return (prefix + f"data: {data}\n\n").encode()


def write_stream(handler, payload, contains):
    events = Events(handler.path.endswith("responses"), contains(payload, "SSE_TOOL"))
    phases = PHASES_TOOL if events.tool else PHASES_TEXT
    handler.send_response(200)
    handler.send_header("content-type", "text/event-stream")
    handler.send_header("cache-control", "no-cache")
    handler.send_header("connection", "close")
    handler.end_headers()
    handler.wfile.flush()

    def emit(value):
        handler.wfile.write(frame(value))
        handler.wfile.flush()

    def held(phase):
        if not handler._controlled_wait(payload, phase):
            return False
        if contains(payload, "SSE_EOF_" + phase.upper()):
            handler.close_connection = True
            return False
        return True

    try:
        if not held("stream_first"):
            return
        if events.responses:
            emit(events.event("response.created", response=events.response()))
            emit(events.event("response.output_item.added", output_index=0, item=events.item()))
        emit(events.delta('{"phone":"' if events.tool else "safe "))
        if not held("stream_text"):
            return
        emit(events.delta("<PHONE_"))
        if not held(phases[2]):
            return
        emit(events.delta('NUMBER_1>","note":"quote\\' if events.tool else "NUMBER_1> tail"))
        if events.tool and not held("stream_escape"):
            return
        # Split a valid SSE JSON record at the transport level as well as deltas.
        record = frame(events.delta('" slash\\u002f"}' if events.tool else ""))
        boundary = record.index(b"data: ") + len(b"data: ") + 9
        handler.wfile.write(record[:boundary])
        handler.wfile.flush()
        if not held("stream_partial"):
            return
        handler.wfile.write(record[boundary:])
        handler.wfile.flush()
        if not held("stream_terminal"):
            return
        if contains(payload, "SSE_ERROR"):
            emit(events.event("error", code="synthetic_stream_error", message="Synthetic SSE failure.", param=None)
                 if events.responses else {"error": {"type": "server_error", "code": "synthetic_stream_error",
                                                       "message": "Synthetic SSE failure."}})
        elif events.responses:
            kind = "response.function_call_arguments.done" if events.tool else "response.output_text.done"
            fields = {"item_id": "fc_test" if events.tool else "msg_test", "output_index": 0}
            if events.tool:
                fields.update(arguments=ARGUMENTS, name="lookup")
            else:
                fields.update(text=TEXT, content_index=0, logprobs=[])
            emit(events.event(kind, **fields))
            emit(events.event("response.output_item.done", output_index=0, item=events.item(True)))
            emit(events.event("response.completed", response=events.response(True)))
        else:
            emit(events.chat({}, "tool_calls" if events.tool else "stop"))
            emit("[DONE]")
    except (BrokenPipeError, ConnectionResetError):
        pass
    finally:
        handler.close_connection = True
