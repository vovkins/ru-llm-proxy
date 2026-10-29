"""Deterministic guardrail flow test without external LLM calls."""

import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import litellm
import pytest
import pytest_asyncio
from litellm.litellm_core_utils.logging_worker import GLOBAL_LOGGING_WORKER
from litellm.llms.custom_httpx.http_handler import AsyncHTTPHandler
from litellm.exceptions import MidStreamFallbackError
from openai import AsyncOpenAI
from pydantic import ValidationError

from litellm_guardrails.pii_guardrail import RuPIIGuardrail
from presidio.entity_types import SUPPORTED_ENTITY_TYPES

DKB_CORPUS = Path(__file__).resolve().parents[1] / "fixtures/dkb"
DKB_CASES = json.loads((DKB_CORPUS / "manifest.json").read_text())
NER_VALUE_CASES = json.loads((Path(__file__).resolve().parents[1] / "fixtures/ner-value-repair.json").read_text())


@pytest_asyncio.fixture
async def llm_logging_lifecycle():
    yield
    # Drain SDK callbacks before pytest closes this test's event loop.
    await asyncio.sleep(0)
    if GLOBAL_LOGGING_WORKER._queue is not None:
        await asyncio.wait_for(GLOBAL_LOGGING_WORKER._queue.join(), timeout=5)
    await GLOBAL_LOGGING_WORKER.stop()


class _WireStream(httpx.AsyncByteStream):
    def __init__(self, wire):
        self.wire = wire
        self.closed = False

    async def __aiter__(self):
        # Transport boundaries deliberately differ from SSE event boundaries.
        for index in range(0, len(self.wire), 17):
            yield self.wire[index : index + 17]

    async def aclose(self):
        self.closed = True


@pytest.mark.asyncio
@pytest.mark.parametrize("repeat_count", [99, 102])
@pytest.mark.parametrize("protected", [False, True])
async def test_native_repeated_chunk_limit_is_not_success(
    repeat_count, protected, llm_logging_lifecycle
):
    assert litellm.REPEATED_STREAMING_CHUNK_LIMIT == 100
    events = [
        {
            "id": "repeat-stream", "object": "chat.completion.chunk",
            "created": 1, "model": "gpt-4o-mini",
            "choices": [{"index": 0, "delta": {"content": " " * 128},
                         "finish_reason": None}],
        }
        for _ in range(repeat_count)
    ]
    events.append({
        "id": "repeat-stream", "object": "chat.completion.chunk", "created": 1,
        "model": "gpt-4o-mini",
        "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
    })
    wire = "".join("data: " + json.dumps(event) + "\n\n" for event in events)
    wire += "data: [DONE]\n\n"
    transport = _WireStream(wire.encode())
    chunks = []
    def provider(_request):
        return httpx.Response(
            200, headers={"Content-Type": "text/event-stream"}, stream=transport
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as http_client:
        client = AsyncOpenAI(
            api_key="synthetic-test-key", base_url="http://mock.invalid/v1",
            http_client=http_client,
        )
        response = await litellm.acompletion(
            model="openai/gpt-4o-mini", messages=[{"role": "user", "content": "test"}],
            stream=True, client=client,
        )
        if protected:
            guardrail = RuPIIGuardrail()
            guardrail._redis = AsyncMock()
            guardrail._redis.get.return_value = None
            response = guardrail.async_post_call_streaming_iterator_hook(
                user_api_key_dict=MagicMock(), response=response,
                request_data={
                    "stream": True, "metadata": {},
                    "messages": [{"role": "user", "content": "test"}],
                },
            )
        # LiteLLM checks already accumulated chunks before adding the next one.
        if repeat_count == 102:
            with pytest.raises(
                MidStreamFallbackError, match="repeating the same chunk"
            ) as raised:
                async for chunk in response:
                    chunks.append(chunk)
            assert isinstance(raised.value.original_exception, litellm.InternalServerError)
            assert not any(c.finish_reason for chunk in chunks for c in chunk.choices)
        else:
            async for chunk in response:
                chunks.append(chunk)
            choices = [c for chunk in chunks for c in chunk.choices]
            assert sum(c.finish_reason == "stop" for c in choices) == 1
            assert "".join(c.delta.content or "" for c in choices) == " " * (128 * repeat_count)
        await client.close()
        await transport.aclose()
    assert transport.closed


@pytest.mark.asyncio
@pytest.mark.parametrize("api", ["chat", "responses"])
@pytest.mark.parametrize("output_kind", ["text", "arguments", "code"])
@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize(
    ("replacement", "entity_type"),
    [
        pytest.param("pii", entity, id=entity)
        for entity in sorted(SUPPORTED_ENTITY_TYPES)
    ]
    + [pytest.param("dictionary", None, id="dictionary")]
    + [pytest.param("long-base64", "BASE64_DATA", id="long-base64")]
    + [pytest.param("long-base64", "PASSWORD", id="long-password-base64")]
    + [pytest.param("dkb", case, id="dkb-" + case["file"]) for case in DKB_CASES],
)
async def test_round_trip_through_litellm_parser(
    api, output_kind, stream, replacement, entity_type, llm_logging_lifecycle
):
    await _round_trip_through_litellm_parser(
        api, output_kind, stream, replacement, entity_type
    )


# These expected spans are also verified against the actual Analyzer
# postprocessor; the transport mock alone cannot establish NER correctness.
@pytest.mark.asyncio
@pytest.mark.parametrize("api", ["chat", "responses"])
@pytest.mark.parametrize("output_kind", ["text", "arguments"])
@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("case", NER_VALUE_CASES, ids=lambda c: c["id"])
async def test_repaired_ner_values_round_trip(api, output_kind, stream, case, llm_logging_lifecycle):
    await _round_trip_through_litellm_parser(api, output_kind, stream, "ner-value", case)


@pytest.mark.asyncio
@pytest.mark.parametrize("api", ["chat", "responses"])
@pytest.mark.parametrize("stream", [False, True])
async def test_uri_component_only_mock_still_respects_final_leak_policy(api, stream, llm_logging_lifecycle):
    case = next(case for case in NER_VALUE_CASES if case["id"] == "uri-components")
    incomplete = {key: value for key, value in case.items() if key != "analyzer_expected"}
    with pytest.raises(litellm.UnprocessableEntityError, match="confirmed raw leak marker"):
        await _round_trip_through_litellm_parser(api, "text", stream, "ner-value", incomplete)


MODEL_RESPONSE_CASES = [
    pytest.param("bare", "{value}", "{value}", True, id="bare-marker"),
    pytest.param("label", "Base64: {value}", "Base64: {value}", True, id="label-kept"),
    pytest.param("label", "{value}", "{value}", False, id="label-deleted"),
    pytest.param(
        "bare", "Это шаблон: {value}. Пришлите настоящий адрес.",
        "Это шаблон: {value}. Пришлите настоящий адрес.", False,
        id="marker-in-explanation",
    ),
    pytest.param("bare", "<email_address_1>", "<email_address_1>", False, id="changed-marker"),
    pytest.param(
        "bare", "&lt;EMAIL_ADDRESS_1&gt;", "&lt;EMAIL_ADDRESS_1&gt;", False,
        id="html-escaped-marker",
    ),
    pytest.param("bare", "Метка удалена.", "Метка удалена.", False, id="deleted-marker"),
    pytest.param("bare", "<EMAIL_ADDRESS_99>", "<EMAIL_ADDRESS_99>", False, id="unknown-marker"),
    pytest.param("repeat", "{value} / {value}", "{value} / {value}", True, id="repeated-marker"),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("api", ["chat", "responses"])
@pytest.mark.parametrize("output_kind", ["text", "arguments"])
@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize(
    ("instruction", "provider_template", "client_template", "instruction_obeyed"),
    MODEL_RESPONSE_CASES,
)
async def test_model_response_behavior_does_not_change_restoration_contract(
    api, output_kind, stream, instruction, provider_template, client_template,
    instruction_obeyed, llm_logging_lifecycle,
):
    await _round_trip_through_litellm_parser(
        api, output_kind, stream, "behavior", "EMAIL_ADDRESS",
        behavior=(instruction, provider_template, client_template, instruction_obeyed),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("api", ["chat", "responses"])
@pytest.mark.parametrize("stream", [False, True])
async def test_model_tool_refusal_is_not_a_successful_tool_call(
    api, stream, llm_logging_lifecycle,
):
    explanation = "Это шаблон: {value}. Пришлите настоящий адрес."
    await _round_trip_through_litellm_parser(
        api, "text", stream, "behavior", "EMAIL_ADDRESS",
        behavior=("bare", explanation, explanation, False), require_tool=True,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("output_kind", ["text", "arguments"])
@pytest.mark.parametrize("replacement,entity_type", [
    ("pii", "EMAIL_ADDRESS"), ("pii", "BASE64_DATA"),
    ("dictionary", None), ("long-base64", "BASE64_DATA"),
])
async def test_stock_responses_chat_bridge_preserves_restoration(
    stream, output_kind, replacement, entity_type, llm_logging_lifecycle,
):
    await _round_trip_through_litellm_parser(
        "responses", output_kind, stream, replacement, entity_type, bridge=True,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("output_kind", ["text", "arguments"])
@pytest.mark.parametrize(
    "instruction,provider_template,client_template,instruction_obeyed",
    MODEL_RESPONSE_CASES,
)
async def test_stock_responses_chat_bridge_does_not_guess_model_output(
    stream, output_kind, instruction, provider_template, client_template,
    instruction_obeyed, llm_logging_lifecycle,
):
    await _round_trip_through_litellm_parser(
        "responses", output_kind, stream, "behavior", "EMAIL_ADDRESS", bridge=True,
        behavior=(instruction, provider_template, client_template, instruction_obeyed),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True])
async def test_stock_responses_chat_bridge_keeps_tool_refusal(
    stream, llm_logging_lifecycle,
):
    explanation = "Это шаблон: {value}. Пришлите настоящий адрес."
    call = _round_trip_through_litellm_parser(
        "responses", "text", stream, "behavior", "EMAIL_ADDRESS", bridge=True,
        behavior=("bare", explanation, explanation, False), require_tool=True,
    )
    if stream:
        # LiteLLM 1.98.0 puts Chat-shaped tool_choice into response.created.
        # Record the platform limitation, not a successful tool invocation.
        with pytest.raises(ValidationError) as failure:
            await call
        assert all(error["loc"][0] == "tool_choice" for error in failure.value.errors())
    else:
        await call


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True])
async def test_stock_responses_chat_bridge_keeps_required_tool_refusal(
    stream, llm_logging_lifecycle,
):
    explanation = "Это шаблон: {value}. Пришлите настоящий адрес."
    await _round_trip_through_litellm_parser(
        "responses", "text", stream, "behavior", "EMAIL_ADDRESS", bridge=True,
        behavior=("bare", explanation, explanation, False), require_tool=True,
        required_tool_choice=True,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("full_history", [False, True])
async def test_stock_responses_chat_bridge_history_without_persisted_history(
    full_history, llm_logging_lifecycle,
):
    first_text = "First turn: <EMAIL_ADDRESS_1>"
    next_text = "Return the contact from the first turn."
    requests = []

    def provider(request):
        assert request.url.path == "/v1/chat/completions"
        requests.append(json.loads(request.content))
        return httpx.Response(200, json={
            "id": "chat-history", "object": "chat.completion", "created": 1,
            "model": "gpt-4o-mini", "choices": [{
                "index": 0, "message": {"role": "assistant", "content": "ok"},
                "finish_reason": "stop",
            }],
        })

    async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as http_client:
        client = AsyncOpenAI(
            api_key="synthetic-test-key", base_url="http://mock.invalid/v1",
            http_client=http_client,
        )
        options = {
            "model": "openai/gpt-4o-mini", "client": client,
            "api_key": "synthetic-test-key", "api_base": "http://mock.invalid/v1",
            "use_chat_completions_api": True,
        }
        with patch(
            "litellm.responses.litellm_completion_transformation.session_handler."
            "ResponsesSessionHandler.get_all_spend_logs_for_previous_response_id",
            new=AsyncMock(return_value=[]),
        ):
            first = await litellm.aresponses(input=first_text, **options)
            input_items = [
                {"role": "user", "content": first_text},
                {"role": "assistant", "content": "ok"},
                {"role": "user", "content": next_text},
            ]
            second = await litellm.aresponses(
                input=input_items if full_history else next_text,
                **({} if full_history else {"previous_response_id": first.id}),
                **options,
            )
    assert second.status == "completed"
    assert len(requests) == 2
    if full_history:
        assert requests[1]["messages"] == input_items
    else:
        # No history is recovered, yet LiteLLM accepts the continuation.
        assert requests[1]["messages"] == [{"role": "user", "content": next_text}]
        assert first_text not in json.dumps(requests[1])


async def _round_trip_through_litellm_parser(
    api, output_kind, stream, replacement, entity_type, *, behavior=None,
    require_tool=False, bridge=False, required_tool_choice=False, analyzer=None,
):
    provider_api = "chat" if bridge else api
    dictionary = replacement == "dictionary"
    corpus_case = entity_type if replacement in {"dkb", "ner-value"} else None
    guardrail = RuPIIGuardrail(
        pre_egress_policy_mode="off" if replacement == "dkb" else None,
        final_payload_leak_check_mode="off" if replacement == "dkb" else None,
        dictionary_substitutions_enabled=dictionary,
        dictionary_substitutions_file="",
        dictionary_substitutions_json=(
            json.dumps(
                {
                    "substitutions": [
                        {
                            "id": "test-bank",
                            "source": "Т-Банк",
                            "replacement": "Зетта Групп",
                            "restore": True,
                        }
                    ],
                }
            )
            if dictionary
            else ""
        ),
    )
    store = {}
    redis = AsyncMock()

    async def setex(key, ttl, value):
        store[key] = value

    async def delete(key):
        store.pop(key, None)

    redis.setex.side_effect = setex
    redis.get.side_effect = store.get
    redis.delete.side_effect = delete
    guardrail._redis = redis
    original = "Т-Банк" if dictionary else 'Москва "центр"\nC:\\work\\main.py\t😀'
    if replacement == "dkb":
        original = (DKB_CORPUS / corpus_case["file"]).read_bytes().decode("utf-8")
    elif replacement == "ner-value":
        original = corpus_case["text"]
        corpus_case = {**corpus_case, "expected": [
            {"start": original.index(value), "end": original.index(value) + len(value), "entities": [kind]}
            for value, kind in corpus_case.get("analyzer_expected", corpus_case["expected"])
        ]}
    if replacement == "long-base64":
        original = "A" * 70000 + "\n    " + "B" * 70000
    expected = original
    strict_expected = None
    if behavior:
        instruction, provider_template, client_template, instruction_obeyed = behavior
        if instruction == "label":
            original = "A" * 15000
            entity_type = "BASE64_DATA"
        strict_expected = {
            "bare": original, "label": "Base64: " + original,
            "repeat": original + " / " + original,
        }[instruction]
        expected = client_template.format(value=original)
    if output_kind == "code":
        if not corpus_case and replacement != "long-base64":
            original = "Т-Банк" if dictionary else "Москва"
        expected = f'let city = "{original}"\nlet path = "C:\\work"\nprint(city)\n'
    prompt = "Верни без изменений: " + (strict_expected or expected)
    data = (
        {"messages": [{"role": "user", "content": prompt}]}
        if api == "chat"
        else {"input": prompt}
    )
    data["stream"] = stream
    tool_options = {}
    if require_tool:
        function = {
            "name": "get_city_code", "description": "Store the exact synthetic value",
            "parameters": {
                "type": "object", "properties": {"city": {"type": "string"}},
                "required": ["city"], "additionalProperties": False,
            },
        }
        tool_options = {
            "tools": [{"type": "function", "function": function}]
            if api == "chat" else [{"type": "function", **function}],
            "tool_choice": {"type": "function", "function": {"name": "get_city_code"}}
            if api == "chat" else {"type": "function", "name": "get_city_code"},
        }
        if required_tool_choice:
            tool_options["tool_choice"] = "required"
        data.update(tool_options)
    entities = [] if dictionary else [_entity(prompt, original, entity_type)]
    if behavior and instruction == "repeat":
        start = prompt.rindex(original)
        entities.append({
            "entity_type": entity_type, "start": start,
            "end": start + len(original), "score": 1.0,
        })
    if corpus_case:
        offset = prompt.index(original)
        entities = [
            {"entity_type": span["entities"][0], "start": offset + span["start"], "end": offset + span["end"], "score": 1.0}
            for span in corpus_case["expected"]
        ]
    with patch.object(
        guardrail,
        "_analyze_text",
        **({"side_effect": analyzer} if analyzer is not None else {"return_value": entities}),
    ):
        masked = await guardrail.async_pre_call_hook(MagicMock(), MagicMock(), data)
    provider_value = "Зетта Групп" if dictionary else f"<{entity_type}_1>"
    mapping_id = masked["metadata"]["pii_request_id"]
    mapping = json.loads(store[f"pii_mapping:{mapping_id}"])
    if corpus_case:
        assert set(mapping.values()) == {original[span["start"]:span["end"]] for span in corpus_case["expected"]}
        provider_value = next(iter(mapping))
        provider_text = masked["messages"][0]["content"] if api == "chat" else masked["input"]
        upstream_text = provider_text[len("Верни без изменений: "):]
    else:
        expected_mapping = {provider_value: original}
        if behavior and instruction == "repeat":
            expected_mapping[f"<{entity_type}_2>"] = original
        assert mapping == expected_mapping
        upstream_text = expected.replace(original, provider_value)
    if behavior:
        upstream_text = provider_template.format(value=provider_value)
    upstream_output = (
        json.dumps({"city": upstream_text})
        if output_kind == "arguments"
        else upstream_text
    )
    pieces = [
        upstream_output[index : index + 3]
        for index in range(0, len(upstream_output), 3)
    ]
    events = []
    if provider_api == "chat":
        for index, piece in enumerate(pieces):
            delta = {"content": piece}
            if output_kind == "arguments":
                delta = {
                    "tool_calls": [
                        {
                            "index": 0,
                            "type": "function",
                            "function": {"arguments": piece},
                        }
                    ]
                }
                if index == 0:
                    delta["tool_calls"][0].update(id="call-wire")
                    delta["tool_calls"][0]["function"]["name"] = "get_city_code"
            events.append(
                {
                    "id": "chat-wire",
                    "object": "chat.completion.chunk",
                    "created": 1,
                    "model": "gpt-4o-mini",
                    "choices": [{"index": 0, "delta": delta, "finish_reason": None}],
                }
            )
        events.append(
            {
                "id": "chat-wire",
                "object": "chat.completion.chunk",
                "created": 1,
                "model": "gpt-4o-mini",
                "choices": [
                    {
                        "index": 0,
                        "delta": {},
                        "finish_reason": (
                            "tool_calls" if output_kind == "arguments" else "stop"
                        ),
                    }
                ],
            }
        )
    else:
        kind = (
            "response.function_call_arguments"
            if output_kind == "arguments"
            else "response.output_text"
        )
        for index, piece in enumerate(pieces):
            event = {
                "type": kind + ".delta",
                "item_id": "item-wire",
                "output_index": 0,
                "sequence_number": index,
                "delta": piece,
            }
            if output_kind != "arguments":
                event.update(content_index=0, logprobs=[])
            events.append(event)
        done = {
            "type": kind + ".done",
            "item_id": "item-wire",
            "output_index": 0,
            "sequence_number": len(events),
        }
        if output_kind == "arguments":
            done.update(arguments=upstream_output, name="get_city_code")
            item = {
                "type": "function_call",
                "id": "item-wire",
                "call_id": "call-wire",
                "name": "get_city_code",
                "arguments": upstream_output,
                "status": "completed",
            }
        else:
            done.update(text=upstream_output, content_index=0, logprobs=[])
            item = {
                "type": "message",
                "id": "item-wire",
                "role": "assistant",
                "status": "completed",
                "content": [
                    {"type": "output_text", "text": upstream_output, "annotations": []}
                ],
            }
        events.append(done)
        events.append(
            {
                "type": "response.output_item.done",
                "output_index": 0,
                "sequence_number": len(events),
                "item": item,
            }
        )
        events.append(
            {
                "type": "response.completed",
                "sequence_number": len(events),
                "response": {
                    "id": "resp-wire",
                    "object": "response",
                    "created_at": 1,
                    "model": "gpt-4o-mini",
                    "status": "completed",
                    "output": [item],
                    "parallel_tool_calls": True,
                    "tool_choice": "auto",
                    "tools": [],
                },
            }
        )
    wire = "".join("data: " + json.dumps(event) + "\n\n" for event in events)
    if provider_api == "chat":
        wire += "data: [DONE]\n\n"
    transport_stream = _WireStream(wire.encode())
    requests = []

    def provider(request):
        assert request.url.path == (
            "/v1/chat/completions" if provider_api == "chat" else "/v1/responses"
        )
        body = json.loads(request.content)
        requests.append(body)
        assert body.get("stream", False) is stream
        if require_tool:
            expected_tool_choice = (
                {"type": "function", "function": {"name": "get_city_code"}}
                if provider_api == "chat" and not required_tool_choice
                else tool_options["tool_choice"]
            )
            assert body["tool_choice"] == expected_tool_choice
        sent = body["messages"][0]["content"] if provider_api == "chat" else body["input"]
        if bridge:
            assert "input" not in body
            assert "use_chat_completions_api" not in body
            assert body["model"] == "gpt-4o-mini"
        assert original not in sent
        assert provider_value in sent
        if corpus_case:
            assert all(value not in sent for value in mapping.values())
        if not stream:
            if provider_api == "chat":
                message = {"role": "assistant", "content": upstream_output}
                if output_kind == "arguments":
                    message.update(
                        content=None,
                        tool_calls=[
                            {
                                "id": "call-wire",
                                "type": "function",
                                "function": {
                                    "name": "get_city_code",
                                    "arguments": upstream_output,
                                },
                            }
                        ],
                    )
                document = {
                    "id": "chat-wire",
                    "object": "chat.completion",
                    "created": 1,
                    "model": "gpt-4o-mini",
                    "choices": [
                        {
                            "index": 0,
                            "message": message,
                            "finish_reason": (
                                "tool_calls" if output_kind == "arguments" else "stop"
                            ),
                        }
                    ],
                }
            else:
                document = events[-1]["response"]
            return httpx.Response(200, json=document)
        return httpx.Response(
            200, headers={"Content-Type": "text/event-stream"}, stream=transport_stream
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(provider)
    ) as http_client:
        if api == "chat":
            client = AsyncOpenAI(
                api_key="synthetic-test-key",
                base_url="http://mock.invalid/v1",
                http_client=http_client,
            )
            response = await litellm.acompletion(
                model="openai/gpt-4o-mini",
                messages=masked["messages"],
                stream=stream,
                client=client,
                **tool_options,
            )
        else:
            if bridge:
                client = AsyncOpenAI(
                    api_key="synthetic-test-key", base_url="http://mock.invalid/v1",
                    http_client=http_client,
                )
            else:
                client = AsyncHTTPHandler()
                await client.client.aclose()
                client.client = http_client
            response = await litellm.aresponses(
                model="openai/gpt-4o-mini",
                input=masked["input"],
                stream=stream,
                api_key="synthetic-test-key",
                api_base="http://mock.invalid/v1",
                client=client,
                **({"use_chat_completions_api": True} if bridge else {}),
                **tool_options,
            )
        if stream:
            restored = [
                event
                async for event in guardrail.async_post_call_streaming_iterator_hook(
                    MagicMock(), response, masked
                )
            ]
        else:
            await guardrail.async_post_call_success_hook(masked, MagicMock(), response)
            restored = [response]

    assert len(requests) == 1
    if stream:
        assert transport_stream.closed
    assert not store
    redis.delete.assert_awaited_once_with(f"pii_mapping:{mapping_id}")
    if api == "chat":
        if output_kind == "arguments":
            output = "".join(
                call.function.arguments or ""
                for event in restored
                for choice in event.choices
                for call in (choice.delta if stream else choice.message).tool_calls
                or []
            )
            assert json.loads(output) == {"city": expected}
        else:
            output = "".join(
                (choice.delta if stream else choice.message).content or ""
                for event in restored
                for choice in event.choices
            )
            assert output.encode() == expected.encode()
        assert [
            choice.finish_reason
            for event in restored
            for choice in event.choices
            if choice.finish_reason
        ] == ["tool_calls" if output_kind == "arguments" else "stop"]
    else:
        documents = [event.model_dump() for event in restored]
        final_response = documents[-1]["response"] if stream else documents[0]
        final_items = [
            item for item in final_response["output"]
            if item["type"] == ("function_call" if output_kind == "arguments" else "message")
        ]
        assert len(final_items) == 1
        final_item = final_items[0]
        output = (
            "".join(event.get("delta", "") for event in documents)
            if stream
            else (
                final_item["arguments"]
                if output_kind == "arguments"
                else final_item["content"][0]["text"]
            )
        )
        if output_kind == "arguments":
            assert json.loads(output) == {"city": expected}
            assert json.loads(final_item["arguments"]) == json.loads(output)
        else:
            assert output.encode() == expected.encode()
            assert final_item["content"][0]["text"] == output
        assert final_response["status"] == "completed"
    if behavior:
        instruction_result = json.loads(output)["city"] if output_kind == "arguments" else output
        assert (instruction_result.encode() == strict_expected.encode()) is instruction_obeyed
    if require_tool:
        if api == "chat":
            assert not any(
                (choice.delta if stream else choice.message).tool_calls
                for event in restored for choice in event.choices
            )
        else:
            assert not any(item["type"] == "function_call" for item in final_response["output"])


def _entity(text, value, entity_type):
    start = text.index(value)
    return {
        "entity_type": entity_type,
        "start": start,
        "end": start + len(value),
        "score": 1.0,
        "text": value,
    }


async def _stream_chunks(chunks):
    for chunk in chunks:
        yield chunk


async def _collect_stream_content(stream):
    parts = []
    async for chunk in stream:
        for choice in chunk.choices:
            content = getattr(choice.delta, "content", None)
            if isinstance(content, str):
                parts.append(content)
    return "".join(parts)


@pytest.mark.asyncio
async def test_guardrail_masks_before_model_and_unmasks_after_model():
    guardrail = RuPIIGuardrail()
    redis_store = {}
    redis = AsyncMock()

    async def setex(key, ttl, value):
        redis_store[key] = value

    async def get(key):
        return redis_store.get(key)

    async def delete(key):
        redis_store.pop(key, None)

    redis.setex.side_effect = setex
    redis.get.side_effect = get
    redis.delete.side_effect = delete
    guardrail._redis = redis

    user_text = "Клиент Иванов Иван, телефон +79031234567, ИНН 7707083893"
    data = {
        "litellm_call_id": "flow-1",
        "messages": [{"role": "user", "content": user_text}],
    }
    analyzer_results = [
        _entity(user_text, "Иванов Иван", "PERSON"),
        _entity(user_text, "+79031234567", "PHONE_NUMBER"),
        _entity(user_text, "7707083893", "RU_INN"),
    ]

    with patch.object(guardrail, "_analyze_text", return_value=analyzer_results):
        masked_data = await guardrail.async_pre_call_hook(
            user_api_key_dict=MagicMock(),
            cache=MagicMock(),
            data=data,
        )

    masked_prompt = masked_data["messages"][0]["content"]
    assert "Иванов Иван" not in masked_prompt
    assert "+79031234567" not in masked_prompt
    assert "7707083893" not in masked_prompt
    assert "<PERSON_1>" in masked_prompt
    assert "<PHONE_NUMBER_1>" in masked_prompt
    assert "<RU_INN_1>" in masked_prompt

    mapping_id = masked_data["metadata"]["pii_request_id"]
    assert mapping_id != "flow-1"
    saved_mapping = json.loads(redis_store[f"pii_mapping:{mapping_id}"])
    assert saved_mapping == {
        "<PERSON_1>": "Иванов Иван",
        "<PHONE_NUMBER_1>": "+79031234567",
        "<RU_INN_1>": "7707083893",
    }

    response = litellm.ModelResponse(
        id="test",
        choices=[
            litellm.Choices(
                index=0,
                message=litellm.Message(
                    role="assistant",
                    content=(
                        "Справка: <PERSON_1>, телефон <PHONE_NUMBER_1>, "
                        "ИНН <RU_INN_1>."
                    ),
                ),
                finish_reason="stop",
            )
        ],
    )

    await guardrail.async_post_call_success_hook(
        data=masked_data,
        user_api_key_dict=MagicMock(),
        response=response,
    )

    content = response.choices[0].message.content
    assert "Иванов Иван" in content
    assert "+79031234567" in content
    assert "7707083893" in content
    assert "<PERSON_1>" not in content
    assert "<PHONE_NUMBER_1>" not in content
    assert "<RU_INN_1>" not in content
    assert f"pii_mapping:{mapping_id}" not in redis_store


@pytest.mark.asyncio
async def test_guardrail_round_trip_preserves_extended_recognizer_values():
    guardrail = RuPIIGuardrail()
    redis_store = {}
    redis = AsyncMock()

    async def setex(key, ttl, value):
        redis_store[key] = value

    async def get(key):
        return redis_store.get(key)

    async def delete(key):
        redis_store.pop(key, None)

    redis.setex.side_effect = setex
    redis.get.side_effect = get
    redis.delete.side_effect = delete
    guardrail._redis = redis

    user_text = (
        "Паспорт 45 12 №678901, СНИЛС 001-234-567 84, "
        "пользователь ci_runner_12, хост app-prod-01, "
        "госконтракт № 0173100004521000123"
    )
    data = {"messages": [{"role": "user", "content": user_text}]}
    analyzer_results = [
        _entity(user_text, "45 12 №678901", "RU_PASSPORT"),
        _entity(user_text, "001-234-567 84", "RU_SNILS"),
        _entity(user_text, "ci_runner_12", "LOGIN"),
        _entity(user_text, "app-prod-01", "HOSTNAME"),
        _entity(user_text, "0173100004521000123", "CONTRACT_NUMBER"),
    ]

    with patch.object(guardrail, "_analyze_text", return_value=analyzer_results):
        masked_data = await guardrail.async_pre_call_hook(
            user_api_key_dict=MagicMock(),
            cache=MagicMock(),
            data=data,
        )

    masked_prompt = masked_data["messages"][0]["content"]
    for original in (
        "45 12 №678901",
        "001-234-567 84",
        "ci_runner_12",
        "app-prod-01",
        "0173100004521000123",
    ):
        assert original not in masked_prompt
    assert "Паспорт <RU_PASSPORT_1>" in masked_prompt
    assert "СНИЛС <RU_SNILS_1>" in masked_prompt
    assert "пользователь <LOGIN_1>" in masked_prompt
    assert "хост <HOSTNAME_1>" in masked_prompt
    assert "госконтракт № <CONTRACT_NUMBER_1>" in masked_prompt

    response = litellm.ModelResponse(
        id="extended-recognizer-flow",
        choices=[
            litellm.Choices(
                index=0,
                message=litellm.Message(
                    role="assistant",
                    content=(
                        "<RU_PASSPORT_1>; <RU_SNILS_1>; <LOGIN_1>; "
                        "<HOSTNAME_1>; <CONTRACT_NUMBER_1>"
                    ),
                ),
                finish_reason="stop",
            )
        ],
    )
    await guardrail.async_post_call_success_hook(
        data=masked_data,
        user_api_key_dict=MagicMock(),
        response=response,
    )

    restored = response.choices[0].message.content
    for original in (
        "45 12 №678901",
        "001-234-567 84",
        "ci_runner_12",
        "app-prod-01",
        "0173100004521000123",
    ):
        assert original in restored


@pytest.mark.asyncio
async def test_block_mode_rejects_extended_recognizer_values_before_provider():
    guardrail = RuPIIGuardrail(pii_mode="block")
    redis = AsyncMock()
    guardrail._redis = redis
    user_text = "Паспорт 45 12 №678901, пользователь ci_runner_12, " "хост app-prod-01"
    data = {"messages": [{"role": "user", "content": user_text}]}
    analyzer_results = [
        _entity(user_text, "45 12 №678901", "RU_PASSPORT"),
        _entity(user_text, "ci_runner_12", "LOGIN"),
        _entity(user_text, "app-prod-01", "HOSTNAME"),
    ]

    with patch.object(guardrail, "_analyze_text", return_value=analyzer_results):
        with pytest.raises(litellm.UnprocessableEntityError) as exc_info:
            await guardrail.async_pre_call_hook(
                user_api_key_dict=MagicMock(),
                cache=MagicMock(),
                data=data,
            )

    assert data["messages"][0]["content"] == user_text
    redis.setex.assert_not_called()
    error_body = exc_info.value.response.json()
    assert error_body["error"]["code"] == "pii_blocked"
    assert set(error_body["error"]["details"]["entities"]) == {
        "HOSTNAME",
        "LOGIN",
        "RU_PASSPORT",
    }
    serialized = json.dumps(error_body, ensure_ascii=False)
    assert "45 12 №678901" not in serialized
    assert "ci_runner_12" not in serialized
    assert "app-prod-01" not in serialized


@pytest.mark.asyncio
async def test_guardrail_masks_before_model_and_unmasks_streaming_response():
    guardrail = RuPIIGuardrail()
    redis_store = {}
    redis = AsyncMock()

    async def setex(key, ttl, value):
        redis_store[key] = value

    async def get(key):
        return redis_store.get(key)

    async def delete(key):
        redis_store.pop(key, None)

    redis.setex.side_effect = setex
    redis.get.side_effect = get
    redis.delete.side_effect = delete
    guardrail._redis = redis

    user_text = "Клиент Иванов Иван, телефон +79031234567"
    data = {
        "litellm_call_id": "flow-stream-1",
        "stream": True,
        "messages": [{"role": "user", "content": user_text}],
    }
    analyzer_results = [
        _entity(user_text, "Иванов Иван", "PERSON"),
        _entity(user_text, "+79031234567", "PHONE_NUMBER"),
    ]

    with patch.object(guardrail, "_analyze_text", return_value=analyzer_results):
        masked_data = await guardrail.async_pre_call_hook(
            user_api_key_dict=MagicMock(),
            cache=MagicMock(),
            data=data,
        )

    masked_prompt = masked_data["messages"][0]["content"]
    assert "Иванов Иван" not in masked_prompt
    assert "+79031234567" not in masked_prompt
    assert "<PERSON_1>" in masked_prompt
    assert "<PHONE_NUMBER_1>" in masked_prompt

    model_stream = _stream_chunks(
        [
            litellm.ModelResponseStream(
                choices=[
                    litellm.StreamingChoices(
                        index=0,
                        delta={"content": "Справка: <PERSON_1>, телефон <PHONE_"},
                    )
                ]
            ),
            litellm.ModelResponseStream(
                choices=[
                    litellm.StreamingChoices(
                        index=0,
                        delta={"content": "NUMBER_1>."},
                    )
                ]
            ),
            litellm.ModelResponseStream(
                choices=[litellm.StreamingChoices(index=0, finish_reason="stop")]
            ),
        ]
    )

    restored_stream = guardrail.async_post_call_streaming_iterator_hook(
        user_api_key_dict=MagicMock(),
        response=model_stream,
        request_data=masked_data,
    )
    content = await _collect_stream_content(restored_stream)

    assert "Иванов Иван" in content
    assert "+79031234567" in content
    assert "<PERSON_1>" not in content
    assert "<PHONE_NUMBER_1>" not in content
    assert f"pii_mapping:{masked_data['metadata']['pii_request_id']}" not in redis_store


@pytest.mark.asyncio
async def test_client_request_id_collision_does_not_cross_restore_streaming_pii():
    guardrail = RuPIIGuardrail()
    redis_store = {}
    redis = AsyncMock()

    async def setex(key, ttl, value):
        redis_store[key] = value

    async def get(key):
        return redis_store.get(key)

    async def delete(key):
        redis_store.pop(key, None)

    redis.setex.side_effect = setex
    redis.get.side_effect = get
    redis.delete.side_effect = delete
    guardrail._redis = redis

    first_text = "Телефон +79031234567"
    second_text = "Телефон +79037654321"
    first_data = {
        "metadata": {"request_id": "client-controlled"},
        "messages": [{"role": "user", "content": first_text}],
    }
    second_data = {
        "metadata": {"request_id": "client-controlled"},
        "messages": [{"role": "user", "content": second_text}],
    }

    with patch.object(
        guardrail,
        "_analyze_text",
        return_value=[_entity(first_text, "+79031234567", "PHONE_NUMBER")],
    ):
        first_masked_data = await guardrail.async_pre_call_hook(
            user_api_key_dict=MagicMock(),
            cache=MagicMock(),
            data=first_data,
        )
    with patch.object(
        guardrail,
        "_analyze_text",
        return_value=[_entity(second_text, "+79037654321", "PHONE_NUMBER")],
    ):
        second_masked_data = await guardrail.async_pre_call_hook(
            user_api_key_dict=MagicMock(),
            cache=MagicMock(),
            data=second_data,
        )

    assert first_masked_data["metadata"]["pii_request_id"] != (
        second_masked_data["metadata"]["pii_request_id"]
    )

    model_stream = _stream_chunks(
        [
            litellm.ModelResponseStream(
                choices=[
                    litellm.StreamingChoices(
                        index=0,
                        delta={"content": "<PHONE_NUMBER_1>"},
                    )
                ]
            )
        ]
    )
    restored_stream = guardrail.async_post_call_streaming_iterator_hook(
        user_api_key_dict=MagicMock(),
        response=model_stream,
        request_data=first_masked_data,
    )

    content = await _collect_stream_content(restored_stream)

    assert content == "+79031234567"
    assert content != "+79037654321"
