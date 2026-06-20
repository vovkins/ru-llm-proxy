"""Unit tests for PII guardrail module."""

import json
import logging
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from litellm_guardrails.pii_guardrail import RuPIIGuardrail


def _entity(text, value, entity_type="PHONE_NUMBER", score=1.0):
    start = text.index(value)
    return {
        "entity_type": entity_type,
        "start": start,
        "end": start + len(value),
        "score": score,
        "text": value,
    }


def _mock_redis(get_value=None):
    redis = AsyncMock()
    redis.setex = AsyncMock()
    redis.get = AsyncMock(return_value=get_value)
    redis.delete = AsyncMock()
    return redis


@pytest.fixture
def guardrail():
    """Create a guardrail instance with mocked Redis."""
    g = RuPIIGuardrail()
    g._redis = _mock_redis()
    return g


# === _get_request_id ===


class TestGetRequestId:
    def test_from_metadata_request_id(self, guardrail):
        data = {"metadata": {"request_id": "req-123"}}
        assert guardrail._get_request_id(data) == "req-123"

    def test_from_litellm_call_id(self, guardrail):
        data = {"litellm_call_id": "call-456"}
        assert guardrail._get_request_id(data) == "call-456"

    def test_generates_uuid_when_none(self, guardrail):
        result = guardrail._get_request_id({})
        assert isinstance(result, str)
        assert len(result) > 0

    def test_metadata_takes_priority(self, guardrail):
        data = {"metadata": {"request_id": "from-meta"}, "litellm_call_id": "from-call"}
        assert guardrail._get_request_id(data) == "from-meta"


# === failure mode ===


class TestFailureMode:
    def test_defaults_to_fail_open_for_unknown_value(self):
        guardrail = RuPIIGuardrail(failure_mode="bad-value")
        assert guardrail.failure_mode == "fail_open"

    def test_accepts_fail_closed_alias(self):
        guardrail = RuPIIGuardrail(failure_mode="fail-closed")
        assert guardrail.failure_mode == "fail_closed"

    def test_rejects_non_positive_mapping_ttl(self):
        guardrail = RuPIIGuardrail(mapping_ttl_seconds=-5)
        assert guardrail.mapping_ttl_seconds == 3600


# === _analyze_text ===


class TestAnalyzeText:
    @pytest.mark.asyncio
    async def test_returns_entities(self, guardrail):
        mock_response = MagicMock()
        mock_response.json.return_value = {
            "entities": [
                {
                    "entity_type": "PHONE_NUMBER",
                    "start": 12,
                    "end": 27,
                    "score": 1.0,
                    "text": "+79031234567",
                }
            ]
        }
        mock_response.raise_for_status = MagicMock()

        mock_instance = AsyncMock()
        mock_instance.post.return_value = mock_response
        mock_instance.__aenter__ = AsyncMock(return_value=mock_instance)
        mock_instance.__aexit__ = AsyncMock(return_value=False)

        with patch("litellm_guardrails.pii_guardrail.httpx.AsyncClient", return_value=mock_instance):
            result = await guardrail._analyze_text("Мой телефон +79031234567")

        assert len(result) == 1
        assert result[0]["entity_type"] == "PHONE_NUMBER"

    @pytest.mark.asyncio
    async def test_returns_empty_on_no_pii(self, guardrail):
        mock_response = MagicMock()
        mock_response.json.return_value = {"entities": []}
        mock_response.raise_for_status = MagicMock()

        mock_instance = AsyncMock()
        mock_instance.post.return_value = mock_response
        mock_instance.__aenter__ = AsyncMock(return_value=mock_instance)
        mock_instance.__aexit__ = AsyncMock(return_value=False)

        with patch("litellm_guardrails.pii_guardrail.httpx.AsyncClient", return_value=mock_instance):
            result = await guardrail._analyze_text("Обычный текст без PII")

        assert result == []


# === _mask_text ===


class TestMaskText:
    def test_masks_with_unique_placeholders_for_same_type(self, guardrail):
        text = "Телефоны +79031234567 и 89031234567"
        entities = [
            _entity(text, "+79031234567"),
            _entity(text, "89031234567"),
        ]

        masked_text, mapping = guardrail._mask_text(text, entities)

        assert masked_text == "Телефоны <PHONE_NUMBER_1> и <PHONE_NUMBER_2>"
        assert mapping == {
            "<PHONE_NUMBER_1>": "+79031234567",
            "<PHONE_NUMBER_2>": "89031234567",
        }

    def test_masks_repeated_same_value_with_distinct_placeholders(self, guardrail):
        phone = "+79031234567"
        text = f"Телефоны {phone} и {phone}"
        first_start = text.index(phone)
        second_start = text.rindex(phone)
        entities = [
            {
                "entity_type": "PHONE_NUMBER",
                "start": first_start,
                "end": first_start + len(phone),
                "score": 1.0,
                "text": phone,
            },
            {
                "entity_type": "PHONE_NUMBER",
                "start": second_start,
                "end": second_start + len(phone),
                "score": 1.0,
                "text": phone,
            },
        ]

        masked_text, mapping = guardrail._mask_text(text, entities)

        assert masked_text == "Телефоны <PHONE_NUMBER_1> и <PHONE_NUMBER_2>"
        assert mapping == {
            "<PHONE_NUMBER_1>": phone,
            "<PHONE_NUMBER_2>": phone,
        }

    def test_entity_counts_are_request_scoped(self, guardrail):
        first = "Первый +79031234567"
        second = "Второй 89031234567"
        counts = {}

        first_masked, first_mapping = guardrail._mask_text(
            first,
            [_entity(first, "+79031234567")],
            counts,
        )
        second_masked, second_mapping = guardrail._mask_text(
            second,
            [_entity(second, "89031234567")],
            counts,
        )

        assert first_masked == "Первый <PHONE_NUMBER_1>"
        assert second_masked == "Второй <PHONE_NUMBER_2>"
        assert first_mapping["<PHONE_NUMBER_1>"] == "+79031234567"
        assert second_mapping["<PHONE_NUMBER_2>"] == "89031234567"

    def test_skips_invalid_and_overlapping_entities(self, guardrail):
        text = "ИНН 7707083893"
        entities = [
            {"entity_type": "RU_INN", "start": -1, "end": 3},
            _entity(text, "7707083893", "RU_INN"),
            {"entity_type": "PHONE_NUMBER", "start": 4, "end": 10},
        ]

        masked_text, mapping = guardrail._mask_text(text, entities)

        assert masked_text == "ИНН <RU_INN_1>"
        assert mapping == {"<RU_INN_1>": "7707083893"}

    def test_prefers_longer_entity_when_overlapping_entities_share_start(self, guardrail):
        text = "Иван Иванов"
        entities = [
            {"entity_type": "PERSON", "start": 0, "end": len("Иван")},
            {"entity_type": "PERSON", "start": 0, "end": len(text)},
        ]

        masked_text, mapping = guardrail._mask_text(text, entities)

        assert masked_text == "<PERSON_1>"
        assert mapping == {"<PERSON_1>": "Иван Иванов"}

    def test_no_entities_returns_original(self, guardrail):
        masked_text, mapping = guardrail._mask_text("Обычный текст", [])
        assert masked_text == "Обычный текст"
        assert mapping == {}

    def test_entity_counts_from_mapping(self, guardrail):
        mapping = {
            "<PHONE_NUMBER_1>": "+79031234567",
            "<PHONE_NUMBER_2>": "89031234567",
            "<RU_INN_1>": "7707083893",
        }

        assert guardrail._entity_counts_from_mapping(mapping) == {
            "PHONE_NUMBER": 2,
            "RU_INN": 1,
        }


# === _save_mapping / _load_mapping ===


class TestRedisMapping:
    @pytest.mark.asyncio
    async def test_save_mapping(self, guardrail):
        await guardrail._save_mapping("req-1", {"<PHONE>": "+79031234567"})

        guardrail._redis.setex.assert_called_once()
        args = guardrail._redis.setex.call_args[0]
        assert args[0] == "pii_mapping:req-1"
        assert args[1] == 3600
        assert json.loads(args[2]) == {"<PHONE>": "+79031234567"}

    @pytest.mark.asyncio
    async def test_save_mapping_uses_configured_ttl(self):
        guardrail = RuPIIGuardrail(mapping_ttl_seconds=30)
        guardrail._redis = _mock_redis()

        await guardrail._save_mapping("req-1", {"<PHONE>": "+79031234567"})

        assert guardrail._redis.setex.call_args[0][1] == 30

    @pytest.mark.asyncio
    async def test_save_empty_mapping_skips(self, guardrail):
        await guardrail._save_mapping("req-1", {})
        guardrail._redis.setex.assert_not_called()

    @pytest.mark.asyncio
    async def test_load_mapping_returns_dict(self, guardrail):
        guardrail._redis.get.return_value = json.dumps({"<PHONE>": "+79031234567"})

        result = await guardrail._load_mapping("req-1")

        assert result == {"<PHONE>": "+79031234567"}

    @pytest.mark.asyncio
    async def test_load_mapping_returns_empty_on_miss(self, guardrail):
        guardrail._redis.get.return_value = None

        result = await guardrail._load_mapping("req-1")

        assert result == {}


# === async_pre_call_hook ===


class TestPreCallHook:
    @pytest.mark.asyncio
    async def test_masks_pii_in_messages_and_saves_mapping(self, guardrail):
        first = "Мой телефон +79031234567"
        second = "Рабочий телефон 89031234567"
        save_mapping = AsyncMock()

        with patch.object(
            guardrail,
            "_analyze_text",
            side_effect=[
                [_entity(first, "+79031234567")],
                [_entity(second, "89031234567")],
            ],
        ):
            with patch.object(guardrail, "_save_mapping", save_mapping):
                data = {
                    "messages": [
                        {"role": "user", "content": first},
                        {"role": "user", "content": second},
                    ]
                }

                result = await guardrail.async_pre_call_hook(
                    user_api_key_dict=MagicMock(),
                    cache=MagicMock(),
                    data=data,
                )

        assert result["messages"][0]["content"] == "Мой телефон <PHONE_NUMBER_1>"
        assert result["messages"][1]["content"] == "Рабочий телефон <PHONE_NUMBER_2>"
        assert result["metadata"]["pii_request_id"]
        saved_mapping = save_mapping.call_args[0][1]
        assert saved_mapping == {
            "<PHONE_NUMBER_1>": "+79031234567",
            "<PHONE_NUMBER_2>": "89031234567",
        }

    @pytest.mark.asyncio
    async def test_masks_pii_in_text_content_blocks(self, guardrail):
        text = "Клиент Иван Иванов, телефон +79031234567"
        save_mapping = AsyncMock()

        with patch.object(
            guardrail,
            "_analyze_text",
            return_value=[
                _entity(text, "Иван Иванов", "PERSON"),
                _entity(text, "+79031234567"),
            ],
        ):
            with patch.object(guardrail, "_save_mapping", save_mapping):
                data = {
                    "messages": [
                        {
                            "role": "user",
                            "content": [
                                {"type": "text", "text": text},
                                {
                                    "type": "image_url",
                                    "image_url": {"url": "https://example.test/image.png"},
                                },
                            ],
                        }
                    ]
                }

                result = await guardrail.async_pre_call_hook(
                    user_api_key_dict=MagicMock(),
                    cache=MagicMock(),
                    data=data,
                )

        content = result["messages"][0]["content"]
        assert content[0]["text"] == "Клиент <PERSON_1>, телефон <PHONE_NUMBER_1>"
        assert content[1] == {
            "type": "image_url",
            "image_url": {"url": "https://example.test/image.png"},
        }
        assert save_mapping.call_args[0][1] == {
            "<PERSON_1>": "Иван Иванов",
            "<PHONE_NUMBER_1>": "+79031234567",
        }

    @pytest.mark.asyncio
    async def test_masks_pii_in_function_arguments(self, guardrail):
        tool_args = '{"phone":"+79031234567"}'
        function_args = '{"inn":"7707083893"}'
        save_mapping = AsyncMock()

        with patch.object(
            guardrail,
            "_analyze_text",
            side_effect=[
                [_entity(tool_args, "+79031234567")],
                [_entity(function_args, "7707083893", "RU_INN")],
            ],
        ):
            with patch.object(guardrail, "_save_mapping", save_mapping):
                data = {
                    "messages": [
                        {
                            "role": "assistant",
                            "tool_calls": [
                                {
                                    "id": "call-1",
                                    "type": "function",
                                    "function": {
                                        "name": "lookup_phone",
                                        "arguments": tool_args,
                                    },
                                }
                            ],
                            "function_call": {
                                "name": "lookup_inn",
                                "arguments": function_args,
                            },
                        }
                    ]
                }

                result = await guardrail.async_pre_call_hook(
                    user_api_key_dict=MagicMock(),
                    cache=MagicMock(),
                    data=data,
                )

        message = result["messages"][0]
        assert message["tool_calls"][0]["function"]["arguments"] == (
            '{"phone":"<PHONE_NUMBER_1>"}'
        )
        assert message["function_call"]["arguments"] == '{"inn":"<RU_INN_1>"}'
        assert save_mapping.call_args[0][1] == {
            "<PHONE_NUMBER_1>": "+79031234567",
            "<RU_INN_1>": "7707083893",
        }

    @pytest.mark.asyncio
    async def test_masking_log_is_structured_and_does_not_include_raw_pii(
        self,
        guardrail,
        caplog,
    ):
        text = "Мой телефон +79031234567"

        with patch.object(
            guardrail,
            "_analyze_text",
            return_value=[_entity(text, "+79031234567")],
        ):
            with patch.object(guardrail, "_save_mapping", AsyncMock()):
                with caplog.at_level(
                    logging.INFO,
                    logger="litellm_guardrails.pii_guardrail",
                ):
                    await guardrail.async_pre_call_hook(
                        user_api_key_dict=MagicMock(),
                        cache=MagicMock(),
                        data={"messages": [{"role": "user", "content": text}]},
                    )

        logs = "\n".join(record.getMessage() for record in caplog.records)
        assert "pii_guardrail_masked" in logs
        assert "PHONE_NUMBER" in logs
        assert "+79031234567" not in logs

    @pytest.mark.asyncio
    async def test_no_messages_returns_data(self, guardrail):
        data = {"model": "glm-5.1"}

        result = await guardrail.async_pre_call_hook(
            user_api_key_dict=MagicMock(),
            cache=MagicMock(),
            data=data,
        )

        assert result == data

    @pytest.mark.asyncio
    async def test_no_pii_passes_through(self, guardrail):
        with patch.object(guardrail, "_analyze_text", return_value=[]):
            data = {"messages": [{"role": "user", "content": "Расскажи joke"}]}

            result = await guardrail.async_pre_call_hook(
                user_api_key_dict=MagicMock(),
                cache=MagicMock(),
                data=data,
            )

        assert result["messages"][0]["content"] == "Расскажи joke"

    @pytest.mark.asyncio
    async def test_analyze_error_fails_open(self, guardrail):
        text = "Мой телефон +79031234567"
        with patch.object(guardrail, "_analyze_text", side_effect=Exception("connection error")):
            data = {"messages": [{"role": "user", "content": text}]}

            result = await guardrail.async_pre_call_hook(
                user_api_key_dict=MagicMock(),
                cache=MagicMock(),
                data=data,
            )

        assert result["messages"][0]["content"] == text
        assert "metadata" not in result

    @pytest.mark.asyncio
    async def test_analyze_error_fails_closed(self):
        guardrail = RuPIIGuardrail(failure_mode="fail_closed")
        guardrail._redis = _mock_redis()
        text = "Мой телефон +79031234567"

        with patch.object(guardrail, "_analyze_text", side_effect=Exception("connection error")):
            with pytest.raises(RuntimeError, match="PII guardrail masking failed"):
                await guardrail.async_pre_call_hook(
                    user_api_key_dict=MagicMock(),
                    cache=MagicMock(),
                    data={"messages": [{"role": "user", "content": text}]},
                )

    @pytest.mark.asyncio
    async def test_redis_save_error_fails_open_without_partial_mutation(self, guardrail):
        text = "Мой телефон +79031234567"
        guardrail._redis.setex.side_effect = RuntimeError("redis down")

        with patch.object(guardrail, "_analyze_text", return_value=[_entity(text, "+79031234567")]):
            data = {"messages": [{"role": "user", "content": text}]}

            result = await guardrail.async_pre_call_hook(
                user_api_key_dict=MagicMock(),
                cache=MagicMock(),
                data=data,
            )

        assert result["messages"][0]["content"] == text
        assert "metadata" not in result

    @pytest.mark.asyncio
    async def test_redis_save_error_fails_closed(self):
        guardrail = RuPIIGuardrail(failure_mode="fail_closed")
        guardrail._redis = _mock_redis()
        guardrail._redis.setex.side_effect = RuntimeError("redis down")
        text = "Мой телефон +79031234567"

        with patch.object(guardrail, "_analyze_text", return_value=[_entity(text, "+79031234567")]):
            with pytest.raises(RuntimeError, match="PII guardrail mapping save failed"):
                await guardrail.async_pre_call_hook(
                    user_api_key_dict=MagicMock(),
                    cache=MagicMock(),
                    data={"messages": [{"role": "user", "content": text}]},
                )


# === async_post_call_success_hook ===


class TestPostCallHook:
    @pytest.mark.asyncio
    async def test_unmasks_response(self, guardrail):
        import litellm

        mapping = {
            "<PHONE_NUMBER_1>": "+79031234567",
            "<PHONE_NUMBER_2>": "89031234567",
        }
        guardrail._redis.get.return_value = json.dumps(mapping)

        response = litellm.ModelResponse(
            id="test",
            choices=[
                litellm.Choices(
                    index=0,
                    message=litellm.Message(
                        role="assistant",
                        content="Телефоны <PHONE_NUMBER_1> и <PHONE_NUMBER_2> подтверждены",
                    ),
                    finish_reason="stop",
                )
            ],
        )

        await guardrail.async_post_call_success_hook(
            data={"metadata": {"pii_request_id": "req-1"}},
            user_api_key_dict=MagicMock(),
            response=response,
        )

        assert "+79031234567" in response.choices[0].message.content
        assert "89031234567" in response.choices[0].message.content
        assert "<PHONE_NUMBER_1>" not in response.choices[0].message.content

    @pytest.mark.asyncio
    async def test_unmasks_tool_and_function_arguments(self, guardrail):
        import litellm

        mapping = {
            "<PHONE_NUMBER_1>": "+79031234567",
            "<RU_INN_1>": "7707083893",
        }
        guardrail._redis.get.return_value = json.dumps(mapping)

        message = MagicMock()
        message.content = "Tool call prepared"
        message.reasoning_content = "Проверяю <PHONE_NUMBER_1>"
        message.tool_calls = [
            {
                "id": "call-1",
                "type": "function",
                "function": {
                    "name": "lookup_phone",
                    "arguments": '{"phone":"<PHONE_NUMBER_1>"}',
                },
            }
        ]
        message.function_call = {
            "name": "lookup_inn",
            "arguments": '{"inn":"<RU_INN_1>"}',
        }
        response = litellm.ModelResponse(id="test", choices=[])
        response.choices = [MagicMock(message=message)]

        await guardrail.async_post_call_success_hook(
            data={"metadata": {"pii_request_id": "req-1"}},
            user_api_key_dict=MagicMock(),
            response=response,
        )

        assert message.reasoning_content == "Проверяю +79031234567"
        assert message.tool_calls[0]["function"]["arguments"] == (
            '{"phone":"+79031234567"}'
        )
        assert message.function_call["arguments"] == '{"inn":"7707083893"}'

    @pytest.mark.asyncio
    async def test_no_request_id_skips(self, guardrail):
        import litellm

        response = litellm.ModelResponse(
            id="test",
            choices=[
                litellm.Choices(
                    index=0,
                    message=litellm.Message(role="assistant", content="test"),
                    finish_reason="stop",
                )
            ],
        )

        await guardrail.async_post_call_success_hook(
            data={"metadata": {}},
            user_api_key_dict=MagicMock(),
            response=response,
        )

        assert response.choices[0].message.content == "test"

    @pytest.mark.asyncio
    async def test_uses_litellm_call_id_when_metadata_id_missing(self, guardrail):
        import litellm

        guardrail._redis.get.return_value = json.dumps(
            {"<PHONE_NUMBER_1>": "+79031234567"}
        )
        response = litellm.ModelResponse(
            id="test",
            choices=[
                litellm.Choices(
                    index=0,
                    message=litellm.Message(role="assistant", content="<PHONE_NUMBER_1>"),
                    finish_reason="stop",
                )
            ],
        )

        await guardrail.async_post_call_success_hook(
            data={"metadata": {}, "litellm_call_id": "req-1"},
            user_api_key_dict=MagicMock(),
            response=response,
        )

        assert response.choices[0].message.content == "+79031234567"

    @pytest.mark.asyncio
    async def test_redis_load_error_fails_open(self, guardrail):
        import litellm

        guardrail._redis.get.side_effect = RuntimeError("redis down")
        response = litellm.ModelResponse(
            id="test",
            choices=[
                litellm.Choices(
                    index=0,
                    message=litellm.Message(role="assistant", content="<PHONE_NUMBER_1>"),
                    finish_reason="stop",
                )
            ],
        )

        await guardrail.async_post_call_success_hook(
            data={"metadata": {"pii_request_id": "req-1"}},
            user_api_key_dict=MagicMock(),
            response=response,
        )

        assert response.choices[0].message.content == "<PHONE_NUMBER_1>"

    @pytest.mark.asyncio
    async def test_redis_load_error_fails_closed(self):
        import litellm

        guardrail = RuPIIGuardrail(failure_mode="fail_closed")
        guardrail._redis = _mock_redis()
        guardrail._redis.get.side_effect = RuntimeError("redis down")
        response = litellm.ModelResponse(
            id="test",
            choices=[
                litellm.Choices(
                    index=0,
                    message=litellm.Message(role="assistant", content="<PHONE_NUMBER_1>"),
                    finish_reason="stop",
                )
            ],
        )

        with pytest.raises(RuntimeError, match="PII guardrail mapping load failed"):
            await guardrail.async_post_call_success_hook(
                data={"metadata": {"pii_request_id": "req-1"}},
                user_api_key_dict=MagicMock(),
                response=response,
            )


# === _replace_placeholders ===


class TestReplacePlaceholders:
    def test_replaces_longer_placeholders_first(self, guardrail):
        mapping = {
            "<PERSON_1>": "Иван",
            "<PERSON_10>": "Петр",
        }

        result = guardrail._replace_placeholders("<PERSON_10> и <PERSON_1>", mapping)

        assert result == "Петр и Иван"
