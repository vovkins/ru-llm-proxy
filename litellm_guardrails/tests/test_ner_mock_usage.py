"""Large NER echo fixtures must use provider usage, not local BPE at SSE EOF."""

import asyncio
import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import litellm
import pytest
import litellm.litellm_core_utils.streaming_chunk_builder_utils as builder
from litellm.litellm_core_utils.litellm_logging import Logging
from openai.types.chat import ChatCompletionChunk

from tests.e2e import mock_openai_upstream as mock
from litellm_guardrails.streaming_restoration import StreamingResponseRestorer


@pytest.mark.parametrize("usage_layout", ["separate", "finish", "missing"])
@pytest.mark.parametrize("restore_large_value", [False, True])
def test_boundary_echo_usage_avoids_local_tokenization(monkeypatch, usage_layout, restore_large_value):
    text = ("<BASE64_DATA_1>" if restore_large_value else
            " " * 128000 + "Компания <ORGANIZATION_1> открыла счёт.")
    expected = "A" * 64 + "\n" + " " * 140000 + "B" * 64 if restore_large_value else text
    messages = [{"role": "user", "content": text}]
    payload = {"model": "mock-chat", "messages": messages, "stream": True}
    output = {}
    monkeypatch.setattr(mock, "ECHO_CHAT_CONTENT", True)
    monkeypatch.setattr(mock, "RESPONSE_DELAY_SECONDS", 0)
    monkeypatch.setattr(mock, "_record_provider_payload", lambda *_: None)
    handler = SimpleNamespace(
        path="/v1/chat/completions",
        _read_json=lambda: payload,
        _controlled_wait=lambda _: True,
        _write_sse=lambda events, **_: output.update(events=events),
    )
    mock.Handler.do_POST(handler)
    events = output["events"]
    assert events[-1] == ("", "[DONE]")
    assert payload["messages"][0]["content"] == text
    if usage_layout == "finish":
        # A direct stream_chunk_builder test accepts this layout, but the
        # OpenAI streaming wrapper loses its usage before aggregation.
        events[-3][1]["usage"] = events[-2][1].pop("usage")
    chunks = []
    for _, event in events:
        if not isinstance(event, dict):
            continue
        event = dict(event)
        if usage_layout == "missing":
            # Negative control: spy on BPE instead of spending minutes in it.
            event.pop("usage", None)
        chunks.append(ChatCompletionChunk(**event))
    counted = []
    monkeypatch.setattr(builder, "token_counter", lambda **kw: counted.append(kw) or 123)

    async def consume():
        async def source():
            for chunk in chunks:
                yield chunk

        logger = Logging(
            model="mock-chat", messages=messages, stream=True, call_type="acompletion",
            start_time=datetime.datetime.now(), litellm_call_id="usage-test", function_id="usage-test",
        )
        logger.dispatch_success_handlers = AsyncMock()
        logger.dispatch_failure_handlers = AsyncMock()
        wrapper = litellm.CustomStreamWrapper(
            source(), model="mock-chat", custom_llm_provider="openai", logging_obj=logger,
            stream_options={"include_usage": True},
        )
        return [chunk async for chunk in wrapper]

    chunks = asyncio.run(consume())
    if restore_large_value:
        restorer = StreamingResponseRestorer({"<BASE64_DATA_1>": expected})
        chunks = [item for chunk in chunks for item in restorer.process(chunk)]
        chunks.extend(restorer.finish())
    response = litellm.stream_chunk_builder(chunks, messages=messages)
    assert response.choices[0].message.content == expected
    assert response.choices[0].finish_reason == "stop"
    falls_back = usage_layout != "separate"
    assert bool(counted) == falls_back
    assert response.usage.prompt_tokens == (123 if falls_back else 1)
    assert response.usage.completion_tokens == (123 if falls_back else 1)
