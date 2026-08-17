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
    user_text = (
        "Паспорт 45 12 №678901, пользователь ci_runner_12, "
        "хост app-prod-01"
    )
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
