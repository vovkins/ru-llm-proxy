"""Unit tests for PII guardrail module."""

import json
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from litellm_guardrails.pii_guardrail import RuPIIGuardrail


@pytest.fixture
def guardrail():
    """Create a guardrail instance with mocked Redis."""
    g = RuPIIGuardrail()
    g._redis = AsyncMock()
    g._redis.setex = AsyncMock()
    g._redis.get = AsyncMock(return_value=None)
    g._redis.delete = AsyncMock()
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
        data = {}
        result = guardrail._get_request_id(data)
        assert isinstance(result, str)
        assert len(result) > 0

    def test_metadata_takes_priority(self, guardrail):
        data = {"metadata": {"request_id": "from-meta"}, "litellm_call_id": "from-call"}
        assert guardrail._get_request_id(data) == "from-meta"


# === _analyze_text ===

class TestAnalyzeText:
    @pytest.mark.asyncio
    async def test_returns_entities(self, guardrail):
        mock_response = MagicMock()
        mock_response.json.return_value = {
            "entities": [
                {"entity_type": "PHONE_NUMBER", "start": 0, "end": 16, "score": 1.0, "text": "+7 903 123 45 67"}
            ]
        }
        mock_response.raise_for_status = MagicMock()

        with patch("litellm_guardrails.pii_guardrail.httpx.AsyncClient") as mock_client:
            mock_client.return_value.__aenter__ = AsyncMock(return_value=mock_response)
            mock_client.return_value.__aexit__ = AsyncMock(return_value=False)
            mock_response.post = AsyncMock()  # not used directly
            # The AsyncClient is used as context manager, so we need to set up post on the instance
            mock_instance = AsyncMock()
            mock_instance.post.return_value = mock_response
            mock_client.return_value = mock_instance
            mock_instance.__aenter__ = AsyncMock(return_value=mock_instance)
            mock_instance.__aexit__ = AsyncMock(return_value=False)

            result = await guardrail._analyze_text("Мой телефон +7 903 123 45 67")
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


# === _anonymize_text ===

class TestAnonymizeText:
    @pytest.mark.asyncio
    async def test_anonymizes_and_builds_mapping(self, guardrail):
        entities = [
            {"entity_type": "PHONE_NUMBER", "start": 0, "end": 16, "score": 1.0, "text": "+7 903 123 45 67"}
        ]
        mock_response = MagicMock()
        mock_response.json.return_value = {
            "text": "Мой телефон <PHONE_NUMBER>",
            "items": [
                {"text": "<PHONE_NUMBER>", "start": 12, "end": 27, "entity_type": "PHONE_NUMBER", "operator": "replace"}
            ]
        }
        mock_response.raise_for_status = MagicMock()

        mock_instance = AsyncMock()
        mock_instance.post.return_value = mock_response
        mock_instance.__aenter__ = AsyncMock(return_value=mock_instance)
        mock_instance.__aexit__ = AsyncMock(return_value=False)

        with patch("litellm_guardrails.pii_guardrail.httpx.AsyncClient", return_value=mock_instance):
            masked_text, mapping = await guardrail._anonymize_text(
                "Мой телефон +7 903 123 45 67", entities
            )
            assert "<PHONE_NUMBER>" in masked_text
            # mapping should map placeholder back to original
            assert "<PHONE_NUMBER>" in mapping

    @pytest.mark.asyncio
    async def test_no_entities_returns_original(self, guardrail):
        masked_text, mapping = await guardrail._anonymize_text("Обычный текст", [])
        assert masked_text == "Обычный текст"
        assert mapping == {}


# === _save_mapping / _load_mapping ===

class TestRedisMapping:
    @pytest.mark.asyncio
    async def test_save_mapping(self, guardrail):
        await guardrail._save_mapping("req-1", {"<PHONE>": "+7 903 123 45 67"})
        guardrail._redis.setex.assert_called_once()
        args = guardrail._redis.setex.call_args[0]
        assert args[0] == "pii_mapping:req-1"
        assert args[1] == 3600
        assert json.loads(args[2]) == {"<PHONE>": "+7 903 123 45 67"}

    @pytest.mark.asyncio
    async def test_save_empty_mapping_skips(self, guardrail):
        await guardrail._save_mapping("req-1", {})
        guardrail._redis.setex.assert_not_called()

    @pytest.mark.asyncio
    async def test_load_mapping_returns_dict(self, guardrail):
        guardrail._redis.get.return_value = json.dumps({"<PHONE>": "+7 903 123 45 67"})
        result = await guardrail._load_mapping("req-1")
        assert result == {"<PHONE>": "+7 903 123 45 67"}

    @pytest.mark.asyncio
    async def test_load_mapping_returns_empty_on_miss(self, guardrail):
        guardrail._redis.get.return_value = None
        result = await guardrail._load_mapping("req-1")
        assert result == {}


# === async_pre_call_hook ===

class TestPreCallHook:
    @pytest.mark.asyncio
    async def test_masks_pii_in_messages(self, guardrail):
        """Verify pre-call hook masks PII and saves mapping."""
        # Mock analyze to return a phone entity
        analyze_entities = [
            {"entity_type": "PHONE_NUMBER", "start": 0, "end": 16, "score": 1.0, "text": "+7 903 123 45 67"}
        ]

        with patch.object(guardrail, "_analyze_text", return_value=analyze_entities):
            with patch.object(guardrail, "_anonymize_text") as mock_anon:
                mock_anon.return_value = (
                    "Мой телефон <PHONE_NUMBER_0>",
                    {"<PHONE_NUMBER_0>": "+7 903 123 45 67"}
                )
                with patch.object(guardrail, "_save_mapping", new_callable=AsyncMock):
                    data = {
                        "messages": [
                            {"role": "user", "content": "Мой телефон +7 903 123 45 67"}
                        ]
                    }
                    result = await guardrail.async_pre_call_hook(
                        user_api_key_dict=MagicMock(),
                        cache=MagicMock(),
                        data=data,
                    )
                    assert result is not None
                    assert "<PHONE_NUMBER_0>" in result["messages"][0]["content"]
                    assert "+7 903 123 45 67" not in result["messages"][0]["content"]

    @pytest.mark.asyncio
    async def test_no_messages_returns_data(self, guardrail):
        """If no messages, should return data unchanged."""
        data = {"model": "glm-5.1"}
        result = await guardrail.async_pre_call_hook(
            user_api_key_dict=MagicMock(),
            cache=MagicMock(),
            data=data,
        )
        assert result == data

    @pytest.mark.asyncio
    async def test_no_pii_passes_through(self, guardrail):
        """If no PII found, message should pass unchanged."""
        with patch.object(guardrail, "_analyze_text", return_value=[]):
            data = {
                "messages": [
                    {"role": "user", "content": "Расскажи joke"}
                ]
            }
            result = await guardrail.async_pre_call_hook(
                user_api_key_dict=MagicMock(),
                cache=MagicMock(),
                data=data,
            )
            assert result["messages"][0]["content"] == "Расскажи joke"

    @pytest.mark.asyncio
    async def test_analyze_error_fails_open(self, guardrail):
        """On analyzer error, message should pass unchanged (fail open)."""
        with patch.object(guardrail, "_analyze_text", side_effect=Exception("connection error")):
            data = {
                "messages": [
                    {"role": "user", "content": "Мой телефон +7 903 123 45 67"}
                ]
            }
            result = await guardrail.async_pre_call_hook(
                user_api_key_dict=MagicMock(),
                cache=MagicMock(),
                data=data,
            )
            # Fail open: original text unchanged
            assert result["messages"][0]["content"] == "Мой телефон +7 903 123 45 67"


# === async_post_call_success_hook ===

class TestPostCallHook:
    @pytest.mark.asyncio
    async def test_unmasks_response(self, guardrail):
        """Verify post-call hook replaces placeholders with originals."""
        import litellm

        mapping = {"<PHONE_NUMBER_0>": "+7 903 123 45 67"}
        guardrail._redis.get.return_value = json.dumps(mapping)

        response = litellm.ModelResponse(
            id="test",
            choices=[
                litellm.Choices(
                    index=0,
                    message=litellm.Message(
                        role="assistant",
                        content="Ваш телефон <PHONE_NUMBER_0> подтверждён"
                    ),
                    finish_reason="stop",
                )
            ],
        )

        data = {"metadata": {"pii_request_id": "req-1"}}
        await guardrail.async_post_call_success_hook(
            data=data,
            user_api_key_dict=MagicMock(),
            response=response,
        )

        assert "+7 903 123 45 67" in response.choices[0].message.content
        assert "<PHONE_NUMBER_0>" not in response.choices[0].message.content

    @pytest.mark.asyncio
    async def test_no_request_id_skips(self, guardrail):
        """If no pii_request_id in metadata, should skip."""
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

        data = {"metadata": {}}
        await guardrail.async_post_call_success_hook(
            data=data,
            user_api_key_dict=MagicMock(),
            response=response,
        )
        # Content unchanged
        assert response.choices[0].message.content == "test"
