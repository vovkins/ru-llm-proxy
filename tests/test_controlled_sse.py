"""Validate the deterministic provider independently of LiteLLM."""

import importlib.util
import io
import json
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("controlled_sse", ROOT / "tests/e2e/controlled_sse.py")
provider = importlib.util.module_from_spec(spec)
spec.loader.exec_module(provider)


class Handler:
    def __init__(self, path):
        self.path = path
        self.wfile = io.BytesIO()
        self.phases = []
        self.close_connection = False

    def send_response(self, status):
        assert status == 200

    def send_header(self, *_args):
        pass

    def end_headers(self):
        pass

    def _controlled_wait(self, _payload, phase):
        self.phases.append(phase)
        return True


def values(handler):
    return [json.loads(line[6:]) if line[6:] != "[DONE]" else "[DONE]"
            for line in handler.wfile.getvalue().decode().splitlines() if line.startswith("data: ")]


@pytest.mark.parametrize("api", ["chat/completions", "responses"])
@pytest.mark.parametrize("tool", [False, True])
def test_controlled_stream_is_valid_and_preserves_all_fragments(api, tool):
    handler = Handler("/v1/" + api)
    provider.write_stream(handler, "SSE_TOOL" if tool else "", lambda payload, text: text in payload)
    result = values(handler)
    assert handler.phases == list(provider.PHASES_TOOL if tool else provider.PHASES_TEXT)
    if api == "responses":
        assert result[-1]["type"] == "response.completed"
        kind = "response.function_call_arguments.delta" if tool else "response.output_text.delta"
        actual = "".join(event.get("delta", "") for event in result if event["type"] == kind)
        assert result[-1]["response"]["output"][1]["encrypted_content"] == provider.OPAQUE
        assert [event["sequence_number"] for event in result] == list(range(len(result)))
    else:
        assert result[-1] == "[DONE]"
        assert result[-2]["choices"][0]["finish_reason"] == ("tool_calls" if tool else "stop")
        actual = "".join(
            event["choices"][0]["delta"]["tool_calls"][0]["function"]["arguments"] if tool
            else event["choices"][0]["delta"].get("content", "") for event in result[:-2]
        )
    if tool:
        assert json.loads(actual) == {"phone": provider.PLACEHOLDER, "note": 'quote" slash/'}
    else:
        assert actual == provider.TEXT
    assert handler.close_connection


@pytest.mark.parametrize("tool", [False, True])
def test_provider_eof_does_not_emit_its_own_terminal(tool):
    handler = Handler("/v1/responses")
    phase = "stream_arguments" if tool else "stream_placeholder"
    payload = ("SSE_TOOL " if tool else "") + "SSE_EOF_" + phase.upper()
    provider.write_stream(handler, payload, lambda value, text: text in value)
    result = values(handler)
    assert phase == handler.phases[-1]
    assert all(value["type"] != "response.completed" for value in result)
    assert result[-1]["delta"] == "<PHONE_"


def test_observer_exposes_the_original_hook_without_an_extra_generator():
    source = (ROOT / "tests/e2e/disconnect_observer.py").read_text()
    assert "async_post_call_streaming_iterator_hook = RuPIIGuardrail.async_post_call_streaming_iterator_hook" in source
