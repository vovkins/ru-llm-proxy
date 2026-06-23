"""Unit tests for PII guardrail module."""

import json
import logging
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import litellm
import pytest

from litellm_guardrails.pii_guardrail import AnalyzerOverloadedError, RuPIIGuardrail


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
    def test_generates_server_id_ignoring_client_ids(self, guardrail):
        data = {
            "metadata": {"request_id": "client-controlled"},
            "litellm_call_id": "call-456",
        }

        result = guardrail._get_request_id(data)

        assert result not in {"client-controlled", "call-456"}
        uuid.UUID(result)

    def test_generates_uuid_when_none(self, guardrail):
        result = guardrail._get_request_id({})
        uuid.UUID(result)

    def test_response_request_id_uses_only_guardrail_mapping_id(self, guardrail):
        data = {
            "metadata": {"request_id": "from-client"},
            "litellm_call_id": "from-call",
        }
        assert guardrail._get_response_request_id(data) is None

        data["metadata"]["pii_request_id"] = "server-mapping"
        assert guardrail._get_response_request_id(data) == "server-mapping"


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


# === policy mode ===


class TestPolicyMode:
    def test_defaults_to_mask_mode(self):
        guardrail = RuPIIGuardrail()
        assert guardrail.pii_mode == "mask"

    def test_accepts_block_mode_alias(self):
        guardrail = RuPIIGuardrail(pii_mode="block")
        assert guardrail.pii_mode == "block"

    def test_defaults_to_mask_for_unknown_value(self):
        guardrail = RuPIIGuardrail(pii_mode="bad-value")
        assert guardrail.pii_mode == "mask"


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

    @pytest.mark.asyncio
    async def test_raises_analyzer_overloaded_for_capacity_503(self, guardrail):
        mock_response = MagicMock()
        mock_response.status_code = 503
        mock_response.json.return_value = {
            "detail": {
                "code": "analyzer_overloaded",
                "reason": "queue_timeout",
                "message": "Timed out waiting for Presidio Analyzer capacity.",
            }
        }
        mock_response.raise_for_status = MagicMock()

        mock_instance = AsyncMock()
        mock_instance.post.return_value = mock_response
        mock_instance.__aenter__ = AsyncMock(return_value=mock_instance)
        mock_instance.__aexit__ = AsyncMock(return_value=False)

        with patch("litellm_guardrails.pii_guardrail.httpx.AsyncClient", return_value=mock_instance):
            with pytest.raises(AnalyzerOverloadedError) as exc_info:
                await guardrail._analyze_text("Мой телефон +79031234567")

        assert exc_info.value.reason == "queue_timeout"
        mock_response.raise_for_status.assert_not_called()


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
    async def test_analyzer_overload_fails_closed_even_in_fail_open_mode(self):
        guardrail = RuPIIGuardrail(failure_mode="fail_open")
        guardrail._redis = _mock_redis()
        data = {
            "messages": [
                {"role": "user", "content": "Мой телефон +79031234567"},
            ]
        }

        with patch.object(
            guardrail,
            "_analyze_text",
            side_effect=AnalyzerOverloadedError(reason="queue_full"),
        ):
            with pytest.raises(RuntimeError) as exc_info:
                await guardrail.async_pre_call_hook(
                    user_api_key_dict=MagicMock(),
                    cache=MagicMock(),
                    data=data,
                )

        assert "analyzer overloaded" in str(exc_info.value)
        assert "metadata" not in data
        guardrail._redis.setex.assert_not_called()

    @pytest.mark.asyncio
    async def test_analyzer_overload_fails_closed_for_responses_input(self):
        guardrail = RuPIIGuardrail(failure_mode="fail_open")
        guardrail._redis = _mock_redis()
        data = {
            "model": "openai-gpt-5.4-mini",
            "instructions": "Не раскрывай телефон +79031234567",
            "input": "Расскажи joke",
        }

        with patch.object(
            guardrail,
            "_analyze_text",
            side_effect=AnalyzerOverloadedError(reason="queue_timeout"),
        ):
            with pytest.raises(RuntimeError) as exc_info:
                await guardrail.async_pre_call_hook(
                    user_api_key_dict=MagicMock(),
                    cache=MagicMock(),
                    data=data,
                    call_type="responses",
                )

        assert "analyzer overloaded" in str(exc_info.value)
        assert data["instructions"] == "Не раскрывай телефон +79031234567"
        assert data["input"] == "Расскажи joke"
        assert "metadata" not in data
        guardrail._redis.setex.assert_not_called()

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
    async def test_masks_pii_in_responses_string_input(self, guardrail):
        text = "Мой телефон +79031234567"
        save_mapping = AsyncMock()

        with patch.object(
            guardrail,
            "_analyze_text",
            return_value=[_entity(text, "+79031234567")],
        ):
            with patch.object(guardrail, "_save_mapping", save_mapping):
                data = {"model": "openai-gpt-5.4-mini", "input": text}

                result = await guardrail.async_pre_call_hook(
                    user_api_key_dict=MagicMock(),
                    cache=MagicMock(),
                    data=data,
                    call_type="responses",
                )

        assert result["input"] == "Мой телефон <PHONE_NUMBER_1>"
        assert result["metadata"]["pii_request_id"]
        assert save_mapping.call_args[0][1] == {
            "<PHONE_NUMBER_1>": "+79031234567",
        }

    @pytest.mark.asyncio
    async def test_masks_pii_in_responses_input_items(self, guardrail):
        first = "Клиент Иван Иванов, телефон +79031234567"
        previous = "Ассистент видел Петр Петров"
        second = "ИНН 7707083893"
        third = "Email test@example.com"
        save_mapping = AsyncMock()

        with patch.object(
            guardrail,
            "_analyze_text",
            side_effect=[
                [
                    _entity(first, "Иван Иванов", "PERSON"),
                    _entity(first, "+79031234567"),
                ],
                [_entity(previous, "Петр Петров", "PERSON")],
                [_entity(second, "7707083893", "RU_INN")],
                [_entity(third, "test@example.com", "EMAIL_ADDRESS")],
            ],
        ):
            with patch.object(guardrail, "_save_mapping", save_mapping):
                data = {
                    "model": "openai-gpt-5.4-mini",
                    "input": [
                        {
                            "role": "user",
                            "content": [
                                {"type": "input_text", "text": first},
                                {
                                    "type": "input_image",
                                    "image_url": "https://example.test/image.png",
                                },
                                {"type": "output_text", "text": previous},
                            ],
                        },
                        {"type": "input_text", "text": second},
                        {"role": "user", "content": third},
                    ],
                }

                result = await guardrail.async_pre_call_hook(
                    user_api_key_dict=MagicMock(),
                    cache=MagicMock(),
                    data=data,
                    call_type="responses",
                )

        assert result["input"][0]["content"][0]["text"] == (
            "Клиент <PERSON_1>, телефон <PHONE_NUMBER_1>"
        )
        assert result["input"][0]["content"][1] == {
            "type": "input_image",
            "image_url": "https://example.test/image.png",
        }
        assert result["input"][0]["content"][2]["text"] == (
            "Ассистент видел <PERSON_2>"
        )
        assert result["input"][1]["text"] == "ИНН <RU_INN_1>"
        assert result["input"][2]["content"] == "Email <EMAIL_ADDRESS_1>"
        assert save_mapping.call_args[0][1] == {
            "<PERSON_1>": "Иван Иванов",
            "<PHONE_NUMBER_1>": "+79031234567",
            "<PERSON_2>": "Петр Петров",
            "<RU_INN_1>": "7707083893",
            "<EMAIL_ADDRESS_1>": "test@example.com",
        }

    @pytest.mark.asyncio
    async def test_masks_pii_in_responses_instructions_and_tool_output(self, guardrail):
        instructions = "Не раскрывай телефон +79031234567"
        tool_output = "Tool returned email test@example.com"
        save_mapping = AsyncMock()

        with patch.object(
            guardrail,
            "_analyze_text",
            side_effect=[
                [_entity(instructions, "+79031234567")],
                [_entity(tool_output, "test@example.com", "EMAIL_ADDRESS")],
            ],
        ):
            with patch.object(guardrail, "_save_mapping", save_mapping):
                data = {
                    "model": "openai-gpt-5.4-mini",
                    "instructions": instructions,
                    "input": [
                        {
                            "type": "function_call_output",
                            "call_id": "call-1",
                            "output": [
                                {"type": "input_text", "text": tool_output},
                                {
                                    "type": "input_image",
                                    "image_url": "https://example.test/image.png",
                                },
                            ],
                        }
                    ],
                }

                result = await guardrail.async_pre_call_hook(
                    user_api_key_dict=MagicMock(),
                    cache=MagicMock(),
                    data=data,
                    call_type="responses",
                )

        assert result["instructions"] == "Не раскрывай телефон <PHONE_NUMBER_1>"
        assert result["input"][0]["output"][0]["text"] == (
            "Tool returned email <EMAIL_ADDRESS_1>"
        )
        assert result["input"][0]["output"][1] == {
            "type": "input_image",
            "image_url": "https://example.test/image.png",
        }
        assert save_mapping.call_args[0][1] == {
            "<PHONE_NUMBER_1>": "+79031234567",
            "<EMAIL_ADDRESS_1>": "test@example.com",
        }

    @pytest.mark.asyncio
    async def test_masks_pii_in_responses_function_call_arguments(self, guardrail):
        arguments = '{"inn":"7707083893"}'
        save_mapping = AsyncMock()

        with patch.object(
            guardrail,
            "_analyze_text",
            return_value=[_entity(arguments, "7707083893", "RU_INN")],
        ):
            with patch.object(guardrail, "_save_mapping", save_mapping):
                data = {
                    "model": "openai-gpt-5.4-mini",
                    "input": [
                        {
                            "type": "function_call",
                            "call_id": "call-1",
                            "name": "lookup_client",
                            "arguments": arguments,
                        }
                    ],
                }

                result = await guardrail.async_pre_call_hook(
                    user_api_key_dict=MagicMock(),
                    cache=MagicMock(),
                    data=data,
                    call_type="responses",
                )

        assert result["input"][0]["arguments"] == '{"inn":"<RU_INN_1>"}'
        assert save_mapping.call_args[0][1] == {
            "<RU_INN_1>": "7707083893",
        }

    @pytest.mark.asyncio
    async def test_clean_responses_input_is_analyzed_and_passes_through(self):
        guardrail = RuPIIGuardrail()
        guardrail._redis = _mock_redis()
        data = {"model": "openai-gpt-5.4-mini", "input": "Расскажи joke"}

        with patch.object(guardrail, "_analyze_text", return_value=[]) as analyze_text:
            result = await guardrail.async_pre_call_hook(
                user_api_key_dict=MagicMock(),
                cache=MagicMock(),
                data=data,
                call_type="responses",
            )

        assert result == data
        analyze_text.assert_called_once_with("Расскажи joke")
        guardrail._redis.setex.assert_not_called()

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
    async def test_block_mode_rejects_pii_without_mutating_or_saving(self):
        guardrail = RuPIIGuardrail(pii_mode="block")
        guardrail._redis = _mock_redis()
        text = "Мой телефон +79031234567"
        data = {"model": "glm-5.1", "messages": [{"role": "user", "content": text}]}

        with patch.object(
            guardrail,
            "_analyze_text",
            return_value=[_entity(text, "+79031234567")],
        ):
            with pytest.raises(litellm.UnprocessableEntityError) as exc_info:
                await guardrail.async_pre_call_hook(
                    user_api_key_dict=MagicMock(),
                    cache=MagicMock(),
                    data=data,
                )

        assert data["messages"][0]["content"] == text
        assert "metadata" not in data
        guardrail._redis.setex.assert_not_called()
        assert exc_info.value.response.status_code == 422
        error_body = exc_info.value.response.json()
        assert error_body == {
            "error": {
                "message": "Request contains personal data and was blocked by PII policy.",
                "type": "pii_detected",
                "code": "pii_blocked",
                "details": {"entities": ["PHONE_NUMBER"]},
            }
        }
        assert "+79031234567" not in json.dumps(error_body, ensure_ascii=False)

    @pytest.mark.asyncio
    async def test_block_mode_rejects_pii_in_text_content_blocks_without_mutating(self):
        guardrail = RuPIIGuardrail(pii_mode="block")
        guardrail._redis = _mock_redis()
        text = "Клиент Иван Иванов, телефон +79031234567"
        data = {
            "model": "glm-5.1",
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
            ],
        }

        with patch.object(
            guardrail,
            "_analyze_text",
            return_value=[
                _entity(text, "Иван Иванов", "PERSON"),
                _entity(text, "+79031234567"),
            ],
        ):
            with pytest.raises(litellm.UnprocessableEntityError) as exc_info:
                await guardrail.async_pre_call_hook(
                    user_api_key_dict=MagicMock(),
                    cache=MagicMock(),
                    data=data,
                )

        content = data["messages"][0]["content"]
        assert content[0]["text"] == text
        assert content[1] == {
            "type": "image_url",
            "image_url": {"url": "https://example.test/image.png"},
        }
        assert "metadata" not in data
        guardrail._redis.setex.assert_not_called()
        assert exc_info.value.response.status_code == 422
        error_body = exc_info.value.response.json()
        assert error_body["error"]["details"]["entities"] == [
            "PERSON",
            "PHONE_NUMBER",
        ]
        assert "Иван Иванов" not in json.dumps(error_body, ensure_ascii=False)
        assert "+79031234567" not in json.dumps(error_body, ensure_ascii=False)

    @pytest.mark.asyncio
    async def test_block_mode_rejects_pii_in_function_arguments_without_mutating(self):
        guardrail = RuPIIGuardrail(pii_mode="block")
        guardrail._redis = _mock_redis()
        tool_args = '{"phone":"+79031234567"}'
        function_args = '{"inn":"7707083893"}'
        data = {
            "model": "glm-5.1",
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
            ],
        }

        with patch.object(
            guardrail,
            "_analyze_text",
            side_effect=[
                [_entity(tool_args, "+79031234567")],
                [_entity(function_args, "7707083893", "RU_INN")],
            ],
        ):
            with pytest.raises(litellm.UnprocessableEntityError) as exc_info:
                await guardrail.async_pre_call_hook(
                    user_api_key_dict=MagicMock(),
                    cache=MagicMock(),
                    data=data,
                )

        message = data["messages"][0]
        assert message["tool_calls"][0]["function"]["arguments"] == tool_args
        assert message["function_call"]["arguments"] == function_args
        assert "metadata" not in data
        guardrail._redis.setex.assert_not_called()
        assert exc_info.value.response.status_code == 422
        error_body = exc_info.value.response.json()
        assert error_body["error"]["details"]["entities"] == [
            "PHONE_NUMBER",
            "RU_INN",
        ]
        assert "+79031234567" not in json.dumps(error_body, ensure_ascii=False)
        assert "7707083893" not in json.dumps(error_body, ensure_ascii=False)

    @pytest.mark.asyncio
    async def test_block_mode_allows_clean_requests(self):
        guardrail = RuPIIGuardrail(pii_mode="block")
        guardrail._redis = _mock_redis()
        data = {"messages": [{"role": "user", "content": "Расскажи joke"}]}

        with patch.object(guardrail, "_analyze_text", return_value=[]):
            result = await guardrail.async_pre_call_hook(
                user_api_key_dict=MagicMock(),
                cache=MagicMock(),
                data=data,
            )

        assert result == data
        guardrail._redis.setex.assert_not_called()

    @pytest.mark.asyncio
    async def test_block_mode_log_does_not_include_raw_pii(self, caplog):
        guardrail = RuPIIGuardrail(pii_mode="block")
        guardrail._redis = _mock_redis()
        text = "Мой телефон +79031234567"

        with patch.object(
            guardrail,
            "_analyze_text",
            return_value=[_entity(text, "+79031234567")],
        ):
            with caplog.at_level(
                logging.INFO,
                logger="litellm_guardrails.pii_guardrail",
            ):
                with pytest.raises(litellm.UnprocessableEntityError) as exc_info:
                    await guardrail.async_pre_call_hook(
                        user_api_key_dict=MagicMock(),
                        cache=MagicMock(),
                        data={"model": "glm-5.1", "messages": [{"role": "user", "content": text}]},
                    )

        assert exc_info.value.response.status_code == 422
        logs = "\n".join(record.getMessage() for record in caplog.records)
        assert "pii_guardrail_blocked" in logs
        assert "PHONE_NUMBER" in logs
        assert "+79031234567" not in logs

    @pytest.mark.asyncio
    async def test_block_mode_keeps_prior_pii_block_when_later_analysis_fails(self):
        guardrail = RuPIIGuardrail(pii_mode="block", failure_mode="fail_open")
        guardrail._redis = _mock_redis()
        pii_text = "Мой телефон +79031234567"
        later_text = "Еще одно поле"
        data = {
            "model": "glm-5.1",
            "messages": [
                {"role": "user", "content": pii_text},
                {"role": "user", "content": later_text},
            ],
        }

        with patch.object(
            guardrail,
            "_analyze_text",
            side_effect=[
                [_entity(pii_text, "+79031234567")],
                RuntimeError("analyzer down"),
            ],
        ):
            with pytest.raises(litellm.UnprocessableEntityError) as exc_info:
                await guardrail.async_pre_call_hook(
                    user_api_key_dict=MagicMock(),
                    cache=MagicMock(),
                    data=data,
                )

        assert exc_info.value.response.status_code == 422
        assert data["messages"][0]["content"] == pii_text
        assert data["messages"][1]["content"] == later_text
        assert "metadata" not in data
        guardrail._redis.setex.assert_not_called()

    @pytest.mark.asyncio
    async def test_block_mode_rejects_pii_in_responses_input_without_mutating(self):
        guardrail = RuPIIGuardrail(pii_mode="block")
        guardrail._redis = _mock_redis()
        text = "Мой телефон +79031234567"
        data = {"model": "openai-gpt-5.4-mini", "input": text}

        with patch.object(
            guardrail,
            "_analyze_text",
            return_value=[_entity(text, "+79031234567")],
        ):
            with pytest.raises(litellm.UnprocessableEntityError) as exc_info:
                await guardrail.async_pre_call_hook(
                    user_api_key_dict=MagicMock(),
                    cache=MagicMock(),
                    data=data,
                    call_type="responses",
                )

        assert data["input"] == text
        assert "metadata" not in data
        guardrail._redis.setex.assert_not_called()
        assert exc_info.value.response.status_code == 422
        error_body = exc_info.value.response.json()
        assert error_body["error"]["details"]["entities"] == ["PHONE_NUMBER"]
        assert "+79031234567" not in json.dumps(error_body, ensure_ascii=False)

    @pytest.mark.asyncio
    async def test_block_mode_rejects_pii_in_responses_blocks_without_mutating(self):
        guardrail = RuPIIGuardrail(pii_mode="block")
        guardrail._redis = _mock_redis()
        text = "Клиент Иван Иванов, телефон +79031234567"
        data = {
            "model": "openai-gpt-5.4-mini",
            "input": [
                {
                    "role": "user",
                    "content": [
                        {"type": "input_text", "text": text},
                        {
                            "type": "input_image",
                            "image_url": "https://example.test/image.png",
                        },
                    ],
                }
            ],
        }

        with patch.object(
            guardrail,
            "_analyze_text",
            return_value=[
                _entity(text, "Иван Иванов", "PERSON"),
                _entity(text, "+79031234567"),
            ],
        ):
            with pytest.raises(litellm.UnprocessableEntityError) as exc_info:
                await guardrail.async_pre_call_hook(
                    user_api_key_dict=MagicMock(),
                    cache=MagicMock(),
                    data=data,
                    call_type="responses",
                )

        content = data["input"][0]["content"]
        assert content[0]["text"] == text
        assert content[1] == {
            "type": "input_image",
            "image_url": "https://example.test/image.png",
        }
        assert "metadata" not in data
        guardrail._redis.setex.assert_not_called()
        assert exc_info.value.response.status_code == 422
        error_body = exc_info.value.response.json()
        assert error_body["error"]["details"]["entities"] == [
            "PERSON",
            "PHONE_NUMBER",
        ]
        assert "Иван Иванов" not in json.dumps(error_body, ensure_ascii=False)
        assert "+79031234567" not in json.dumps(error_body, ensure_ascii=False)

    @pytest.mark.asyncio
    async def test_block_mode_rejects_pii_in_responses_instructions_without_mutating(self):
        guardrail = RuPIIGuardrail(pii_mode="block")
        guardrail._redis = _mock_redis()
        instructions = "Не раскрывай телефон +79031234567"
        data = {
            "model": "openai-gpt-5.4-mini",
            "instructions": instructions,
            "input": "Расскажи joke",
        }

        with patch.object(
            guardrail,
            "_analyze_text",
            side_effect=[
                [_entity(instructions, "+79031234567")],
                [],
            ],
        ):
            with pytest.raises(litellm.UnprocessableEntityError) as exc_info:
                await guardrail.async_pre_call_hook(
                    user_api_key_dict=MagicMock(),
                    cache=MagicMock(),
                    data=data,
                    call_type="responses",
                )

        assert data["instructions"] == instructions
        assert "metadata" not in data
        guardrail._redis.setex.assert_not_called()
        assert exc_info.value.response.status_code == 422
        error_body = exc_info.value.response.json()
        assert error_body["error"]["details"]["entities"] == ["PHONE_NUMBER"]
        assert "+79031234567" not in json.dumps(error_body, ensure_ascii=False)

    @pytest.mark.asyncio
    async def test_block_mode_rejects_pii_in_responses_tool_output_without_mutating(self):
        guardrail = RuPIIGuardrail(pii_mode="block")
        guardrail._redis = _mock_redis()
        tool_output = "Tool returned email test@example.com"
        data = {
            "model": "openai-gpt-5.4-mini",
            "input": [
                {
                    "type": "function_call_output",
                    "call_id": "call-1",
                    "output": tool_output,
                }
            ],
        }

        with patch.object(
            guardrail,
            "_analyze_text",
            return_value=[_entity(tool_output, "test@example.com", "EMAIL_ADDRESS")],
        ):
            with pytest.raises(litellm.UnprocessableEntityError) as exc_info:
                await guardrail.async_pre_call_hook(
                    user_api_key_dict=MagicMock(),
                    cache=MagicMock(),
                    data=data,
                    call_type="responses",
                )

        assert data["input"][0]["output"] == tool_output
        assert "metadata" not in data
        guardrail._redis.setex.assert_not_called()
        assert exc_info.value.response.status_code == 422
        error_body = exc_info.value.response.json()
        assert error_body["error"]["details"]["entities"] == ["EMAIL_ADDRESS"]
        assert "test@example.com" not in json.dumps(error_body, ensure_ascii=False)

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
            data = {
                "metadata": {
                    "pii_request_id": "caller-supplied",
                    "pii_streaming_restoration_done": True,
                },
                "messages": [{"role": "user", "content": "Расскажи joke"}],
            }

            result = await guardrail.async_pre_call_hook(
                user_api_key_dict=MagicMock(),
                cache=MagicMock(),
                data=data,
            )

        assert result["messages"][0]["content"] == "Расскажи joke"
        assert "pii_request_id" not in result["metadata"]
        assert "pii_streaming_restoration_done" not in result["metadata"]

    @pytest.mark.asyncio
    async def test_analyze_error_fails_open(self, guardrail):
        text = "Мой телефон +79031234567"
        with patch.object(guardrail, "_analyze_text", side_effect=Exception("connection error")):
            data = {
                "metadata": {"pii_request_id": "caller-supplied"},
                "messages": [{"role": "user", "content": text}],
            }

            result = await guardrail.async_pre_call_hook(
                user_api_key_dict=MagicMock(),
                cache=MagicMock(),
                data=data,
            )

        assert result["messages"][0]["content"] == text
        assert "pii_request_id" not in result["metadata"]

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
    async def test_ignores_client_request_id_and_call_id_when_mapping_id_missing(
        self,
        guardrail,
    ):
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
            data={
                "metadata": {"request_id": "client-controlled"},
                "litellm_call_id": "req-1",
            },
            user_api_key_dict=MagicMock(),
            response=response,
        )

        assert response.choices[0].message.content == "<PHONE_NUMBER_1>"
        guardrail._redis.get.assert_not_called()

    @pytest.mark.asyncio
    async def test_stream_true_model_response_still_restores_when_iterator_did_not_run(
        self,
        guardrail,
    ):
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
            data={"stream": True, "metadata": {"pii_request_id": "req-1"}},
            user_api_key_dict=MagicMock(),
            response=response,
        )

        assert response.choices[0].message.content == "+79031234567"
        guardrail._redis.delete.assert_awaited_once_with("pii_mapping:req-1")

    @pytest.mark.asyncio
    async def test_stream_true_model_response_skips_after_iterator_processed(
        self,
        guardrail,
    ):
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
            data={
                "stream": True,
                "metadata": {
                    "pii_request_id": "req-1",
                    "pii_streaming_restoration_done": True,
                },
            },
            user_api_key_dict=MagicMock(),
            response=response,
        )

        assert response.choices[0].message.content == "<PHONE_NUMBER_1>"
        guardrail._redis.get.assert_not_called()
        guardrail._redis.delete.assert_not_called()

    @pytest.mark.asyncio
    async def test_stream_true_non_model_response_skips_success_hook(
        self,
        guardrail,
    ):
        response = litellm.ModelResponseStream(
            choices=[
                litellm.StreamingChoices(
                    index=0,
                    delta={"content": "<PHONE_NUMBER_1>"},
                )
            ]
        )

        await guardrail.async_post_call_success_hook(
            data={"stream": True, "metadata": {"pii_request_id": "req-1"}},
            user_api_key_dict=MagicMock(),
            response=response,
        )

        guardrail._redis.get.assert_not_called()
        guardrail._redis.delete.assert_not_called()

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


# === async_post_call_streaming_iterator_hook ===


async def _collect_stream_text(stream):
    chunks = []
    async for chunk in stream:
        chunks.append(chunk)

    content_parts = []
    reasoning_parts = []
    for chunk in chunks:
        for choice in chunk.choices:
            delta = choice.delta
            if isinstance(getattr(delta, "content", None), str):
                content_parts.append(delta.content)
            if isinstance(getattr(delta, "reasoning_content", None), str):
                reasoning_parts.append(delta.reasoning_content)
    return chunks, "".join(content_parts), "".join(reasoning_parts)


async def _stream_chunks(chunks):
    for chunk in chunks:
        yield chunk


async def _broken_stream(first_chunk):
    yield first_chunk
    raise RuntimeError("upstream stream failed")


class TestStreamingPostCallHook:
    @pytest.mark.asyncio
    async def test_restores_placeholder_split_across_content_chunks_and_cleans_mapping(
        self,
        guardrail,
    ):
        mapping = {"<PHONE_NUMBER_1>": "+79031234567"}
        guardrail._redis.get.return_value = json.dumps(mapping)
        chunks = [
            litellm.ModelResponseStream(
                choices=[
                    litellm.StreamingChoices(
                        index=0,
                        delta={"content": "Телефон <PHONE_"},
                    )
                ]
            ),
            litellm.ModelResponseStream(
                choices=[
                    litellm.StreamingChoices(
                        index=0,
                        delta={"content": "NUMBER_1> подтвержден"},
                    )
                ]
            ),
            litellm.ModelResponseStream(
                choices=[litellm.StreamingChoices(index=0, finish_reason="stop")]
            ),
        ]

        request_data = {"metadata": {"pii_request_id": "req-1"}}
        result_stream = guardrail.async_post_call_streaming_iterator_hook(
            user_api_key_dict=MagicMock(),
            response=_stream_chunks(chunks),
            request_data=request_data,
        )
        yielded, content, _reasoning = await _collect_stream_text(result_stream)

        assert content == "Телефон +79031234567 подтвержден"
        yielded_content_parts = [
            getattr(choice.delta, "content", "") or ""
            for chunk in yielded
            for choice in chunk.choices
        ]
        assert all("<PHONE_NUMBER_1>" not in part for part in yielded_content_parts)
        assert request_data["metadata"]["pii_streaming_restoration_done"] is True
        guardrail._redis.delete.assert_awaited_once_with("pii_mapping:req-1")

    @pytest.mark.asyncio
    async def test_flushes_pending_placeholder_on_finish_chunk(self, guardrail):
        mapping = {"<PHONE_NUMBER_1>": "+79031234567"}
        guardrail._redis.get.return_value = json.dumps(mapping)
        chunks = [
            litellm.ModelResponseStream(
                choices=[
                    litellm.StreamingChoices(
                        index=0,
                        delta={"content": "<PHONE_NUMBER_1"},
                    )
                ]
            ),
            litellm.ModelResponseStream(
                choices=[
                    litellm.StreamingChoices(
                        index=0,
                        finish_reason="stop",
                        delta={"content": ">"},
                    )
                ]
            ),
        ]

        result_stream = guardrail.async_post_call_streaming_iterator_hook(
            user_api_key_dict=MagicMock(),
            response=_stream_chunks(chunks),
            request_data={"metadata": {"pii_request_id": "req-1"}},
        )
        _yielded, content, _reasoning = await _collect_stream_text(result_stream)

        assert content == "+79031234567"
        guardrail._redis.delete.assert_awaited_once_with("pii_mapping:req-1")

    @pytest.mark.asyncio
    async def test_restores_reasoning_content_stream(self, guardrail):
        mapping = {"<PERSON_1>": "Иван Иванов"}
        guardrail._redis.get.return_value = json.dumps(mapping)
        chunks = [
            litellm.ModelResponseStream(
                choices=[
                    litellm.StreamingChoices(
                        index=0,
                        delta={"reasoning_content": "Проверяю <PERSON"},
                    )
                ]
            ),
            litellm.ModelResponseStream(
                choices=[
                    litellm.StreamingChoices(
                        index=0,
                        delta={"reasoning_content": "_1>"},
                    )
                ]
            ),
            litellm.ModelResponseStream(
                choices=[litellm.StreamingChoices(index=0, finish_reason="stop")]
            ),
        ]

        result_stream = guardrail.async_post_call_streaming_iterator_hook(
            user_api_key_dict=MagicMock(),
            response=_stream_chunks(chunks),
            request_data={"metadata": {"pii_request_id": "req-1"}},
        )
        _yielded, _content, reasoning = await _collect_stream_text(result_stream)

        assert reasoning == "Проверяю Иван Иванов"
        guardrail._redis.delete.assert_awaited_once_with("pii_mapping:req-1")

    @pytest.mark.asyncio
    async def test_streaming_mapping_load_error_fails_open(self, guardrail):
        guardrail._redis.get.side_effect = RuntimeError("redis down")
        chunks = [
            litellm.ModelResponseStream(
                choices=[
                    litellm.StreamingChoices(
                        index=0,
                        delta={"content": "<PHONE_NUMBER_1>"},
                    )
                ]
            )
        ]

        result_stream = guardrail.async_post_call_streaming_iterator_hook(
            user_api_key_dict=MagicMock(),
            response=_stream_chunks(chunks),
            request_data={"metadata": {"pii_request_id": "req-1"}},
        )
        _yielded, content, _reasoning = await _collect_stream_text(result_stream)

        assert content == "<PHONE_NUMBER_1>"
        guardrail._redis.delete.assert_not_called()

    @pytest.mark.asyncio
    async def test_streaming_mapping_load_error_fails_closed(self):
        guardrail = RuPIIGuardrail(failure_mode="fail_closed")
        guardrail._redis = _mock_redis()
        guardrail._redis.get.side_effect = RuntimeError("redis down")
        chunks = [
            litellm.ModelResponseStream(
                choices=[
                    litellm.StreamingChoices(
                        index=0,
                        delta={"content": "<PHONE_NUMBER_1>"},
                    )
                ]
            )
        ]

        result_stream = guardrail.async_post_call_streaming_iterator_hook(
            user_api_key_dict=MagicMock(),
            response=_stream_chunks(chunks),
            request_data={"metadata": {"pii_request_id": "req-1"}},
        )

        with pytest.raises(RuntimeError, match="PII guardrail stream mapping load failed"):
            await anext(result_stream)

        guardrail._redis.delete.assert_not_called()

    @pytest.mark.asyncio
    async def test_streaming_deletes_mapping_when_upstream_iterator_fails(
        self,
        guardrail,
    ):
        mapping = {"<PHONE_NUMBER_1>": "+79031234567"}
        guardrail._redis.get.return_value = json.dumps(mapping)
        first_chunk = litellm.ModelResponseStream(
            choices=[
                litellm.StreamingChoices(
                    index=0,
                    delta={"content": "Начало "},
                )
            ]
        )

        result_stream = guardrail.async_post_call_streaming_iterator_hook(
            user_api_key_dict=MagicMock(),
            response=_broken_stream(first_chunk),
            request_data={"metadata": {"pii_request_id": "req-1"}},
        )

        first_result = await anext(result_stream)
        assert first_result.choices[0].delta.content == "Начало "

        with pytest.raises(RuntimeError, match="upstream stream failed"):
            await anext(result_stream)

        guardrail._redis.delete.assert_awaited_once_with("pii_mapping:req-1")


# === _replace_placeholders ===


class TestReplacePlaceholders:
    def test_replaces_longer_placeholders_first(self, guardrail):
        mapping = {
            "<PERSON_1>": "Иван",
            "<PERSON_10>": "Петр",
        }

        result = guardrail._replace_placeholders("<PERSON_10> и <PERSON_1>", mapping)

        assert result == "Петр и Иван"
