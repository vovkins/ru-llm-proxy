"""Regression tests for native streaming text and JSON tool arguments."""

import asyncio
import copy
import json
import random
from unittest.mock import AsyncMock, MagicMock

import litellm
import pytest
from openai.types.responses import (
    ResponseFunctionCallArgumentsDeltaEvent,
    ResponseStreamEvent,
)
from pydantic import TypeAdapter

from litellm_guardrails.pii_guardrail import RuPIIGuardrail
from litellm_guardrails.streaming_restoration import (
    _StreamingJSONRestorer,
    _StreamingPlaceholderReplacer,
    restore_arguments,
    restore_text,
)


async def _chunks(events):
    for event in events:
        yield event


def _guardrail(mapping):
    guardrail = RuPIIGuardrail()
    guardrail._redis = AsyncMock()
    guardrail._redis.get.return_value = json.dumps(mapping)
    return guardrail


async def _restore(guardrail, events, request_id="stream-test"):
    return [
        event
        async for event in guardrail.async_post_call_streaming_iterator_hook(
            user_api_key_dict=MagicMock(),
            response=_chunks(events),
            request_data={"metadata": {"pii_request_id": request_id}},
        )
    ]


def _chat(delta, *, finish=None, choice=0):
    return litellm.ModelResponseStream(
        id="chat-stream-test",
        model="mock-chat",
        choices=[
            litellm.StreamingChoices(index=choice, delta=delta, finish_reason=finish)
        ],
    )


def _arguments_delta(text, *, output=0, sequence=0, typed=False):
    event = {
        "type": "response.function_call_arguments.delta",
        "item_id": f"fc-{output}",
        "output_index": output,
        "sequence_number": sequence,
        "delta": text,
    }
    return ResponseFunctionCallArgumentsDeltaEvent(**event) if typed else event


def _field(event, name):
    return event.get(name) if isinstance(event, dict) else getattr(event, name, None)


@pytest.mark.asyncio
@pytest.mark.parametrize("api", ["chat", "responses", "responses-typed"])
async def test_stream_tools_restore_city_and_escaped_code(api):
    original = 'Тверь "центр"\nC:\\code\\file.py\t😀'
    guardrail = _guardrail({"<LOCATION_1>": original})
    arguments = '{"city":"<LOCATION_1>"}'
    pieces = [arguments[:14], arguments[14:19], arguments[19:]]
    if api == "chat":
        events = [
            _chat(
                {
                    "tool_calls": [
                        {
                            "index": 0,
                            "id": "call-test" if index == 0 else None,
                            "function": {
                                "name": "get_city_code" if index == 0 else None,
                                "arguments": piece,
                            },
                        }
                    ]
                }
            )
            for index, piece in enumerate(pieces)
        ] + [_chat({}, finish="tool_calls")]
    else:
        events = [
            _arguments_delta(piece, sequence=index, typed=api == "responses-typed")
            for index, piece in enumerate(pieces)
        ]
    restored = await _restore(guardrail, events)
    if api == "chat":
        result = "".join(
            call.function.arguments or ""
            for event in restored
            for choice in event.choices
            for call in choice.delta.tool_calls or []
        )
    else:
        result = "".join(_field(event, "delta") or "" for event in restored)
    assert json.loads(result) == {"city": original}
    guardrail._redis.get.assert_awaited_once()
    guardrail._redis.delete.assert_awaited_once_with("pii_mapping:stream-test")


@pytest.mark.asyncio
@pytest.mark.parametrize("api", ["chat", "responses"])
async def test_nonstream_tool_arguments_use_the_same_safe_json_restoration(api):
    original = 'Тверь "центр"\nC:\\code\t😀 literal <PERSON_1>'
    guardrail = _guardrail({"<LOCATION_1>": original, "<PERSON_1>": "Олег"})
    arguments = '{"<LOCATION_1>":"schema-key","city":"<LOCATION_1>"}'
    if api == "chat":
        response = litellm.ModelResponse(
            choices=[
                litellm.Choices(
                    message=litellm.Message(
                        role="assistant",
                        tool_calls=[
                            {
                                "id": "call-test",
                                "type": "function",
                                "function": {
                                    "name": "get_city_code",
                                    "arguments": arguments,
                                },
                            }
                        ],
                    ),
                    finish_reason="tool_calls",
                )
            ]
        )
        function = response.choices[0].message.tool_calls[0].function
    else:
        function = {
            "type": "function_call",
            "id": "fc-test",
            "call_id": "call-test",
            "name": "get_city_code",
            "arguments": arguments,
        }
        response = {"object": "response", "output": [function]}
    await guardrail.async_post_call_success_hook(
        data={"metadata": {"pii_request_id": "nonstream-json-test"}},
        user_api_key_dict=MagicMock(),
        response=response,
    )
    assert json.loads(_field(function, "arguments")) == {
        "<LOCATION_1>": "schema-key",
        "city": original,
    }
    assert _field(function, "name") == "get_city_code"
    guardrail._redis.delete.assert_awaited_once_with("pii_mapping:nonstream-json-test")


@pytest.mark.asyncio
async def test_responses_text_deltas_and_completed_snapshot_agree():
    guardrail = _guardrail({"<LOCATION_1>": "Тверь"})
    output = [
        {
            "id": "msg-test",
            "type": "message",
            "role": "assistant",
            "content": [
                {
                    "type": "output_text",
                    "text": "Город: <LOCATION_1>",
                    "annotations": [],
                }
            ],
        },
        {
            "type": "reasoning",
            "encrypted_content": "opaque-<LOCATION_1>",
        },
    ]
    events = [
        {
            "type": "response.output_text.delta",
            "item_id": "msg-test",
            "output_index": 0,
            "content_index": 0,
            "sequence_number": index,
            "delta": text,
            "logprobs": [],
        }
        for index, text in enumerate(["Город: <LOC", "ATION_1>"])
    ]
    events.append(
        {
            "type": "response.completed",
            "sequence_number": 2,
            "response": {
                "id": "resp-test",
                "object": "response",
                "status": "completed",
                "output": output,
            },
        }
    )
    restored = await _restore(guardrail, copy.deepcopy(events))
    text = "".join(event.get("delta", "") for event in restored)
    assert text == "Город: Тверь"
    assert restored[-1]["response"]["output"][0]["content"][0]["text"] == text
    assert (
        restored[-1]["response"]["output"][1]["encrypted_content"]
        == "opaque-<LOCATION_1>"
    )
    assert [event["sequence_number"] for event in restored] == [0, 1, 2]


@pytest.mark.parametrize("escaped", [False, True])
@pytest.mark.parametrize("split", range(1, 29))
def test_json_restoration_at_every_split_including_unicode_escapes(escaped, split):
    mapping = {"<LOCATION_1>": 'Тверь "центр"\n\\😀'}
    source = (
        '{"city":"<LOCATION_1>","array":["<LOCATION_1>",{"<LOCATION_1>":"unchanged"}]}'
    )
    if escaped:
        source = source.replace("<", "\\u003c").replace(">", "\\u003e")
    restorer = _StreamingJSONRestorer(mapping)
    result = (
        restorer.push(source[:split]) + restorer.push(source[split:]) + restorer.flush()
    )
    assert json.loads(result) == {
        "city": mapping["<LOCATION_1>"],
        "array": [mapping["<LOCATION_1>"], {"<LOCATION_1>": "unchanged"}],
    }


def test_json_escapes_and_surrogate_pairs_in_single_character_fragments():
    mapping = {"😀": 'emoji "original"', "Зетта Групп": "Т-Банк"}
    source = json.dumps(
        {"city": "😀 Зетта Групп", "other": [1, True, None]}, ensure_ascii=True
    )
    restorer = _StreamingJSONRestorer(mapping)
    result = "".join(restorer.push(char) for char in source) + restorer.flush()
    assert json.loads(result) == {
        "city": 'emoji "original" Т-Банк',
        "other": [1, True, None],
    }


def test_replacement_does_not_rewrite_inserted_originals():
    mapping = {"<LOCATION_1>": "literal <PERSON_1>", "<PERSON_1>": "other person"}
    assert restore_text("<LOCATION_1>", mapping) == "literal <PERSON_1>"
    assert json.loads(restore_arguments('{"city":"<LOCATION_1>"}', mapping)) == {
        "city": "literal <PERSON_1>",
    }


def test_pending_text_is_bounded_and_unknown_prefix_is_not_dropped():
    restorer = _StreamingPlaceholderReplacer({"<LOCATION_1>": "Тверь"})
    source = "x" * 100_000 + "<LOC"
    assert restorer.push(0, source) == "x" * 100_000
    assert restorer._pending == {0: "<LOC"}
    assert restorer.flush(0) == "<LOC"


@pytest.mark.parametrize("pieces", [["aa"], ["a", "a"], ["a", "aa", "b"], ["aaab"]])
def test_overlapping_dictionary_keys_are_not_split_inside_a_complete_match(pieces):
    mapping = {"aa": "X", "abc": "Y"}
    restorer = _StreamingPlaceholderReplacer(mapping)
    output = "".join(restorer.push(0, piece) for piece in pieces) + restorer.flush(0)
    assert output == restore_text("".join(pieces), mapping)


def test_dictionary_restoration_matches_whole_text_for_randomized_boundaries():
    rng = random.Random(1234)
    for _ in range(500):
        mapping = {
            "".join(rng.choices("abc", k=rng.randint(1, 6))): str(index)
            for index in range(rng.randint(1, 5))
        }
        source = "".join(rng.choices("abc", k=rng.randint(1, 25)))
        restorer = _StreamingPlaceholderReplacer(mapping)
        output = []
        offset = 0
        while offset < len(source):
            length = rng.randint(1, 5)
            output.append(restorer.push(0, source[offset : offset + length]))
            offset += length
        assert "".join(output) + restorer.flush(0) == restore_text(source, mapping)


@pytest.mark.asyncio
@pytest.mark.parametrize("typed", [False, True])
@pytest.mark.parametrize("part_kind", ["text", "summary", "refusal"])
async def test_native_part_prefix_and_delta_share_state_without_touching_metadata(
    typed, part_kind
):
    guardrail = _guardrail({"<LOCATION_1>": "Тверь"})
    field = "refusal" if part_kind == "refusal" else "text"
    kind = {
        "text": "response.output_text",
        "summary": "response.reasoning_summary_text",
        "refusal": "response.refusal",
    }[part_kind]
    part_type = {
        "text": "output_text",
        "summary": "summary_text",
        "refusal": "refusal",
    }[part_kind]
    index_field = "summary_index" if part_kind == "summary" else "content_index"
    part = {"type": part_type, field: "<LOC"}
    if part_kind == "text":
        part["annotations"] = []
    prefix = (
        "response.reasoning_summary_part"
        if part_kind == "summary"
        else "response.content_part"
    )
    common = {"item_id": "item-<LOCATION_1>", "output_index": 1, index_field: 2}
    events = [
        {"type": prefix + ".added", "sequence_number": 0, "part": part, **common},
        {"type": kind + ".delta", "sequence_number": 1, "delta": "ATION_1>", **common},
        {"type": kind + ".done", "sequence_number": 2, field: "<LOCATION_1>", **common},
    ]
    if part_kind == "text":
        events[1]["logprobs"] = []
        events[2]["logprobs"] = []
    if typed:
        adapter = TypeAdapter(ResponseStreamEvent)
        events = [adapter.validate_python(event) for event in events]
    restored = await _restore(guardrail, events)
    assert (
        _field(_field(restored[0], "part"), field) + _field(restored[1], "delta")
        == "Тверь"
    )
    assert _field(restored[2], field) == "Тверь"
    assert all(_field(event, "item_id") == "item-<LOCATION_1>" for event in restored)
    assert all(_field(event, index_field) == 2 for event in restored)


@pytest.mark.asyncio
async def test_unknown_response_event_passes_through_without_a_fake_terminal_event():
    guardrail = _guardrail({"<LOCATION_1>": "Тверь"})
    unknown = {
        "type": "response.future.delta",
        "delta": "<LOCATION_1>",
        "encrypted_content": "opaque-<LOCATION_1>",
        "sequence_number": 4,
    }
    events = [unknown, _arguments_delta('{"city":"<LOC', sequence=5)]
    restored = await _restore(guardrail, copy.deepcopy(events))
    assert restored[0] == unknown
    assert "".join(event["delta"] for event in restored[1:]) == '{"city":"<LOC'
    assert all(event["type"].endswith(".delta") for event in restored)


def test_incomplete_json_key_is_not_rewritten_or_duplicated_at_eof():
    source = '{"<LOCATION_1>\\'
    assert restore_arguments(source, {"<LOCATION_1>": "Тверь"}) == source


@pytest.mark.asyncio
async def test_invalid_argument_escape_cleans_mapping_without_logging_values(caplog):
    guardrail = _guardrail({"<LOCATION_1>": "Тверь"})
    with pytest.raises(ValueError, match="Invalid JSON escape"):
        await _restore(guardrail, [_arguments_delta('{"city":"private\\q"}')])
    guardrail._redis.delete.assert_awaited_once_with("pii_mapping:stream-test")
    assert "private" not in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize("api", ["chat", "responses"])
async def test_interleaved_tool_calls_keep_ids_and_arguments_separate(api):
    guardrail = _guardrail({"<LOCATION_1>": "Тверь", "<PERSON_1>": "Олег"})
    fragments = [
        (0, '{"city":"<LOC'),
        (1, '{"person":"<PERSON'),
        (0, 'ATION_1>"}'),
        (1, '_1>"}'),
    ]
    if api == "chat":
        events = [
            _chat(
                {
                    "tool_calls": [
                        {
                            "index": index,
                            "id": f"call-{index}" if sequence < 2 else None,
                            "function": {
                                "name": f"tool-{index}" if sequence < 2 else None,
                                "arguments": piece,
                            },
                        }
                    ]
                }
            )
            for sequence, (index, piece) in enumerate(fragments)
        ]
        events.append(_chat({}, finish="tool_calls"))
    else:
        events = [
            _arguments_delta(piece, output=index, sequence=sequence)
            for sequence, (index, piece) in enumerate(fragments)
        ]
    restored = await _restore(guardrail, events)
    arguments = {0: "", 1: ""}
    if api == "chat":
        calls = [
            call
            for event in restored
            for choice in event.choices
            for call in choice.delta.tool_calls or []
        ]
        assert [call.id for call in calls if call.id] == ["call-0", "call-1"]
        assert [call.function.name for call in calls if call.function.name] == [
            "tool-0",
            "tool-1",
        ]
        for call in calls:
            arguments[call.index] += call.function.arguments
    else:
        for event in restored:
            assert event["item_id"] == f"fc-{event['output_index']}"
            arguments[event["output_index"]] += event["delta"]
    assert json.loads(arguments[0]) == {"city": "Тверь"}
    assert json.loads(arguments[1]) == {"person": "Олег"}


@pytest.mark.asyncio
async def test_chat_multiple_choices_and_legacy_function_call():
    guardrail = _guardrail({"<LOCATION_1>": "Тверь", "<PERSON_1>": "Олег"})
    events = [
        _chat(
            {
                "content": "<LOC",
                "function_call": {"name": "legacy", "arguments": '{"city":"<LOC'},
            },
            choice=0,
        ),
        _chat({"content": "<PERSON_1>"}, choice=1),
        _chat(
            {"content": "ATION_1>", "function_call": {"arguments": 'ATION_1>"}'}},
            choice=0,
        ),
        _chat({}, choice=1, finish="stop"),
        _chat({}, choice=0, finish="function_call"),
    ]
    restored = await _restore(guardrail, events)
    text = {0: "", 1: ""}
    arguments = ""
    for event in restored:
        for choice in event.choices:
            text[choice.index] += choice.delta.content or ""
            function = choice.delta.function_call
            if function:
                arguments += _field(function, "arguments") or ""
    assert text == {0: "Тверь", 1: "Олег"}
    assert json.loads(arguments) == {"city": "Тверь"}


def _response(output, status="completed"):
    return {
        "id": "resp-test",
        "object": "response",
        "created_at": 1,
        "status": status,
        "model": "mock-chat",
        "output": output,
        "parallel_tool_calls": True,
        "tool_choice": "auto",
        "tools": [],
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("typed", [False, True])
async def test_native_done_events_and_final_snapshot_restore_without_duplicate_text(
    typed,
):
    guardrail = _guardrail({"<LOCATION_1>": "Тверь"})
    part = {"type": "output_text", "text": "<LOCATION_1>", "annotations": []}
    item = {
        "type": "message",
        "id": "msg-test",
        "role": "assistant",
        "status": "completed",
        "content": [part],
    }
    events = [
        {
            "type": "response.output_text.delta",
            "sequence_number": 0,
            "output_index": 0,
            "content_index": 0,
            "item_id": "msg-test",
            "delta": "<LOCATION_1>",
            "logprobs": [],
        },
        {
            "type": "response.output_text.done",
            "sequence_number": 1,
            "output_index": 0,
            "content_index": 0,
            "item_id": "msg-test",
            "text": "<LOCATION_1>",
            "logprobs": [],
        },
        {
            "type": "response.content_part.done",
            "sequence_number": 2,
            "output_index": 0,
            "content_index": 0,
            "item_id": "msg-test",
            "part": part,
        },
        {
            "type": "response.output_item.done",
            "sequence_number": 3,
            "output_index": 0,
            "item": item,
        },
        {
            "type": "response.completed",
            "sequence_number": 4,
            "response": _response([item]),
        },
    ]
    events = copy.deepcopy(events)
    if typed:
        adapter = TypeAdapter(ResponseStreamEvent)
        events = [adapter.validate_python(event) for event in events]
    restored = await _restore(guardrail, events)
    documents = [event.model_dump() if typed else event for event in restored]
    assert "".join(event.get("delta", "") for event in documents) == "Тверь"
    assert documents[1]["text"] == "Тверь"
    assert documents[2]["part"]["text"] == "Тверь"
    assert documents[3]["item"]["content"][0]["text"] == "Тверь"
    assert documents[4]["response"]["output"][0]["content"][0]["text"] == "Тверь"
    assert [event["sequence_number"] for event in documents] == list(range(5))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "terminal", ["response.completed", "response.incomplete", "response.failed"]
)
async def test_response_flush_uses_native_delta_and_preserves_terminal_status(terminal):
    guardrail = _guardrail({"<LOCATION_1>": "Тверь"})
    events = [
        {
            "type": "response.output_text.delta",
            "sequence_number": 0,
            "output_index": 0,
            "content_index": 0,
            "item_id": "msg-test",
            "delta": "unknown <LOC",
            "logprobs": [],
        },
        {
            "type": terminal,
            "sequence_number": 1,
            "response": _response([], terminal.split(".")[-1]),
        },
    ]
    restored = await _restore(guardrail, events)
    assert "".join(event.get("delta", "") for event in restored) == "unknown <LOC"
    assert [event["type"] for event in restored] == [
        "response.output_text.delta",
        "response.output_text.delta",
        terminal,
    ]
    assert [event["sequence_number"] for event in restored] == [0, 1, 2]
    assert restored[-1]["response"]["status"] == terminal.split(".")[-1]


@pytest.mark.asyncio
async def test_stream_close_cleans_mapping_and_closes_upstream_without_success():
    guardrail = _guardrail({"<LOCATION_1>": "Тверь"})
    closed = asyncio.Event()

    async def upstream():
        try:
            yield _chat({"content": "first"})
            pytest.fail("Cancellation must not consume another upstream event")
        finally:
            closed.set()

    stream = guardrail.async_post_call_streaming_iterator_hook(
        user_api_key_dict=MagicMock(),
        response=upstream(),
        request_data={"metadata": {"pii_request_id": "cancel-test"}},
    )
    await anext(stream)
    guardrail._redis.delete.assert_not_awaited()
    await stream.aclose()
    assert closed.is_set()
    guardrail._redis.delete.assert_awaited_once_with("pii_mapping:cancel-test")


@pytest.mark.asyncio
async def test_concurrent_requests_do_not_share_restoration_state_or_log_values(caplog):
    async def request(original, request_id):
        guardrail = _guardrail({"<LOCATION_1>": original})

        async def upstream():
            yield _arguments_delta('{"city":"<LOC', sequence=0)
            await asyncio.sleep(0)
            yield _arguments_delta('ATION_1>"}', sequence=1)

        events = [
            event
            async for event in guardrail.async_post_call_streaming_iterator_hook(
                user_api_key_dict=MagicMock(),
                response=upstream(),
                request_data={"metadata": {"pii_request_id": request_id}},
            )
        ]
        guardrail._redis.delete.assert_awaited_once_with(f"pii_mapping:{request_id}")
        return json.loads("".join(event["delta"] for event in events))

    results = await asyncio.gather(request("Тверь", "one"), request("Москва", "two"))
    assert results == [{"city": "Тверь"}, {"city": "Москва"}]
    assert "Тверь" not in caplog.text
    assert "Москва" not in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize("typed", [False, True])
async def test_argument_prefix_in_added_item_continues_in_delta(typed):
    guardrail = _guardrail({"<LOCATION_1>": "Тверь"})
    item = {
        "type": "function_call",
        "id": "fc-0",
        "call_id": "call-test",
        "name": "get_city_code",
        "arguments": '{"city":"<LOC',
    }
    events = [
        {
            "type": "response.output_item.added",
            "sequence_number": 0,
            "output_index": 0,
            "item": item,
        },
        _arguments_delta('ATION_1>"}', sequence=1),
        {
            "type": "response.function_call_arguments.done",
            "sequence_number": 2,
            "output_index": 0,
            "item_id": "fc-0",
            "name": "get_city_code",
            "arguments": '{"city":"<LOCATION_1>"}',
        },
    ]
    if typed:
        adapter = TypeAdapter(ResponseStreamEvent)
        events = [adapter.validate_python(event) for event in events]
    restored = await _restore(guardrail, events)
    added = _field(_field(restored[0], "item"), "arguments")
    delta = "".join(_field(event, "delta") or "" for event in restored)
    assert json.loads(added + delta) == {"city": "Тверь"}
    assert json.loads(_field(restored[-1], "arguments")) == {"city": "Тверь"}


@pytest.mark.asyncio
async def test_close_cancellation_does_not_skip_mapping_cleanup():
    guardrail = _guardrail({"<LOCATION_1>": "Тверь"})

    class Upstream:
        def __aiter__(self):
            return self

        async def __anext__(self):
            return _chat({"content": "first"})

        async def aclose(self):
            raise asyncio.CancelledError()

    stream = guardrail.async_post_call_streaming_iterator_hook(
        user_api_key_dict=MagicMock(),
        response=Upstream(),
        request_data={"metadata": {"pii_request_id": "cancel-close-test"}},
    )
    await anext(stream)
    with pytest.raises(asyncio.CancelledError):
        await stream.aclose()
    guardrail._redis.delete.assert_awaited_once_with("pii_mapping:cancel-close-test")


@pytest.mark.asyncio
@pytest.mark.parametrize("mapping", [{"<LOCATION_1>": "Тверь"}, {}])
async def test_responses_cancellation_closes_http_response_even_without_mapping(
    mapping,
):
    import httpx

    guardrail = _guardrail(mapping)

    class Upstream:
        response = httpx.Response(200)

        def __aiter__(self):
            return self

        async def __anext__(self):
            return _arguments_delta('{"city":"<LOC')

    upstream = Upstream()
    upstream.response.is_closed = False
    stream = guardrail.async_post_call_streaming_iterator_hook(
        user_api_key_dict=MagicMock(),
        response=upstream,
        request_data={"metadata": {"pii_request_id": "cancel-http-test"}},
    )
    await anext(stream)
    await stream.aclose()
    assert upstream.response.is_closed
    if mapping:
        guardrail._redis.delete.assert_awaited_once_with("pii_mapping:cancel-http-test")
    else:
        guardrail._redis.delete.assert_not_awaited()
