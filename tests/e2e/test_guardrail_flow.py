"""Deterministic guardrail flow test without external LLM calls."""

import json
from unittest.mock import AsyncMock, MagicMock, patch

import litellm
import pytest

from litellm_guardrails.pii_guardrail import RuPIIGuardrail


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

    saved_mapping = json.loads(redis_store["pii_mapping:flow-1"])
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
    assert "pii_mapping:flow-1" not in redis_store


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
    assert "pii_mapping:flow-stream-1" not in redis_store
