"""Unit tests for PII guardrail module."""

import asyncio
import json
import logging
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import litellm
import pytest
import pytest_asyncio

import litellm_guardrails.pii_guardrail as pii_guardrail
from litellm_guardrails.pii_guardrail import (
    AnalyzerOverloadedError,
    AnalyzerUnavailableError,
    HTTPException,
    ProxyException,
    RuPIIGuardrail,
)


PRE_EGRESS_BLOCK_EXCEPTION_TYPES = (HTTPException, ProxyException)
NER_GUARDRAIL_CASES = (
    pytest.param("PERSON", "Олег Волков", id="person"),
    pytest.param("LOCATION", "Туле", id="location"),
    pytest.param("ORGANIZATION", "ООО Север", id="organization"),
    pytest.param("LOGIN", "oleg.volkov", id="login"),
    pytest.param("PASSWORD", "Mix3d-Value!", id="password"),
    pytest.param(
        "AUTH_TOKEN",
        "mixed-auth-token-00073",
        id="auth-token",
    ),
    pytest.param(
        "SECRET_KEY",
        "mixed-secret-key-00084",
        id="secret-key",
    ),
    pytest.param("CONTRACT_NUMBER", "OV-2026/81", id="contract-number"),
)


@pytest_asyncio.fixture(autouse=True)
async def dependency_client_cache_isolation():
    await pii_guardrail.close_guardrail_dependency_clients()
    yield
    await pii_guardrail.close_guardrail_dependency_clients()


def _entity(text, value, entity_type="PHONE_NUMBER", score=1.0):
    start = text.index(value)
    return {
        "entity_type": entity_type,
        "start": start,
        "end": start + len(value),
        "score": score,
        "text": value,
    }


def _synthetic_allowlist(*rules):
    return json.dumps(list(rules), ensure_ascii=False)


def _dictionary_substitutions(*rules):
    return json.dumps({"substitutions": list(rules)}, ensure_ascii=False)


def _mock_redis(get_value=None):
    redis = AsyncMock()
    redis.setex = AsyncMock()
    redis.get = AsyncMock(return_value=get_value)
    redis.delete = AsyncMock()
    return redis


def _responses_api_response(output):
    return litellm.ResponsesAPIResponse(
        id="resp-test",
        created_at=1,
        model="mock-chat",
        object="response",
        status="completed",
        output=output,
    )


def _error_body_from_exception(error):
    if isinstance(error, HTTPException):
        detail = error.detail
        if isinstance(detail, dict) and "error" in detail:
            return detail
        return {"error": detail}
    if isinstance(error, ProxyException):
        provider_fields = error.provider_specific_fields or {}
        structured_error = provider_fields.get("error")
        if isinstance(structured_error, dict):
            return {"error": structured_error}
        return {"error": error.to_dict()}
    return error.response.json()


def _status_code_from_exception(error):
    if isinstance(error, HTTPException):
        return error.status_code
    if isinstance(error, ProxyException):
        return int(error.status_code)
    return error.response.status_code


def _json_log_events(caplog, event):
    events = []
    for record in caplog.records:
        try:
            payload = json.loads(record.getMessage())
        except json.JSONDecodeError:
            continue
        if payload.get("event") == event:
            events.append(payload)
    return events


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
    def test_defaults_to_fail_closed_for_unknown_value(self):
        guardrail = RuPIIGuardrail(failure_mode="bad-value")
        assert guardrail.failure_mode == "fail_closed"

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


# === synthetic/test PII allowlist mode ===


class TestSyntheticPIIAllowlistMode:
    def test_defaults_to_off_mode_and_no_rules(self):
        guardrail = RuPIIGuardrail()
        assert guardrail.synthetic_pii_allowlist_mode == "off"
        assert guardrail.synthetic_pii_allowlist_rules == []

    def test_accepts_allow_alias(self):
        guardrail = RuPIIGuardrail(synthetic_pii_allowlist_mode="enabled")
        assert guardrail.synthetic_pii_allowlist_mode == "allow"

    def test_defaults_to_off_for_unknown_value(self):
        guardrail = RuPIIGuardrail(synthetic_pii_allowlist_mode="bad-value")
        assert guardrail.synthetic_pii_allowlist_mode == "off"

    def test_loads_exact_values_and_safe_patterns(self):
        guardrail = RuPIIGuardrail(
            synthetic_pii_allowlist_mode="allow",
            synthetic_pii_allowlist_json=_synthetic_allowlist(
                {
                    "rule_id": "docs_synthetic_contacts",
                    "entity_types": ["PHONE_NUMBER", "EMAIL_ADDRESS"],
                    "values": ["+79031234567"],
                    "patterns": [r"^[A-Za-z0-9._%+-]+@example\.test$"],
                }
            ),
        )

        assert len(guardrail.synthetic_pii_allowlist_rules) == 1
        rule = guardrail.synthetic_pii_allowlist_rules[0]
        assert rule["rule_id"] == "docs_synthetic_contacts"
        assert rule["entity_types"] == frozenset({"PHONE_NUMBER", "EMAIL_ADDRESS"})
        assert rule["values"] == frozenset({"+79031234567"})
        assert len(rule["patterns"]) == 1

    def test_ignores_invalid_json_and_unsafe_patterns(self):
        invalid = RuPIIGuardrail(
            synthetic_pii_allowlist_mode="allow",
            synthetic_pii_allowlist_json="{bad json",
        )
        unsafe = RuPIIGuardrail(
            synthetic_pii_allowlist_mode="allow",
            synthetic_pii_allowlist_json=_synthetic_allowlist(
                {
                    "rule_id": "unsafe",
                    "entity_types": ["EMAIL_ADDRESS"],
                    "patterns": ["^.*$"],
                }
            ),
        )

        assert invalid.synthetic_pii_allowlist_rules == []
        assert unsafe.synthetic_pii_allowlist_rules == []

    def test_ignores_non_pii_policy_rules(self):
        guardrail = RuPIIGuardrail(
            synthetic_pii_allowlist_mode="allow",
            synthetic_pii_allowlist_json=_synthetic_allowlist(
                {
                    "rule_id": "final_only",
                    "policies": ["final_payload"],
                    "entity_types": ["PHONE_NUMBER"],
                    "values": ["+79031234567"],
                }
            ),
        )

        assert guardrail.synthetic_pii_allowlist_rules == []


# === regulated-topic policy mode ===


class TestRegulatedTopicPolicyMode:
    def test_defaults_to_off_mode(self):
        guardrail = RuPIIGuardrail()
        assert guardrail.regulated_topic_policy_mode == "off"

    def test_accepts_block_mode_alias(self):
        guardrail = RuPIIGuardrail(regulated_topic_policy_mode="enabled")
        assert guardrail.regulated_topic_policy_mode == "block"

    def test_defaults_to_off_for_unknown_value(self):
        guardrail = RuPIIGuardrail(regulated_topic_policy_mode="bad-value")
        assert guardrail.regulated_topic_policy_mode == "off"


# === pre-egress policy mode ===


class TestPreEgressPolicyMode:
    def test_defaults_to_block_mode(self):
        guardrail = RuPIIGuardrail()
        assert guardrail.pre_egress_policy_mode == "block"

    def test_accepts_off_alias(self):
        guardrail = RuPIIGuardrail(pre_egress_policy_mode="off")
        assert guardrail.pre_egress_policy_mode == "off"

    def test_defaults_to_block_for_unknown_value(self):
        guardrail = RuPIIGuardrail(pre_egress_policy_mode="bad-value")
        assert guardrail.pre_egress_policy_mode == "block"


# === final payload leak-check mode ===


class TestFinalPayloadLeakCheckMode:
    def test_defaults_to_block_mode(self):
        guardrail = RuPIIGuardrail()
        assert guardrail.final_payload_leak_check_mode == "block"

    def test_accepts_off_alias(self):
        guardrail = RuPIIGuardrail(final_payload_leak_check_mode="off")
        assert guardrail.final_payload_leak_check_mode == "off"

    def test_defaults_to_block_for_unknown_value(self):
        guardrail = RuPIIGuardrail(final_payload_leak_check_mode="bad-value")
        assert guardrail.final_payload_leak_check_mode == "block"


# === _analyze_text ===


class TestAnalyzeText:
    @staticmethod
    def _mock_analyzer_response(entities=None):
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"entities": entities or []}
        mock_response.raise_for_status = MagicMock()
        return mock_response

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

    @pytest.mark.asyncio
    async def test_raises_analyzer_unavailable_for_required_ner_503(self, guardrail):
        mock_response = MagicMock()
        mock_response.status_code = 503
        mock_response.json.return_value = {
            "detail": {
                "code": "required_ner_unavailable",
                "phase": "inference",
                "failure_class": "forward_pass_failed",
            }
        }
        mock_response.raise_for_status = MagicMock()

        client = AsyncMock()
        client.post.return_value = mock_response

        with patch(
            "litellm_guardrails.pii_guardrail._get_shared_analyzer_http_client",
            return_value=client,
        ):
            with pytest.raises(AnalyzerUnavailableError) as exc_info:
                await guardrail._analyze_text("PASSWORD=raw-secret")

        assert exc_info.value.phase == "inference"
        assert exc_info.value.failure_class == "forward_pass_failed"
        assert "raw-secret" not in str(exc_info.value)
        mock_response.raise_for_status.assert_not_called()

    @pytest.mark.asyncio
    async def test_analyzer_unavailable_normalizes_untrusted_categories(self, guardrail):
        mock_response = MagicMock()
        mock_response.status_code = 503
        mock_response.json.return_value = {
            "detail": {
                "code": "required_ner_unavailable",
                "phase": "prompt-derived-phase",
                "failure_class": "prompt-derived-class",
            }
        }
        client = AsyncMock()
        client.post.return_value = mock_response

        with patch(
            "litellm_guardrails.pii_guardrail._get_shared_analyzer_http_client",
            return_value=client,
        ):
            with pytest.raises(AnalyzerUnavailableError) as exc_info:
                await guardrail._analyze_text("Обычный текст")

        assert exc_info.value.phase == "readiness"
        assert exc_info.value.failure_class == "unexpected_failure"

    @pytest.mark.asyncio
    async def test_unknown_analyzer_503_is_always_unavailable(self, guardrail):
        mock_response = MagicMock()
        mock_response.status_code = 503
        mock_response.json.side_effect = ValueError("invalid intermediary response")
        mock_response.raise_for_status = MagicMock()
        client = AsyncMock()
        client.post.return_value = mock_response

        with patch(
            "litellm_guardrails.pii_guardrail._get_shared_analyzer_http_client",
            return_value=client,
        ):
            with pytest.raises(AnalyzerUnavailableError) as exc_info:
                await guardrail._analyze_text("Обычный текст")

        assert exc_info.value.phase == "readiness"
        assert exc_info.value.failure_class == "unexpected_failure"
        mock_response.raise_for_status.assert_not_called()


# === dependency clients ===


class TestDependencyClients:
    @pytest.mark.asyncio
    async def test_redis_client_is_shared_across_guardrail_instances(self):
        redis_client = AsyncMock()

        with patch("redis.asyncio.from_url", return_value=redis_client) as from_url:
            first = RuPIIGuardrail()
            second = RuPIIGuardrail()

            first_client = await first._get_redis()
            second_client = await second._get_redis()

        assert first_client is redis_client
        assert second_client is redis_client
        from_url.assert_called_once_with(
            pii_guardrail.REDIS_URL,
            decode_responses=True,
            max_connections=20,
            socket_connect_timeout=1.0,
            socket_timeout=2.0,
        )

    @pytest.mark.asyncio
    async def test_instance_redis_override_is_still_used_for_tests(self):
        override = _mock_redis()
        guardrail = RuPIIGuardrail()
        guardrail._redis = override

        with patch("redis.asyncio.from_url") as from_url:
            redis_client = await guardrail._get_redis()

        assert redis_client is override
        from_url.assert_not_called()

    @pytest.mark.asyncio
    async def test_analyzer_http_client_is_shared_across_guardrail_instances(self):
        response = TestAnalyzeText._mock_analyzer_response()
        client = AsyncMock()
        client.post.return_value = response
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock(return_value=False)

        with patch(
            "litellm_guardrails.pii_guardrail.httpx.AsyncClient",
            return_value=client,
        ) as async_client:
            first = RuPIIGuardrail()
            second = RuPIIGuardrail()

            await first._analyze_text("Обычный текст")
            await second._analyze_text("Еще один текст")

        async_client.assert_called_once()
        kwargs = async_client.call_args.kwargs
        assert isinstance(kwargs["timeout"], httpx.Timeout)
        assert kwargs["timeout"].connect == 5.0
        assert kwargs["timeout"].read == 30.0
        assert isinstance(kwargs["limits"], httpx.Limits)
        assert kwargs["limits"].max_connections == 20
        assert kwargs["limits"].max_keepalive_connections == 10

    @pytest.mark.asyncio
    async def test_close_dependency_clients_closes_cached_clients_and_clears_cache(self):
        loop = asyncio.get_running_loop()
        redis_client = AsyncMock()
        analyzer_client = AsyncMock()
        pii_guardrail._REDIS_CLIENTS_BY_LOOP[loop] = redis_client
        pii_guardrail._ANALYZER_HTTP_CLIENTS_BY_LOOP[loop] = analyzer_client

        await pii_guardrail.close_guardrail_dependency_clients()

        redis_client.aclose.assert_awaited_once_with(close_connection_pool=True)
        analyzer_client.aclose.assert_awaited_once_with()
        assert len(pii_guardrail._REDIS_CLIENTS_BY_LOOP) == 0
        assert len(pii_guardrail._ANALYZER_HTTP_CLIENTS_BY_LOOP) == 0

    @pytest.mark.asyncio
    async def test_close_dependency_clients_logs_fallback_close_errors(self, caplog):
        class LegacyRedisClose:
            def __init__(self):
                self.close_connection_pool_values = []

            async def aclose(self, close_connection_pool=None):
                self.close_connection_pool_values.append(close_connection_pool)
                if close_connection_pool is not None:
                    raise TypeError("unsupported keyword")
                raise RuntimeError("close failed")

        loop = asyncio.get_running_loop()
        redis_client = LegacyRedisClose()
        pii_guardrail._REDIS_CLIENTS_BY_LOOP[loop] = redis_client

        with caplog.at_level(logging.WARNING):
            await pii_guardrail.close_guardrail_dependency_clients()

        assert redis_client.close_connection_pool_values == [True, None]
        assert "pii_guardrail_dependency_client_close_failed" in caplog.text
        assert len(pii_guardrail._REDIS_CLIENTS_BY_LOOP) == 0

    @pytest.mark.asyncio
    async def test_dependency_clients_are_recreated_after_close(self):
        first_redis = AsyncMock()
        second_redis = AsyncMock()
        first_response = TestAnalyzeText._mock_analyzer_response()
        second_response = TestAnalyzeText._mock_analyzer_response()
        first_http = AsyncMock()
        first_http.post.return_value = first_response
        second_http = AsyncMock()
        second_http.post.return_value = second_response

        with (
            patch(
                "redis.asyncio.from_url",
                side_effect=[first_redis, second_redis],
            ) as from_url,
            patch(
                "litellm_guardrails.pii_guardrail.httpx.AsyncClient",
                side_effect=[first_http, second_http],
            ) as async_client,
        ):
            first = RuPIIGuardrail()
            assert await first._get_redis() is first_redis
            await first._analyze_text("Обычный текст")

            await pii_guardrail.close_guardrail_dependency_clients()

            second = RuPIIGuardrail()
            assert await second._get_redis() is second_redis
            await second._analyze_text("Еще один текст")

        assert from_url.call_count == 2
        assert async_client.call_count == 2
        first_redis.aclose.assert_awaited_once_with(close_connection_pool=True)
        first_http.aclose.assert_awaited_once_with()

    def test_dependency_client_config_defaults_are_explicit(self):
        assert pii_guardrail.PII_GUARDRAIL_REDIS_MAX_CONNECTIONS == 20
        assert (
            pii_guardrail.PII_GUARDRAIL_REDIS_SOCKET_CONNECT_TIMEOUT_SECONDS
            == 1.0
        )
        assert pii_guardrail.PII_GUARDRAIL_REDIS_SOCKET_TIMEOUT_SECONDS == 2.0
        assert pii_guardrail.PII_GUARDRAIL_ANALYZER_TIMEOUT_SECONDS == 30.0
        assert pii_guardrail.PII_GUARDRAIL_ANALYZER_CONNECT_TIMEOUT_SECONDS == 5.0
        assert pii_guardrail.PII_GUARDRAIL_ANALYZER_MAX_CONNECTIONS == 20
        assert pii_guardrail.PII_GUARDRAIL_ANALYZER_MAX_KEEPALIVE_CONNECTIONS == 10

    def test_float_env_parser_falls_back_for_invalid_values(self, monkeypatch, caplog):
        monkeypatch.setenv("PII_TEST_FLOAT", "not-a-float")

        with caplog.at_level(logging.WARNING):
            value = pii_guardrail._get_float_env("PII_TEST_FLOAT", 1.5)

        assert value == 1.5
        assert "Invalid PII_TEST_FLOAT" in caplog.text


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

    def test_masks_custom_requisite_entity_names(self, guardrail):
        text = "БИК 044525225, расчетный счет 40702810900000000000"
        entities = [
            _entity(text, "044525225", "RU_BIK"),
            _entity(text, "40702810900000000000", "RU_SETTLEMENT_ACCOUNT"),
        ]

        masked_text, mapping = guardrail._mask_text(text, entities)

        assert masked_text == (
            "БИК <RU_BIK_1>, расчетный счет <RU_SETTLEMENT_ACCOUNT_1>"
        )
        assert mapping == {
            "<RU_BIK_1>": "044525225",
            "<RU_SETTLEMENT_ACCOUNT_1>": "40702810900000000000",
        }

    def test_masks_infrastructure_secret_entity_names(self, guardrail):
        text = (
            "Endpoint 10.24.3.7 uses Authorization: Bearer "
            "abcdefghijklmnopqrstuvwxyz123456"
        )
        bearer = "Authorization: Bearer abcdefghijklmnopqrstuvwxyz123456"
        entities = [
            _entity(text, "10.24.3.7", "INTERNAL_IP"),
            _entity(text, bearer, "BEARER_TOKEN"),
        ]

        masked_text, mapping = guardrail._mask_text(text, entities)

        assert masked_text == "Endpoint <INTERNAL_IP_1> uses <BEARER_TOKEN_1>"
        assert mapping == {
            "<INTERNAL_IP_1>": "10.24.3.7",
            "<BEARER_TOKEN_1>": bearer,
        }

    @pytest.mark.parametrize("entity_type,value", NER_GUARDRAIL_CASES)
    def test_masks_every_ner_entity_type(self, guardrail, entity_type, value):
        text = f"Значение: {value}."

        masked_text, mapping = guardrail._mask_text(
            text,
            [_entity(text, value, entity_type)],
        )

        placeholder = f"<{entity_type}_1>"
        assert masked_text == f"Значение: {placeholder}."
        assert mapping == {placeholder: value}


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
    async def test_dictionary_substitution_runs_before_analyzer_and_saves_mapping(
        self,
    ):
        guardrail = RuPIIGuardrail(
            dictionary_substitutions_enabled=True,
            dictionary_substitutions_file="",
            dictionary_substitutions_json=_dictionary_substitutions(
                {
                    "id": "tbank_to_zetta",
                    "source": "Т-Банк",
                    "replacement": "Зетта Групп",
                    "match": {"case_sensitive": False, "whole_phrase": True},
                    "restore": True,
                }
            ),
        )
        guardrail._redis = _mock_redis()
        save_mapping = AsyncMock()

        with patch.object(guardrail, "_analyze_text", return_value=[]) as analyze:
            with patch.object(guardrail, "_save_mapping", save_mapping):
                data = {"messages": [{"role": "user", "content": "Проверь Т-Банк"}]}

                result = await guardrail.async_pre_call_hook(
                    user_api_key_dict=MagicMock(),
                    cache=MagicMock(),
                    data=data,
                )

        assert result["messages"][0]["content"] == "Проверь Зетта Групп"
        analyze.assert_awaited_once_with("Проверь Зетта Групп")
        assert result["metadata"]["pii_request_id"]
        assert save_mapping.call_args[0][1] == {"Зетта Групп": "Т-Банк"}

    @pytest.mark.asyncio
    async def test_dictionary_replacement_span_is_not_remasked_as_pii(self):
        guardrail = RuPIIGuardrail(
            dictionary_substitutions_enabled=True,
            dictionary_substitutions_file="",
            dictionary_substitutions_json=_dictionary_substitutions(
                {
                    "id": "tbank_to_zetta",
                    "source": "Т-Банк",
                    "replacement": "Зетта Групп",
                }
            ),
        )
        guardrail._redis = _mock_redis()
        provider_text = "Проверь Зетта Групп"
        save_mapping = AsyncMock()

        with patch.object(
            guardrail,
            "_analyze_text",
            return_value=[_entity(provider_text, "Зетта Групп", "ORGANIZATION")],
        ):
            with patch.object(guardrail, "_save_mapping", save_mapping):
                data = {"messages": [{"role": "user", "content": "Проверь Т-Банк"}]}

                result = await guardrail.async_pre_call_hook(
                    user_api_key_dict=MagicMock(),
                    cache=MagicMock(),
                    data=data,
                )

        assert result["messages"][0]["content"] == provider_text
        assert "<ORGANIZATION_1>" not in result["messages"][0]["content"]
        assert save_mapping.call_args[0][1] == {"Зетта Групп": "Т-Банк"}

    @pytest.mark.asyncio
    async def test_dictionary_substitution_block_mode_allows_replacement_span(self):
        guardrail = RuPIIGuardrail(
            pii_mode="block",
            dictionary_substitutions_enabled=True,
            dictionary_substitutions_file="",
            dictionary_substitutions_json=_dictionary_substitutions(
                {
                    "id": "tbank_to_zetta",
                    "source": "Т-Банк",
                    "replacement": "Зетта Групп",
                }
            ),
        )
        guardrail._redis = _mock_redis()
        provider_text = "Проверь Зетта Групп"
        save_mapping = AsyncMock()

        with patch.object(
            guardrail,
            "_analyze_text",
            return_value=[_entity(provider_text, "Зетта Групп", "ORGANIZATION")],
        ):
            with patch.object(guardrail, "_save_mapping", save_mapping):
                data = {"messages": [{"role": "user", "content": "Проверь Т-Банк"}]}

                result = await guardrail.async_pre_call_hook(
                    user_api_key_dict=MagicMock(),
                    cache=MagicMock(),
                    data=data,
                )

        assert result["messages"][0]["content"] == provider_text
        assert save_mapping.call_args[0][1] == {"Зетта Групп": "Т-Банк"}

    @pytest.mark.asyncio
    async def test_dictionary_substitution_ambiguous_request_fails_closed(self):
        guardrail = RuPIIGuardrail(
            dictionary_substitutions_enabled=True,
            dictionary_substitutions_file="",
            dictionary_substitutions_failure_mode="fail_closed",
            dictionary_substitutions_json=_dictionary_substitutions(
                {
                    "id": "tbank_to_zetta",
                    "source": "Т-Банк",
                    "replacement": "Зетта Групп",
                }
            ),
        )
        guardrail._redis = _mock_redis()

        with patch.object(guardrail, "_analyze_text", AsyncMock()) as analyze:
            with pytest.raises(
                RuntimeError,
                match="Dictionary substitution ambiguous request failed",
            ):
                await guardrail.async_pre_call_hook(
                    user_api_key_dict=MagicMock(),
                    cache=MagicMock(),
                    data={
                        "messages": [
                            {
                                "role": "user",
                                "content": "Сравни Т-Банк и Зетта Групп",
                            }
                        ]
                    },
                )

        analyze.assert_not_called()

    @pytest.mark.asyncio
    async def test_synthetic_allowlist_keeps_exact_value_and_masks_other_pii(self):
        guardrail = RuPIIGuardrail(
            synthetic_pii_allowlist_mode="allow",
            synthetic_pii_allowlist_json=_synthetic_allowlist(
                {
                    "rule_id": "docs_synthetic_phone",
                    "entity_types": ["PHONE_NUMBER"],
                    "values": ["+79031234567"],
                }
            ),
        )
        guardrail._redis = _mock_redis()
        text = "Тестовый телефон +79031234567, реальный +79035551234"
        save_mapping = AsyncMock()

        with patch.object(
            guardrail,
            "_analyze_text",
            return_value=[
                _entity(text, "+79031234567"),
                _entity(text, "+79035551234"),
            ],
        ):
            with patch.object(guardrail, "_save_mapping", save_mapping):
                data = {"messages": [{"role": "user", "content": text}]}

                result = await guardrail.async_pre_call_hook(
                    user_api_key_dict=MagicMock(),
                    cache=MagicMock(),
                    data=data,
                )

        assert result["messages"][0]["content"] == (
            "Тестовый телефон +79031234567, реальный <PHONE_NUMBER_1>"
        )
        assert save_mapping.call_args[0][1] == {
            "<PHONE_NUMBER_1>": "+79035551234",
        }

    @pytest.mark.asyncio
    async def test_synthetic_allowlist_block_mode_allows_allowlisted_only_pii(
        self,
        caplog,
    ):
        guardrail = RuPIIGuardrail(
            pii_mode="block",
            synthetic_pii_allowlist_mode="allow",
            synthetic_pii_allowlist_json=_synthetic_allowlist(
                {
                    "rule_id": "docs_synthetic_phone",
                    "entity_types": ["PHONE_NUMBER"],
                    "values": ["+79031234567"],
                }
            ),
        )
        guardrail._redis = _mock_redis()
        text = "Тестовый телефон +79031234567"
        metric_label = MagicMock()
        metric = MagicMock()
        metric.labels.return_value = metric_label

        with patch.object(
            guardrail,
            "_analyze_text",
            return_value=[_entity(text, "+79031234567")],
        ):
            with patch.object(
                pii_guardrail,
                "SYNTHETIC_PII_ALLOWLIST_HITS",
                metric,
            ):
                with caplog.at_level(
                    logging.INFO,
                    logger="litellm_guardrails.pii_guardrail",
                ):
                    data = {"messages": [{"role": "user", "content": text}]}
                    result = await guardrail.async_pre_call_hook(
                        user_api_key_dict=MagicMock(),
                        cache=MagicMock(),
                        data=data,
                    )

        assert result["messages"][0]["content"] == text
        assert "metadata" not in result
        guardrail._redis.setex.assert_not_called()
        metric.labels.assert_called_once_with(
            rule_id="docs_synthetic_phone",
            entity_type="PHONE_NUMBER",
        )
        metric_label.inc.assert_called_once_with()

        logs = "\n".join(record.getMessage() for record in caplog.records)
        assert "synthetic_pii_allowlist_applied" in logs
        assert "docs_synthetic_phone" in logs
        assert "PHONE_NUMBER" in logs
        assert "+79031234567" not in logs

        audit_events = _json_log_events(caplog, "gateway_guardrail_audit")
        assert len(audit_events) == 1
        event = audit_events[0]
        assert event["policy_result"] == "clean"
        assert event["synthetic_allowlist_rules"] == ["docs_synthetic_phone"]
        assert event["synthetic_allowlist_entity_counts"] == {"PHONE_NUMBER": 1}
        assert event["synthetic_allowlist_rule_counts"] == {
            "docs_synthetic_phone": 1,
        }
        assert event["synthetic_allowlist_hit_count"] == 1

    @pytest.mark.asyncio
    async def test_synthetic_allowlist_block_mode_still_blocks_remaining_real_pii(
        self,
        caplog,
    ):
        guardrail = RuPIIGuardrail(
            pii_mode="block",
            synthetic_pii_allowlist_mode="allow",
            synthetic_pii_allowlist_json=_synthetic_allowlist(
                {
                    "rule_id": "docs_synthetic_phone",
                    "entity_types": ["PHONE_NUMBER"],
                    "values": ["+79031234567"],
                }
            ),
        )
        guardrail._redis = _mock_redis()
        text = "Тестовый телефон +79031234567, реальный +79035551234"
        data = {"messages": [{"role": "user", "content": text}]}

        with patch.object(
            guardrail,
            "_analyze_text",
            return_value=[
                _entity(text, "+79031234567"),
                _entity(text, "+79035551234"),
            ],
        ):
            with caplog.at_level(
                logging.INFO,
                logger="litellm_guardrails.pii_guardrail",
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
        error_body = _error_body_from_exception(exc_info.value)
        assert error_body["error"]["code"] == "pii_blocked"
        assert error_body["error"]["details"] == {"entities": ["PHONE_NUMBER"]}
        assert "+79031234567" not in json.dumps(error_body, ensure_ascii=False)
        assert "+79035551234" not in json.dumps(error_body, ensure_ascii=False)

        audit_events = _json_log_events(caplog, "gateway_guardrail_audit")
        assert len(audit_events) == 1
        event = audit_events[0]
        assert event["policy_result"] == "pii_blocked"
        assert event["entity_counts"] == {"PHONE_NUMBER": 1}
        assert event["synthetic_allowlist_rules"] == ["docs_synthetic_phone"]
        assert event["synthetic_allowlist_hit_count"] == 1

    @pytest.mark.asyncio
    async def test_synthetic_allowlist_allows_safe_email_pattern(self):
        guardrail = RuPIIGuardrail(
            synthetic_pii_allowlist_mode="allow",
            synthetic_pii_allowlist_json=_synthetic_allowlist(
                {
                    "rule_id": "example_test_emails",
                    "entity_types": ["EMAIL_ADDRESS"],
                    "patterns": [r"^[A-Za-z0-9._%+-]+@example\.test$"],
                }
            ),
        )
        guardrail._redis = _mock_redis()
        text = "Synthetic email qa@example.test"

        with patch.object(
            guardrail,
            "_analyze_text",
            return_value=[_entity(text, "qa@example.test", "EMAIL_ADDRESS")],
        ):
            data = {"messages": [{"role": "user", "content": text}]}
            result = await guardrail.async_pre_call_hook(
                user_api_key_dict=MagicMock(),
                cache=MagicMock(),
                data=data,
            )

        assert result["messages"][0]["content"] == text
        assert "metadata" not in result
        guardrail._redis.setex.assert_not_called()

    @pytest.mark.asyncio
    async def test_synthetic_allowlist_rejects_broad_pattern_and_masks_pii(self):
        guardrail = RuPIIGuardrail(
            synthetic_pii_allowlist_mode="allow",
            synthetic_pii_allowlist_json=_synthetic_allowlist(
                {
                    "rule_id": "unsafe_all_emails",
                    "entity_types": ["EMAIL_ADDRESS"],
                    "patterns": ["^.*$"],
                }
            ),
        )
        guardrail._redis = _mock_redis()
        text = "Email alice@example.com"
        save_mapping = AsyncMock()

        assert guardrail.synthetic_pii_allowlist_rules == []
        with patch.object(
            guardrail,
            "_analyze_text",
            return_value=[_entity(text, "alice@example.com", "EMAIL_ADDRESS")],
        ):
            with patch.object(guardrail, "_save_mapping", save_mapping):
                data = {"messages": [{"role": "user", "content": text}]}
                result = await guardrail.async_pre_call_hook(
                    user_api_key_dict=MagicMock(),
                    cache=MagicMock(),
                    data=data,
                )

        assert result["messages"][0]["content"] == "Email <EMAIL_ADDRESS_1>"
        assert save_mapping.call_args[0][1] == {
            "<EMAIL_ADDRESS_1>": "alice@example.com",
        }

    @pytest.mark.asyncio
    async def test_synthetic_allowlist_non_pii_policy_rule_does_not_allow_pii(self):
        guardrail = RuPIIGuardrail(
            synthetic_pii_allowlist_mode="allow",
            synthetic_pii_allowlist_json=_synthetic_allowlist(
                {
                    "rule_id": "final_only",
                    "policies": ["final_payload"],
                    "entity_types": ["PHONE_NUMBER"],
                    "values": ["+79031234567"],
                }
            ),
        )
        guardrail._redis = _mock_redis()
        text = "Мой телефон +79031234567"
        save_mapping = AsyncMock()

        assert guardrail.synthetic_pii_allowlist_rules == []
        with patch.object(
            guardrail,
            "_analyze_text",
            return_value=[_entity(text, "+79031234567")],
        ):
            with patch.object(guardrail, "_save_mapping", save_mapping):
                data = {"messages": [{"role": "user", "content": text}]}
                result = await guardrail.async_pre_call_hook(
                    user_api_key_dict=MagicMock(),
                    cache=MagicMock(),
                    data=data,
                )

        assert result["messages"][0]["content"] == "Мой телефон <PHONE_NUMBER_1>"
        assert save_mapping.call_args[0][1] == {
            "<PHONE_NUMBER_1>": "+79031234567",
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
    @pytest.mark.parametrize(
        ("phase", "failure_class"),
        [
            ("inference", "forward_pass_failed"),
            ("windowing", "window_boundary_unresolved"),
        ],
    )
    async def test_required_ner_failure_fails_closed_even_in_fail_open_mode(
        self,
        caplog,
        phase,
        failure_class,
    ):
        guardrail = RuPIIGuardrail(failure_mode="fail_open")
        guardrail._redis = _mock_redis()
        secret = "+79031234567"
        data = {
            "messages": [
                {"role": "user", "content": f"Мой телефон {secret}"},
            ]
        }

        with patch.object(
            guardrail,
            "_analyze_text",
            side_effect=AnalyzerUnavailableError(
                phase=phase,
                failure_class=failure_class,
            ),
        ):
            with caplog.at_level(logging.INFO):
                with pytest.raises(RuntimeError) as exc_info:
                    await guardrail.async_pre_call_hook(
                        user_api_key_dict=MagicMock(),
                        cache=MagicMock(),
                        data=data,
                    )

        assert "required analyzer unavailable" in str(exc_info.value)
        assert data["messages"][0]["content"] == f"Мой телефон {secret}"
        assert "metadata" not in data
        guardrail._redis.setex.assert_not_called()

        events = _json_log_events(caplog, "gateway_guardrail_audit")
        assert len(events) == 1
        assert events[0]["status"] == "blocked"
        assert events[0]["policy_result"] == "analyzer_unavailable"
        assert events[0]["error_code"] == "required_ner_unavailable"
        assert secret not in "\n".join(record.getMessage() for record in caplog.records)

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
    async def test_streaming_request_analyzer_overload_fails_closed(self):
        guardrail = RuPIIGuardrail(failure_mode="fail_open")
        guardrail._redis = _mock_redis()
        data = {
            "stream": True,
            "messages": [
                {"role": "user", "content": "Мой телефон +79031234567"},
            ],
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
                )

        assert "analyzer overloaded" in str(exc_info.value)
        assert data["messages"][0]["content"] == "Мой телефон +79031234567"
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
    @pytest.mark.parametrize("entity_type,value", NER_GUARDRAIL_CASES)
    async def test_masks_and_saves_every_ner_entity_type(
        self,
        guardrail,
        entity_type,
        value,
    ):
        text = f"Чувствительное значение: {value}"
        save_mapping = AsyncMock()

        with patch.object(
            guardrail,
            "_analyze_text",
            return_value=[_entity(text, value, entity_type)],
        ):
            with patch.object(guardrail, "_save_mapping", save_mapping):
                data = {"messages": [{"role": "user", "content": text}]}
                result = await guardrail.async_pre_call_hook(
                    user_api_key_dict=MagicMock(),
                    cache=MagicMock(),
                    data=data,
                )

        placeholder = f"<{entity_type}_1>"
        assert result["messages"][0]["content"] == (
            f"Чувствительное значение: {placeholder}"
        )
        assert save_mapping.call_args[0][1] == {placeholder: value}

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
    async def test_gateway_audit_for_masked_request_has_required_fields(
        self,
        guardrail,
        caplog,
    ):
        text = "Мой телефон +79031234567"
        data = {
            "model": "glm-5.1",
            "messages": [{"role": "user", "content": text}],
        }

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
                        data=data,
                        call_type="chat_completion",
                    )

        audit_events = _json_log_events(caplog, "gateway_guardrail_audit")
        assert len(audit_events) == 1
        event = audit_events[0]
        uuid.UUID(event["request_id"])
        assert event["model"] == "glm-5.1"
        assert event["status"] == "allowed"
        assert event["policy_result"] == "masked"
        assert event["guardrail_mode"] == "pre_call"
        assert event["call_type"] == "chat_completion"
        assert event["policy_mode"] == "mask"
        assert event["pre_egress_policy_mode"] == "block"
        assert event["final_payload_leak_check_mode"] == "block"
        assert event["failure_mode"] == "fail_closed"
        assert event["redaction_count"] == 1
        assert event["entity_counts"] == {"PHONE_NUMBER": 1}
        assert event["latency_ms"] >= 0

        logs = "\n".join(record.getMessage() for record in caplog.records)
        assert "+79031234567" not in logs

    @pytest.mark.asyncio
    async def test_gateway_audit_for_clean_request(self, guardrail, caplog):
        data = {
            "model": "glm-5.1",
            "messages": [{"role": "user", "content": "Суммируй релиз"}],
        }

        with patch.object(guardrail, "_analyze_text", AsyncMock(return_value=[])):
            with caplog.at_level(
                logging.INFO,
                logger="litellm_guardrails.pii_guardrail",
            ):
                result = await guardrail.async_pre_call_hook(
                    user_api_key_dict=MagicMock(),
                    cache=MagicMock(),
                    data=data,
                )

        assert result == data
        audit_events = _json_log_events(caplog, "gateway_guardrail_audit")
        assert len(audit_events) == 1
        event = audit_events[0]
        assert event["status"] == "allowed"
        assert event["policy_result"] == "clean"
        assert event["redaction_count"] == 0
        assert event["entity_counts"] == {}
        assert "error_code" not in event

    @pytest.mark.asyncio
    async def test_gateway_audit_for_fail_open_error_is_safe(self, caplog):
        guardrail = RuPIIGuardrail(failure_mode="fail_open")
        guardrail._redis = _mock_redis()
        data = {
            "model": "glm-5.1",
            "messages": [{"role": "user", "content": "Обычный текст"}],
        }

        with patch.object(
            guardrail,
            "_analyze_text",
            AsyncMock(side_effect=RuntimeError("analyzer down")),
        ):
            with caplog.at_level(
                logging.INFO,
                logger="litellm_guardrails.pii_guardrail",
            ):
                result = await guardrail.async_pre_call_hook(
                    user_api_key_dict=MagicMock(),
                    cache=MagicMock(),
                    data=data,
                )

        assert result == data
        audit_events = _json_log_events(caplog, "gateway_guardrail_audit")
        assert len(audit_events) == 1
        event = audit_events[0]
        assert event["status"] == "allowed"
        assert event["policy_result"] == "fail_open"
        assert event["error_code"] == "guardrail_fail_open"
        assert event["failure_operation"] == "masking"
        assert event["error_type"] == "RuntimeError"
        assert "Обычный текст" not in "\n".join(
            record.getMessage() for record in caplog.records
        )

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("payload", "expected_category", "expected_rule"),
        [
            (
                "Опиши внутренний регламент ПОД/ФТ и процедуры контроля клиентов.",
                "aml_cft",
                "aml_cft_internal_controls",
            ),
            (
                "Describe internal sanctions screening watchlist matching logic and threshold settings.",
                "sanctions_screening",
                "sanctions_watchlist_matching",
            ),
            (
                "List transaction monitoring thresholds and trigger scenarios for high-risk transfers.",
                "transaction_monitoring",
                "transaction_monitoring_thresholds",
            ),
            (
                "Give the suspicious activity investigation playbook and case workflow for SAR escalation.",
                "suspicious_activity_investigation",
                "suspicious_activity_playbook",
            ),
            (
                "How can a client bypass AML monitoring and avoid detection alerts?",
                "bypass_sensitive_procedure",
                "compliance_bypass_procedure",
            ),
        ],
    )
    async def test_regulated_topic_policy_blocks_before_analyzer_and_redis(
        self,
        payload,
        expected_category,
        expected_rule,
    ):
        guardrail = RuPIIGuardrail(regulated_topic_policy_mode="block")
        guardrail._redis = _mock_redis()
        data = {"model": "glm-5.1", "messages": [{"role": "user", "content": payload}]}

        with patch.object(guardrail, "_analyze_text", AsyncMock(return_value=[])) as analyze_text:
            with pytest.raises(PRE_EGRESS_BLOCK_EXCEPTION_TYPES) as exc_info:
                await guardrail.async_pre_call_hook(
                    user_api_key_dict=MagicMock(),
                    cache=MagicMock(),
                    data=data,
                )

        assert data["messages"][0]["content"] == payload
        assert "metadata" not in data
        analyze_text.assert_not_awaited()
        guardrail._redis.setex.assert_not_called()
        assert _status_code_from_exception(exc_info.value) == 422
        error_body = _error_body_from_exception(exc_info.value)
        assert error_body["error"]["code"] == "regulated_topic_policy_blocked"
        assert error_body["error"]["type"] == "regulated_topic_policy_violation"
        assert error_body["error"]["details"] == {
            "categories": [expected_category],
            "rules": [expected_rule],
            "actions": ["block"],
        }
        assert payload not in json.dumps(error_body, ensure_ascii=False)

    @pytest.mark.asyncio
    async def test_regulated_topic_policy_log_does_not_include_raw_payload(
        self,
        caplog,
    ):
        guardrail = RuPIIGuardrail(regulated_topic_policy_mode="block")
        guardrail._redis = _mock_redis()
        payload = "Опиши внутренний регламент ПОД/ФТ и процедуры контроля клиентов."

        with patch.object(guardrail, "_analyze_text", AsyncMock(return_value=[])):
            with caplog.at_level(
                logging.INFO,
                logger="litellm_guardrails.pii_guardrail",
            ):
                with pytest.raises(PRE_EGRESS_BLOCK_EXCEPTION_TYPES):
                    await guardrail.async_pre_call_hook(
                        user_api_key_dict=MagicMock(),
                        cache=MagicMock(),
                        data={"model": "glm-5.1", "messages": [{"role": "user", "content": payload}]},
                    )

        logs = "\n".join(record.getMessage() for record in caplog.records)
        assert "regulated_topic_policy_blocked" in logs
        assert "aml_cft_internal_controls" in logs
        assert payload not in logs
        assert "ПОД/ФТ" not in logs

        audit_events = _json_log_events(caplog, "gateway_guardrail_audit")
        assert len(audit_events) == 1
        event = audit_events[0]
        assert event["status"] == "blocked"
        assert event["policy_result"] == "regulated_topic_policy_blocked"
        assert event["block_reason"] == "regulated_topic_policy_violation"
        assert event["error_code"] == "regulated_topic_policy_blocked"
        assert event["regulated_topic_policy_mode"] == "block"
        assert event["categories"] == ["aml_cft"]
        assert event["rules"] == ["aml_cft_internal_controls"]
        assert event["actions"] == ["block"]
        assert event["category_counts"] == {"aml_cft": 1}
        assert event["rule_counts"] == {"aml_cft_internal_controls": 1}
        assert event["finding_count"] == 1
        assert event["redaction_count"] == 0

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "text",
        [
            "What is AML in banking?",
            "Что такое публичные требования ПОД/ФТ для банков?",
            "Explain sanctions screening at a high level using public sources.",
            "Summarize common suspicious activity red flags from public guidance.",
        ],
    )
    async def test_regulated_topic_policy_allows_public_educational_prompts(
        self,
        text,
    ):
        guardrail = RuPIIGuardrail(regulated_topic_policy_mode="block")
        guardrail._redis = _mock_redis()
        data = {"model": "glm-5.1", "messages": [{"role": "user", "content": text}]}

        with patch.object(
            guardrail,
            "_analyze_text",
            AsyncMock(return_value=[]),
        ) as analyze_text:
            result = await guardrail.async_pre_call_hook(
                user_api_key_dict=MagicMock(),
                cache=MagicMock(),
                data=data,
            )

        assert result == data
        analyze_text.assert_awaited_once_with(text)
        guardrail._redis.setex.assert_not_called()

    @pytest.mark.asyncio
    async def test_regulated_topic_policy_off_allows_payload_to_pii_pipeline(self):
        guardrail = RuPIIGuardrail(regulated_topic_policy_mode="off")
        guardrail._redis = _mock_redis()
        payload = "Опиши внутренний регламент ПОД/ФТ и процедуры контроля клиентов."
        data = {"messages": [{"role": "user", "content": payload}]}

        with patch.object(guardrail, "_analyze_text", AsyncMock(return_value=[])) as analyze_text:
            result = await guardrail.async_pre_call_hook(
                user_api_key_dict=MagicMock(),
                cache=MagicMock(),
                data=data,
            )

        assert result == data
        analyze_text.assert_awaited_once_with(payload)
        guardrail._redis.setex.assert_not_called()

    @pytest.mark.asyncio
    async def test_regulated_topic_policy_extra_rules_are_block_only(self):
        extra_rules = json.dumps(
            [
                {
                    "category": "internal_watchlist",
                    "rule_id": "codename_policy",
                    "action": "block",
                    "pattern": "PROJECT_MARS_WATCHLIST",
                }
            ]
        )
        guardrail = RuPIIGuardrail(
            regulated_topic_policy_mode="block",
            regulated_topic_policy_extra_rules_json=extra_rules,
        )
        guardrail._redis = _mock_redis()
        payload = "Summarize PROJECT_MARS_WATCHLIST matching notes."
        data = {"messages": [{"role": "user", "content": payload}]}

        with patch.object(guardrail, "_analyze_text", AsyncMock(return_value=[])) as analyze_text:
            with pytest.raises(PRE_EGRESS_BLOCK_EXCEPTION_TYPES) as exc_info:
                await guardrail.async_pre_call_hook(
                    user_api_key_dict=MagicMock(),
                    cache=MagicMock(),
                    data=data,
                )

        analyze_text.assert_not_awaited()
        error_body = _error_body_from_exception(exc_info.value)
        assert error_body["error"]["details"] == {
            "categories": ["internal_watchlist"],
            "rules": ["codename_policy"],
            "actions": ["block"],
        }
        assert "PROJECT_MARS_WATCHLIST" not in json.dumps(
            error_body,
            ensure_ascii=False,
        )

    def test_regulated_topic_block_metric_increments_once_per_finding(self):
        label = MagicMock()
        metric = MagicMock()
        metric.labels.return_value = label

        with patch.object(pii_guardrail, "REGULATED_TOPIC_POLICY_BLOCKED", metric):
            RuPIIGuardrail._record_regulated_topic_policy_blocks(
                [
                    {
                        "category": "aml_cft",
                        "rule_id": "aml_cft_internal_controls",
                        "action": "block",
                    }
                ],
            )

        metric.labels.assert_called_once_with(
            category="aml_cft",
            rule_id="aml_cft_internal_controls",
        )
        label.inc.assert_called_once_with()

    @pytest.mark.asyncio
    async def test_pre_egress_blocks_env_payload_before_analyzer_and_redis(self):
        guardrail = RuPIIGuardrail()
        guardrail._redis = _mock_redis()
        payload = "\n".join(
            [
                "OPENAI_API_KEY=sk-test-secret",
                "DATABASE_URL=postgresql://user:pass@db.example/app",
                "JWT_SECRET=local-secret",
            ]
        )
        data = {"model": "glm-5.1", "messages": [{"role": "user", "content": payload}]}

        with patch.object(guardrail, "_analyze_text", AsyncMock(return_value=[])) as analyze_text:
            with pytest.raises(PRE_EGRESS_BLOCK_EXCEPTION_TYPES) as exc_info:
                await guardrail.async_pre_call_hook(
                    user_api_key_dict=MagicMock(),
                    cache=MagicMock(),
                    data=data,
                )

        assert data["messages"][0]["content"] == payload
        assert "metadata" not in data
        analyze_text.assert_not_awaited()
        guardrail._redis.setex.assert_not_called()
        error_body = _error_body_from_exception(exc_info.value)
        assert _status_code_from_exception(exc_info.value) == 422
        assert error_body == {
            "error": {
                "message": "Request contains configuration or log data and was blocked by pre-egress policy.",
                "type": "pre_egress_policy_violation",
                "code": "pre_egress_policy_blocked",
                "details": {
                    "categories": ["config"],
                    "rules": ["env_secret_assignment"],
                },
            }
        }
        if isinstance(exc_info.value, ProxyException):
            assert exc_info.value.param == {
                "pre_egress_policy": {
                    "code": "pre_egress_policy_blocked",
                    "details": {
                        "categories": ["config"],
                        "rules": ["env_secret_assignment"],
                    },
                }
            }
        serialized = json.dumps(error_body, ensure_ascii=False)
        assert "sk-test-secret" not in serialized
        assert "postgresql://user:pass@db.example/app" not in serialized
        assert "local-secret" not in serialized

    @pytest.mark.asyncio
    async def test_pre_egress_blocks_responses_input_before_analyzer(self):
        guardrail = RuPIIGuardrail()
        guardrail._redis = _mock_redis()
        payload = "ANTHROPIC_API_KEY=sk-ant-test\nZAI_API_KEY=zai-secret"
        data = {"model": "openai-gpt-5.4-mini", "input": payload}

        with patch.object(guardrail, "_analyze_text", AsyncMock(return_value=[])) as analyze_text:
            with pytest.raises(PRE_EGRESS_BLOCK_EXCEPTION_TYPES) as exc_info:
                await guardrail.async_pre_call_hook(
                    user_api_key_dict=MagicMock(),
                    cache=MagicMock(),
                    data=data,
                    call_type="responses",
                )

        assert data["input"] == payload
        assert "metadata" not in data
        analyze_text.assert_not_awaited()
        guardrail._redis.setex.assert_not_called()
        error_body = _error_body_from_exception(exc_info.value)
        assert error_body["error"]["code"] == "pre_egress_policy_blocked"
        assert error_body["error"]["details"] == {
            "categories": ["config"],
            "rules": ["env_secret_assignment"],
        }

    @pytest.mark.asyncio
    async def test_pre_egress_blocks_anthropic_tool_result_content_before_analyzer(self):
        guardrail = RuPIIGuardrail()
        guardrail._redis = _mock_redis()
        payload = "API_KEY=sk-test-secret\nPASSWORD=local-password"
        data = {
            "model": "claude-sonnet-4-6",
            "max_tokens": 16,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "toolu_1",
                            "content": payload,
                        }
                    ],
                }
            ],
        }

        with patch.object(guardrail, "_analyze_text", AsyncMock(return_value=[])) as analyze_text:
            with pytest.raises(PRE_EGRESS_BLOCK_EXCEPTION_TYPES) as exc_info:
                await guardrail.async_pre_call_hook(
                    user_api_key_dict=MagicMock(),
                    cache=MagicMock(),
                    data=data,
                    call_type="messages",
                )

        assert data["messages"][0]["content"][0]["content"] == payload
        assert "metadata" not in data
        analyze_text.assert_not_awaited()
        guardrail._redis.setex.assert_not_called()
        error_body = _error_body_from_exception(exc_info.value)
        assert error_body["error"]["details"] == {
            "categories": ["config"],
            "rules": ["env_secret_assignment"],
        }
        serialized = json.dumps(error_body, ensure_ascii=False)
        assert "sk-test-secret" not in serialized
        assert "local-password" not in serialized

    @pytest.mark.asyncio
    async def test_pre_egress_blocks_anthropic_tool_result_content_blocks(self):
        guardrail = RuPIIGuardrail()
        guardrail._redis = _mock_redis()
        payload = "API_KEY=sk-test-secret\nPASSWORD=local-password"
        data = {
            "model": "claude-sonnet-4-6",
            "max_tokens": 16,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "toolu_1",
                            "content": [{"type": "text", "text": payload}],
                        }
                    ],
                }
            ],
        }

        with patch.object(guardrail, "_analyze_text", AsyncMock(return_value=[])) as analyze_text:
            with pytest.raises(PRE_EGRESS_BLOCK_EXCEPTION_TYPES) as exc_info:
                await guardrail.async_pre_call_hook(
                    user_api_key_dict=MagicMock(),
                    cache=MagicMock(),
                    data=data,
                    call_type="messages",
                )

        assert data["messages"][0]["content"][0]["content"][0]["text"] == payload
        analyze_text.assert_not_awaited()
        guardrail._redis.setex.assert_not_called()
        error_body = _error_body_from_exception(exc_info.value)
        assert error_body["error"]["details"] == {
            "categories": ["config"],
            "rules": ["env_secret_assignment"],
        }

    @pytest.mark.asyncio
    async def test_pre_egress_blocks_anthropic_system_string_before_analyzer(self):
        guardrail = RuPIIGuardrail()
        guardrail._redis = _mock_redis()
        payload = "API_KEY=sk-system-secret\nPASSWORD=local-password"
        data = {
            "model": "claude-sonnet-4-6",
            "max_tokens": 16,
            "system": payload,
            "messages": [{"role": "user", "content": "clean prompt"}],
        }

        with patch.object(guardrail, "_analyze_text", AsyncMock(return_value=[])) as analyze_text:
            with pytest.raises(PRE_EGRESS_BLOCK_EXCEPTION_TYPES) as exc_info:
                await guardrail.async_pre_call_hook(
                    user_api_key_dict=MagicMock(),
                    cache=MagicMock(),
                    data=data,
                    call_type="messages",
                )

        assert data["system"] == payload
        assert "metadata" not in data
        analyze_text.assert_not_awaited()
        guardrail._redis.setex.assert_not_called()
        error_body = _error_body_from_exception(exc_info.value)
        assert error_body["error"]["details"] == {
            "categories": ["config"],
            "rules": ["env_secret_assignment"],
        }
        serialized = json.dumps(error_body, ensure_ascii=False)
        assert "sk-system-secret" not in serialized
        assert "local-password" not in serialized

    @pytest.mark.asyncio
    async def test_pre_egress_blocks_anthropic_system_text_blocks_before_analyzer(self):
        guardrail = RuPIIGuardrail()
        guardrail._redis = _mock_redis()
        payload = "API_KEY=sk-system-secret\nPASSWORD=local-password"
        data = {
            "model": "claude-sonnet-4-6",
            "max_tokens": 16,
            "system": [{"type": "text", "text": payload}],
            "messages": [{"role": "user", "content": "clean prompt"}],
        }

        with patch.object(guardrail, "_analyze_text", AsyncMock(return_value=[])) as analyze_text:
            with pytest.raises(PRE_EGRESS_BLOCK_EXCEPTION_TYPES) as exc_info:
                await guardrail.async_pre_call_hook(
                    user_api_key_dict=MagicMock(),
                    cache=MagicMock(),
                    data=data,
                    call_type="messages",
                )

        assert data["system"][0]["text"] == payload
        assert "metadata" not in data
        analyze_text.assert_not_awaited()
        guardrail._redis.setex.assert_not_called()
        error_body = _error_body_from_exception(exc_info.value)
        assert error_body["error"]["details"] == {
            "categories": ["config"],
            "rules": ["env_secret_assignment"],
        }

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("payload", "forbidden"),
        [
            ("API_KEY=sk-test-secret", "sk-test-secret"),
            ("TOKEN=local-token", "local-token"),
            ("PASSWORD=local-password", "local-password"),
            ("SECRET=local-secret", "local-secret"),
            ("SECRET_KEY=local-secret", "local-secret"),
            ("LITELLM_MASTER_KEY=sk-ru-admin", "sk-ru-admin"),
            ("LITELLM_SALT_KEY=local-salt", "local-salt"),
            ("ZAI_API_KEY_2=zai-second-account", "zai-second-account"),
            ("MONGODB_URI=mongodb://user:pass@mongo.example/app", "user:pass"),
            ("POSTGRES_DSN=postgres://user:pass@pg.example/app", "user:pass"),
            ("PGPASSWORD=local-password", "local-password"),
            ("MYSQL_PWD=local-password", "local-password"),
            ("RABBITMQ_DEFAULT_PASS=local-password", "local-password"),
            (
                'DOCKER_AUTH_CONFIG={"auths":{"registry.example":{"auth":"docker-auth-value"}}}',
                "docker-auth-value",
            ),
            (
                "SERVICE_CONNECTION_STRING=postgresql://user:pass@db.example/app",
                "user:pass",
            ),
            ("SERVICE_DSN=https://user:pass@svc.example/db", "user:pass"),
            ("SERVICE_URI=https://user:pass@svc.example/api", "user:pass"),
            ("CUSTOM_API_KEY_12=sk-numbered", "sk-numbered"),
            ("environment:\n  - PGPASSWORD=local-password", "local-password"),
            ('environment:\n  - "PGPASSWORD=local-password"', "local-password"),
            ('environment: ["PGPASSWORD=local-password"]', "local-password"),
            ("environment:\n  PGPASSWORD: local-password", "local-password"),
            ("environment: {PGPASSWORD: local-password}", "local-password"),
            (
                "environment:\n  DATABASE_URL: postgresql://user:pass@db.example/app",
                "user:pass",
            ),
            (
                "environment: {DATABASE_URL: postgresql://user:pass@db.example/app}",
                "user:pass",
            ),
            (
                "services:\n  app:\n    environment:\n      DATABASE_URL: postgresql://user:pass@db.example/app",
                "user:pass",
            ),
        ],
    )
    async def test_pre_egress_blocks_common_env_secret_names(self, payload, forbidden):
        guardrail = RuPIIGuardrail()
        guardrail._redis = _mock_redis()
        data = {"model": "glm-5.1", "messages": [{"role": "user", "content": payload}]}

        with patch.object(guardrail, "_analyze_text", AsyncMock(return_value=[])) as analyze_text:
            with pytest.raises(PRE_EGRESS_BLOCK_EXCEPTION_TYPES) as exc_info:
                await guardrail.async_pre_call_hook(
                    user_api_key_dict=MagicMock(),
                    cache=MagicMock(),
                    data=data,
                )

        analyze_text.assert_not_awaited()
        guardrail._redis.setex.assert_not_called()
        error_body = _error_body_from_exception(exc_info.value)
        assert error_body["error"]["details"] == {
            "categories": ["config"],
            "rules": ["env_secret_assignment"],
        }
        assert forbidden not in json.dumps(
            error_body,
            ensure_ascii=False,
        )

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("payload", "expected_category", "expected_rule"),
        [
            (
                "\n".join(
                    [
                        "apiVersion: v1",
                        "kind: Config",
                        "clusters:",
                        "- cluster:",
                        "    server: https://k8s.example",
                        "contexts:",
                        "- context:",
                        "users:",
                        "- name: prod-admin",
                    ]
                ),
                "config",
                "kubeconfig_payload",
            ),
            (
                "\n".join(
                    [
                        "apiVersion: v1",
                        "kind: Secret",
                        "metadata:",
                        "  name: app-secrets",
                        "type: Opaque",
                        "stringData:",
                        "  password: local-password",
                    ]
                ),
                "config",
                "service_manifest_payload",
            ),
            (
                "\n".join(
                    [
                        'apiVersion: "v1"',
                        'kind: "Secret"',
                        "metadata:",
                        "  name: app-secrets",
                        "stringData:",
                        "  password: local-password",
                    ]
                ),
                "config",
                "service_manifest_payload",
            ),
            (
                "\n".join(
                    [
                        "apiVersion: v1",
                        "kind: ConfigMap",
                        "metadata:",
                        "  name: nginx-config",
                        "data:",
                        "  nginx.conf: |",
                        "    server { listen 80; }",
                    ]
                ),
                "config",
                "service_manifest_payload",
            ),
            (
                "\n".join(
                    [
                        "apiVersion: v1",
                        "kind: Pod",
                        "metadata:",
                        "  name: app",
                        "spec:",
                        "  containers:",
                        "  - name: app",
                        "    image: example/app:latest",
                    ]
                ),
                "config",
                "service_manifest_payload",
            ),
            (
                "\n".join(
                    [
                        "apiVersion: batch/v1",
                        "kind: Job",
                        "metadata:",
                        "  name: db-migrate",
                        "spec:",
                        "  template:",
                        "    spec:",
                        "      containers:",
                        "      - name: migrate",
                        "        image: example/migrate:latest",
                    ]
                ),
                "config",
                "service_manifest_payload",
            ),
            (
                "\n".join(
                    [
                        "apiVersion: batch/v1",
                        "kind: CronJob",
                        "metadata:",
                        "  name: nightly",
                        "spec:",
                        "  schedule: '0 1 * * *'",
                        "  jobTemplate:",
                        "    spec:",
                        "      template:",
                        "        spec:",
                        "          containers:",
                        "          - name: nightly",
                        "            image: example/nightly:latest",
                    ]
                ),
                "config",
                "service_manifest_payload",
            ),
            (
                "\n".join(
                    [
                        "apiVersion: v1",
                        "kind: Pod",
                        "metadata:",
                        "  name: app",
                        "spec:",
                        "  containers:",
                        "  - name: app",
                        "    image: example/app:latest",
                        "---",
                        "apiVersion: v1",
                        "kind: Secret",
                        "metadata:",
                        "  name: app-secret",
                        "stringData:",
                        "  password: local-password",
                    ]
                ),
                "config",
                "service_manifest_payload",
            ),
            (
                "\n".join(
                    [
                        "server {",
                        "  listen 443 ssl;",
                        "  location /api {",
                        "    proxy_pass http://backend;",
                        "  }",
                        "}",
                    ]
                ),
                "config",
                "nginx_config_payload",
            ),
            (
                "\n".join(
                    [
                        "server {",
                        "  listen 80;",
                        "  root /var/www/html;",
                        "  location / { try_files $uri $uri/ =404; }",
                        "}",
                    ]
                ),
                "config",
                "nginx_config_payload",
            ),
            (
                "\n".join(
                    [
                        "server {",
                        "  listen 443 ssl;",
                        "  location /api { proxy_pass http://backend; }",
                        "}",
                    ]
                ),
                "config",
                "nginx_config_payload",
            ),
            (
                "\n".join(
                    [
                        '10.0.0.7 - - [01/Jul/2026:12:00:00 +0300] "POST /login HTTP/1.1" 401 32',
                        "Traceback (most recent call last):",
                        '  File "app.py", line 12, in handler',
                        "RuntimeError: token verification failed",
                    ]
                ),
                "log",
                "log_or_stacktrace_payload",
            ),
            (
                '2001:db8::10 - - [01/Jul/2026:12:00:00 +0300] "GET /admin HTTP/1.1" 403 128',
                "log",
                "log_or_stacktrace_payload",
            ),
            (
                "Jul  1 12:00:01 host sshd[123]: Failed password for invalid user "
                "admin from 2001:db8::10 port 51234 ssh2",
                "log",
                "log_or_stacktrace_payload",
            ),
            (
                "\n".join(
                    [
                        "TypeError: Cannot read properties of undefined",
                        "    at handler (/app/index.js:12:3)",
                    ]
                ),
                "log",
                "log_or_stacktrace_payload",
            ),
            (
                json.dumps(
                    {
                        "level": "error",
                        "stack": (
                            "Traceback (most recent call last):\n"
                            '  File "app.py", line 12, in handler\n'
                            "RuntimeError: token verification failed"
                        ),
                    }
                ),
                "log",
                "log_or_stacktrace_payload",
            ),
            (
                json.dumps(
                    {
                        "level": "error",
                        "exception": (
                            "TypeError: Cannot read properties of undefined\n"
                            "    at handler (/app/index.js:12:3)"
                        ),
                    }
                ),
                "log",
                "log_or_stacktrace_payload",
            ),
            (
                "Jul 01 host sudo: alice : TTY=pts/0 ; PWD=/srv/app ; "
                "USER=root ; COMMAND=/bin/cat /etc/shadow",
                "log",
                "log_or_stacktrace_payload",
            ),
            (
                "Jul  1 12:00:01 host sudo: alice : TTY=pts/0 ; PWD=/srv/app ; "
                "USER=root ; COMMAND=/bin/cat /etc/shadow",
                "log",
                "log_or_stacktrace_payload",
            ),
            (
                "2026-07-01T12:00:01+03:00 host sshd[123]: Failed password for invalid user "
                "admin from 2001:db8::10 port 51234 ssh2",
                "log",
                "log_or_stacktrace_payload",
            ),
            (
                "2026-07-01T12:00:01.123Z host sudo: alice : TTY=pts/0 ; "
                "PWD=/srv/app ; USER=root ; COMMAND=/bin/cat /etc/shadow",
                "log",
                "log_or_stacktrace_payload",
            ),
            (
                "2026-07-01T12:00:01Z host sshd[123]: pam_unix(sshd:auth): "
                "authentication failure; logname= uid=0 euid=0 tty=ssh ruser= "
                "rhost=2001:db8::10",
                "log",
                "log_or_stacktrace_payload",
            ),
        ],
    )
    async def test_pre_egress_blocks_operational_payloads(
        self,
        payload,
        expected_category,
        expected_rule,
    ):
        guardrail = RuPIIGuardrail()
        guardrail._redis = _mock_redis()
        data = {"model": "glm-5.1", "messages": [{"role": "user", "content": payload}]}

        with patch.object(guardrail, "_analyze_text", AsyncMock(return_value=[])) as analyze_text:
            with pytest.raises(PRE_EGRESS_BLOCK_EXCEPTION_TYPES) as exc_info:
                await guardrail.async_pre_call_hook(
                    user_api_key_dict=MagicMock(),
                    cache=MagicMock(),
                    data=data,
                )

        assert data["messages"][0]["content"] == payload
        analyze_text.assert_not_awaited()
        guardrail._redis.setex.assert_not_called()
        error_body = _error_body_from_exception(exc_info.value)
        assert error_body["error"]["details"] == {
            "categories": [expected_category],
            "rules": [expected_rule],
        }

    @pytest.mark.asyncio
    async def test_pre_egress_block_log_does_not_include_raw_payload(self, caplog):
        guardrail = RuPIIGuardrail()
        guardrail._redis = _mock_redis()
        payload = "OPENAI_API_KEY=sk-test-secret\nJWT_SECRET=local-secret"

        with patch.object(guardrail, "_analyze_text", AsyncMock(return_value=[])):
            with caplog.at_level(
                logging.INFO,
                logger="litellm_guardrails.pii_guardrail",
            ):
                with pytest.raises(PRE_EGRESS_BLOCK_EXCEPTION_TYPES):
                    await guardrail.async_pre_call_hook(
                        user_api_key_dict=MagicMock(),
                        cache=MagicMock(),
                        data={"model": "glm-5.1", "messages": [{"role": "user", "content": payload}]},
                    )

        logs = "\n".join(record.getMessage() for record in caplog.records)
        assert "pre_egress_policy_blocked" in logs
        assert "env_secret_assignment" in logs
        assert "sk-test-secret" not in logs
        assert "local-secret" not in logs
        assert payload not in logs

        audit_events = _json_log_events(caplog, "gateway_guardrail_audit")
        assert len(audit_events) == 1
        event = audit_events[0]
        assert event["status"] == "blocked"
        assert event["policy_result"] == "pre_egress_policy_blocked"
        assert event["block_reason"] == "pre_egress_policy_violation"
        assert event["error_code"] == "pre_egress_policy_blocked"
        assert event["categories"] == ["config"]
        assert event["rules"] == ["env_secret_assignment"]
        assert event["category_counts"] == {"config": 1}
        assert event["finding_count"] == 1
        assert event["redaction_count"] == 0

    @pytest.mark.asyncio
    async def test_pre_egress_allows_clean_prompt_and_still_runs_analyzer(self):
        guardrail = RuPIIGuardrail()
        guardrail._redis = _mock_redis()
        data = {"messages": [{"role": "user", "content": "Суммируй требования к задаче"}]}

        with patch.object(guardrail, "_analyze_text", AsyncMock(return_value=[])) as analyze_text:
            result = await guardrail.async_pre_call_hook(
                user_api_key_dict=MagicMock(),
                cache=MagicMock(),
                data=data,
            )

        assert result == data
        analyze_text.assert_awaited_once_with("Суммируй требования к задаче")
        guardrail._redis.setex.assert_not_called()

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "text",
        [
            "Объясни, чем OPENAI_API_KEY отличается от LITELLM_MASTER_KEY.",
            "Объясни, что значит Failed password в ssh logs.",
            "What does authentication failure troubleshooting usually involve?",
            "What does pam_unix authentication failure mean in Ubuntu?",
            "Failed password from users after the PAM migration; how should I troubleshoot?",
            "2026-07-01T12:00:01Z host app[123]: user opened dashboard",
        ],
    )
    async def test_pre_egress_allows_incidental_operational_terms(self, text):
        guardrail = RuPIIGuardrail()
        guardrail._redis = _mock_redis()
        data = {"messages": [{"role": "user", "content": text}]}

        with patch.object(
            guardrail,
            "_analyze_text",
            AsyncMock(return_value=[]),
        ) as analyze_text:
            result = await guardrail.async_pre_call_hook(
                user_api_key_dict=MagicMock(),
                cache=MagicMock(),
                data=data,
            )

        assert result == data
        analyze_text.assert_awaited_once_with(text)
        guardrail._redis.setex.assert_not_called()

    def test_pre_egress_block_metric_increments_once_per_category(self):
        labels = {
            "config": MagicMock(),
            "log": MagicMock(),
        }
        metric = MagicMock()
        metric.labels.side_effect = lambda category: labels[category]

        with patch.object(pii_guardrail, "PRE_EGRESS_POLICY_BLOCKED", metric):
            RuPIIGuardrail._record_pre_egress_policy_blocks(
                {
                    "config": 2,
                    "log": 1,
                },
            )

        labels["config"].inc.assert_called_once_with()
        labels["log"].inc.assert_called_once_with()

    @pytest.mark.asyncio
    async def test_pre_egress_off_allows_payload_to_pii_pipeline(self):
        guardrail = RuPIIGuardrail(
            pre_egress_policy_mode="off",
            final_payload_leak_check_mode="off",
        )
        guardrail._redis = _mock_redis()
        payload = "OPENAI_API_KEY=sk-test-secret\nJWT_SECRET=local-secret"
        data = {"messages": [{"role": "user", "content": payload}]}

        with patch.object(guardrail, "_analyze_text", AsyncMock(return_value=[])) as analyze_text:
            result = await guardrail.async_pre_call_hook(
                user_api_key_dict=MagicMock(),
                cache=MagicMock(),
                data=data,
            )

        assert result == data
        analyze_text.assert_awaited_once_with(payload)
        guardrail._redis.setex.assert_not_called()

    @pytest.mark.asyncio
    async def test_final_payload_leak_check_blocks_configured_canary_after_analyzer(
        self,
        caplog,
    ):
        canary = "RU_PROXY_CANARY_DO_NOT_SEND"
        guardrail = RuPIIGuardrail(
            final_payload_leak_check_canaries=(canary,),
        )
        guardrail._redis = _mock_redis()
        data = {
            "model": "glm-5.1",
            "messages": [{"role": "user", "content": f"Summarize {canary}"}],
        }

        with patch.object(
            guardrail,
            "_analyze_text",
            AsyncMock(return_value=[]),
        ) as analyze_text:
            with caplog.at_level(
                logging.INFO,
                logger="litellm_guardrails.pii_guardrail",
            ):
                with pytest.raises(litellm.UnprocessableEntityError) as exc_info:
                    await guardrail.async_pre_call_hook(
                        user_api_key_dict=MagicMock(),
                        cache=MagicMock(),
                        data=data,
                    )

        analyze_text.assert_awaited_once_with(f"Summarize {canary}")
        guardrail._redis.setex.assert_not_called()
        assert "metadata" not in data
        error_body = exc_info.value.response.json()
        assert exc_info.value.response.status_code == 422
        assert error_body["error"]["code"] == "final_payload_leak_check_blocked"
        assert error_body["error"]["details"] == {"rules": ["configured_canary"]}
        serialized = json.dumps(error_body, ensure_ascii=False)
        assert canary not in serialized

        logs = "\n".join(record.getMessage() for record in caplog.records)
        assert "final_payload_leak_check_blocked" in logs
        assert "configured_canary" in logs
        assert canary not in logs

        audit_events = _json_log_events(caplog, "gateway_guardrail_audit")
        assert len(audit_events) == 1
        event = audit_events[0]
        assert event["status"] == "blocked"
        assert event["policy_result"] == "final_payload_leak_check_blocked"
        assert event["block_reason"] == "final_payload_leak_check_violation"
        assert event["error_code"] == "final_payload_leak_check_blocked"
        assert event["rules"] == ["configured_canary"]
        assert event["rule_counts"] == {"configured_canary": 1}
        assert event["finding_count"] == 1
        assert event["redaction_count"] == 0

    @pytest.mark.asyncio
    async def test_final_payload_leak_check_blocks_responses_input_canary(self):
        canary = "RU_PROXY_RESPONSES_CANARY"
        guardrail = RuPIIGuardrail(
            final_payload_leak_check_canaries=(canary,),
        )
        guardrail._redis = _mock_redis()
        data = {
            "model": "openai-gpt-5.4-mini",
            "input": [
                {
                    "role": "user",
                    "content": [{"type": "input_text", "text": canary}],
                }
            ],
        }

        with patch.object(
            guardrail,
            "_analyze_text",
            AsyncMock(return_value=[]),
        ) as analyze_text:
            with pytest.raises(litellm.UnprocessableEntityError) as exc_info:
                await guardrail.async_pre_call_hook(
                    user_api_key_dict=MagicMock(),
                    cache=MagicMock(),
                    data=data,
                    call_type="responses",
                )

        analyze_text.assert_awaited_once_with(canary)
        guardrail._redis.setex.assert_not_called()
        error_body = exc_info.value.response.json()
        assert error_body["error"]["code"] == "final_payload_leak_check_blocked"
        assert error_body["error"]["details"] == {"rules": ["configured_canary"]}
        assert canary not in json.dumps(error_body, ensure_ascii=False)

    @pytest.mark.asyncio
    @pytest.mark.parametrize("item_type", ["reasoning", "compaction"])
    async def test_responses_encrypted_content_is_opaque_to_guardrail(
        self,
        item_type,
        caplog,
    ):
        canary = "RU_PROXY_ENCRYPTED_STATE_CANARY"
        encrypted_content = f"opaque-prefix-{canary}-opaque-suffix"
        pii_text = "Телефон клиента +79031234567"
        guardrail = RuPIIGuardrail(
            final_payload_leak_check_canaries=(canary,),
        )
        guardrail._redis = _mock_redis()
        save_mapping = AsyncMock()
        data = {
            "model": "openai-responses-model",
            "input": [
                {
                    "type": item_type,
                    "id": f"{item_type}-1",
                    "encrypted_content": encrypted_content,
                },
                {
                    "role": "user",
                    "content": [{"type": "input_text", "text": pii_text}],
                },
            ],
        }

        caplog.set_level(logging.INFO)
        with patch.object(
            guardrail,
            "_analyze_text",
            AsyncMock(
                return_value=[_entity(pii_text, "+79031234567")],
            ),
        ) as analyze_text:
            with patch.object(guardrail, "_save_mapping", save_mapping):
                result = await guardrail.async_pre_call_hook(
                    user_api_key_dict=MagicMock(),
                    cache=MagicMock(),
                    data=data,
                    call_type="responses",
                )

        analyze_text.assert_awaited_once_with(pii_text)
        assert result["input"][0]["encrypted_content"] == encrypted_content
        assert result["input"][1]["content"][0]["text"] == (
            "Телефон клиента <PHONE_NUMBER_1>"
        )
        assert save_mapping.call_args[0][1] == {
            "<PHONE_NUMBER_1>": "+79031234567",
        }
        assert encrypted_content not in "\n".join(
            record.getMessage() for record in caplog.records
        )

    @pytest.mark.asyncio
    async def test_encrypted_content_outside_known_responses_items_is_scanned(self):
        canary = "RU_PROXY_UNTRUSTED_ENCRYPTED_FIELD_CANARY"
        guardrail = RuPIIGuardrail(
            final_payload_leak_check_canaries=(canary,),
        )
        guardrail._redis = _mock_redis()
        data = {
            "model": "openai-responses-model",
            "input": [
                {
                    "type": "custom_input",
                    "encrypted_content": canary,
                },
                {"role": "user", "content": "Чистый запрос"},
            ],
        }

        with patch.object(
            guardrail,
            "_analyze_text",
            AsyncMock(return_value=[]),
        ) as analyze_text:
            with pytest.raises(litellm.UnprocessableEntityError) as exc_info:
                await guardrail.async_pre_call_hook(
                    user_api_key_dict=MagicMock(),
                    cache=MagicMock(),
                    data=data,
                    call_type="responses",
                )

        analyze_text.assert_awaited_once_with("Чистый запрос")
        error_body = exc_info.value.response.json()
        assert error_body["error"]["code"] == "final_payload_leak_check_blocked"
        assert error_body["error"]["details"] == {"rules": ["configured_canary"]}

    def test_response_encrypted_content_is_not_a_restoration_target(self):
        encrypted_content = "opaque-response-state-<PHONE_NUMBER_1>"
        message = {
            "content": "Телефон <PHONE_NUMBER_1>",
            "reasoning_content": "Проверяю <PHONE_NUMBER_1>",
            "encrypted_content": encrypted_content,
        }

        targets = RuPIIGuardrail._iter_response_text_targets(message)

        assert (message, "content") in targets
        assert (message, "reasoning_content") in targets
        assert (message, "encrypted_content") not in targets
        assert message["encrypted_content"] == encrypted_content

    @pytest.mark.asyncio
    async def test_final_payload_leak_check_blocks_extra_body_canary(self):
        canary = "RU_PROXY_EXTRA_BODY_CANARY"
        guardrail = RuPIIGuardrail(
            final_payload_leak_check_canaries=(canary,),
        )
        guardrail._redis = _mock_redis()
        data = {
            "model": "glm-5.1",
            "messages": [{"role": "user", "content": "Use provider options."}],
            "extra_body": {
                "providerOptions": {
                    "trace": canary,
                }
            },
        }

        with patch.object(
            guardrail,
            "_analyze_text",
            AsyncMock(return_value=[]),
        ) as analyze_text:
            with pytest.raises(litellm.UnprocessableEntityError) as exc_info:
                await guardrail.async_pre_call_hook(
                    user_api_key_dict=MagicMock(),
                    cache=MagicMock(),
                    data=data,
                )

        analyze_text.assert_awaited_once_with("Use provider options.")
        guardrail._redis.setex.assert_not_called()
        error_body = exc_info.value.response.json()
        assert error_body["error"]["code"] == "final_payload_leak_check_blocked"
        assert error_body["error"]["details"] == {"rules": ["configured_canary"]}
        assert canary not in json.dumps(error_body, ensure_ascii=False)

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "request_fragment",
        [
            {
                "tools": [
                    {
                        "type": "function",
                        "function": {
                            "name": "lookup_account",
                            "description": "Use RU_PROXY_TOOL_SCHEMA_CANARY internally.",
                            "parameters": {"type": "object"},
                        },
                    }
                ],
            },
            {
                "tools": [
                    {
                        "type": "function",
                        "function": {
                            "name": "lookup_account",
                            "description": "Lookup account.",
                            "parameters": {
                                "type": "object",
                                "properties": {
                                    "account_id": {
                                        "type": "string",
                                        "description": "RU_PROXY_TOOL_SCHEMA_CANARY",
                                    }
                                },
                            },
                        },
                    }
                ],
            },
            {
                "tools": [
                    {
                        "type": "function",
                        "function": {
                            "name": "lookup_account",
                            "description": "Lookup account.",
                            "parameters": {
                                "type": "object",
                                "properties": {
                                    "RU_PROXY_TOOL_SCHEMA_CANARY": {
                                        "type": "string",
                                        "description": "Account id",
                                    }
                                },
                            },
                        },
                    }
                ],
            },
            {
                "functions": [
                    {
                        "name": "lookup_account",
                        "description": "RU_PROXY_TOOL_SCHEMA_CANARY",
                        "parameters": {"type": "object"},
                    }
                ],
            },
            {
                "function_call": {
                    "name": "RU_PROXY_TOOL_SCHEMA_CANARY",
                },
            },
            {
                "prediction": {
                    "type": "content",
                    "content": "RU_PROXY_TOOL_SCHEMA_CANARY",
                },
            },
            {
                "response_format": {
                    "type": "json_schema",
                    "json_schema": {
                        "name": "account_lookup",
                        "schema": {
                            "type": "object",
                            "properties": {
                                "account_id": {
                                    "type": "string",
                                    "description": "RU_PROXY_TOOL_SCHEMA_CANARY",
                                }
                            },
                        },
                    },
                }
            },
            {
                "text": {
                    "format": {
                        "type": "json_schema",
                        "name": "account_lookup",
                        "schema": {
                            "type": "object",
                            "properties": {
                                "account_id": {
                                    "type": "string",
                                    "description": "RU_PROXY_TOOL_SCHEMA_CANARY",
                                }
                            },
                        },
                    }
                }
            },
            {
                "stop": "RU_PROXY_TOOL_SCHEMA_CANARY",
            },
            {
                "stop": ["clean", "RU_PROXY_TOOL_SCHEMA_CANARY"],
            },
            {
                "stop_sequences": ["RU_PROXY_TOOL_SCHEMA_CANARY"],
            },
            {
                "prompt_cache_key": "RU_PROXY_TOOL_SCHEMA_CANARY",
            },
            {
                "safety_identifier": "RU_PROXY_TOOL_SCHEMA_CANARY",
            },
            {
                "user": "RU_PROXY_TOOL_SCHEMA_CANARY",
            },
            {
                "web_search_options": {
                    "user_location": {
                        "type": "approximate",
                        "city": "RU_PROXY_TOOL_SCHEMA_CANARY",
                    }
                },
            },
            {
                "metadata": {
                    "user_id": "RU_PROXY_TOOL_SCHEMA_CANARY",
                },
            },
        ],
    )
    async def test_final_payload_leak_check_blocks_schema_canary(
        self,
        request_fragment,
    ):
        canary = "RU_PROXY_TOOL_SCHEMA_CANARY"
        guardrail = RuPIIGuardrail(
            final_payload_leak_check_canaries=(canary,),
        )
        guardrail._redis = _mock_redis()
        data = {
            "model": "glm-5.1",
            "messages": [{"role": "user", "content": "Use the tool."}],
        }
        data.update(request_fragment)

        with patch.object(
            guardrail,
            "_analyze_text",
            AsyncMock(return_value=[]),
        ) as analyze_text:
            with pytest.raises(litellm.UnprocessableEntityError) as exc_info:
                await guardrail.async_pre_call_hook(
                    user_api_key_dict=MagicMock(),
                    cache=MagicMock(),
                    data=data,
                )

        analyze_text.assert_awaited_once_with("Use the tool.")
        guardrail._redis.setex.assert_not_called()
        error_body = exc_info.value.response.json()
        assert error_body["error"]["code"] == "final_payload_leak_check_blocked"
        assert error_body["error"]["details"] == {"rules": ["configured_canary"]}
        assert canary not in json.dumps(error_body, ensure_ascii=False)

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "request_fragment",
        [
            {
                "tools": [
                    {
                        "type": "function",
                        "function": {
                            "name": "lookup_account",
                            "description": "PASSWORD=local-password",
                            "parameters": {"type": "object"},
                        },
                    }
                ],
            },
            {
                "extra_body": {
                    "debug": "DATABASE_URL=postgres://user:pass@db.local/app",
                },
            },
            {
                "extra_body": {
                    "DATABASE_URL": "postgres://user:pass@db.local/app",
                },
            },
            {
                "extra_body": {
                    "PASSWORD": "local-password",
                },
            },
            {
                "tools": [
                    {
                        "type": "function",
                        "function": {
                            "name": "lookup_account",
                            "description": "Lookup account",
                            "parameters": {
                                "type": "object",
                                "properties": {
                                    "DATABASE_URL": {
                                        "type": "string",
                                        "default": "postgres://user:pass@db.local/app",
                                    }
                                },
                            },
                        },
                    }
                ],
            },
        ],
        ids=[
            "tool-description-env-secret",
            "extra-body-credential-url",
            "extra-body-secret-key-url-value",
            "extra-body-secret-key-scalar-value",
            "tool-schema-secret-key-default",
        ],
    )
    async def test_final_payload_leak_check_blocks_structured_secret_markers(
        self,
        request_fragment,
    ):
        guardrail = RuPIIGuardrail()
        guardrail._redis = _mock_redis()
        data = {
            "model": "glm-5.1",
            "messages": [{"role": "user", "content": "Use provider config."}],
        }
        data.update(request_fragment)

        with patch.object(
            guardrail,
            "_analyze_text",
            AsyncMock(return_value=[]),
        ) as analyze_text:
            with pytest.raises(litellm.UnprocessableEntityError) as exc_info:
                await guardrail.async_pre_call_hook(
                    user_api_key_dict=MagicMock(),
                    cache=MagicMock(),
                    data=data,
                )

        analyze_text.assert_awaited_once_with("Use provider config.")
        guardrail._redis.setex.assert_not_called()
        error_body = exc_info.value.response.json()
        assert error_body["error"]["code"] == "final_payload_leak_check_blocked"
        assert error_body["error"]["details"] == {"rules": ["env_secret_assignment"]}
        serialized = json.dumps(error_body, ensure_ascii=False)
        assert "local-password" not in serialized
        assert "user:pass" not in serialized

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "request_fragment",
        [
            {
                "tools": [
                    {
                        "type": "function",
                        "function": {
                            "name": "lookup_account",
                            "description": "RU_PROXY_SCHEMA_ONLY_CANARY",
                            "parameters": {"type": "object"},
                        },
                    }
                ],
            },
            {
                "functions": [
                    {
                        "name": "lookup_account",
                        "description": "RU_PROXY_SCHEMA_ONLY_CANARY",
                        "parameters": {"type": "object"},
                    }
                ],
            },
            {
                "function_call": {
                    "name": "RU_PROXY_SCHEMA_ONLY_CANARY",
                },
            },
            {
                "prediction": {
                    "type": "content",
                    "content": "RU_PROXY_SCHEMA_ONLY_CANARY",
                },
            },
        ],
    )
    async def test_final_payload_leak_check_blocks_schema_without_message_targets(
        self,
        request_fragment,
    ):
        canary = "RU_PROXY_SCHEMA_ONLY_CANARY"
        guardrail = RuPIIGuardrail(
            final_payload_leak_check_canaries=(canary,),
        )
        guardrail._redis = _mock_redis()
        data = {"model": "glm-5.1"}
        data.update(request_fragment)

        with patch.object(
            guardrail,
            "_analyze_text",
            AsyncMock(return_value=[]),
        ) as analyze_text:
            with pytest.raises(litellm.UnprocessableEntityError) as exc_info:
                await guardrail.async_pre_call_hook(
                    user_api_key_dict=MagicMock(),
                    cache=MagicMock(),
                    data=data,
                )

        analyze_text.assert_not_awaited()
        guardrail._redis.setex.assert_not_called()
        error_body = exc_info.value.response.json()
        assert error_body["error"]["code"] == "final_payload_leak_check_blocked"
        assert error_body["error"]["details"] == {"rules": ["configured_canary"]}
        assert canary not in json.dumps(error_body, ensure_ascii=False)

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "payload",
        [
            {
                "model": "claude-opus",
                "system": "RU_PROXY_MESSAGES_CANARY",
                "messages": [{"role": "user", "content": "hello"}],
            },
            {
                "model": "claude-opus",
                "system": [{"type": "text", "text": "RU_PROXY_MESSAGES_CANARY"}],
                "messages": [{"role": "user", "content": "hello"}],
            },
            {
                "model": "claude-opus",
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": "toolu_1",
                                "content": "RU_PROXY_MESSAGES_CANARY",
                            }
                        ],
                    }
                ],
            },
            {
                "model": "claude-opus",
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": "toolu_1",
                                "content": [
                                    {
                                        "type": "text",
                                        "text": "RU_PROXY_MESSAGES_CANARY",
                                    }
                                ],
                            }
                        ],
                    }
                ],
            },
            {
                "model": "claude-opus",
                "messages": [
                    {
                        "role": "assistant",
                        "content": [
                            {
                                "type": "tool_use",
                                "id": "toolu_1",
                                "name": "lookup_account",
                                "input": {
                                    "query": "RU_PROXY_MESSAGES_CANARY",
                                },
                            }
                        ],
                    }
                ],
            },
        ],
    )
    async def test_final_payload_leak_check_blocks_anthropic_messages_canary(
        self,
        payload,
    ):
        canary = "RU_PROXY_MESSAGES_CANARY"
        guardrail = RuPIIGuardrail(
            final_payload_leak_check_canaries=(canary,),
        )
        guardrail._redis = _mock_redis()

        with patch.object(
            guardrail,
            "_analyze_text",
            AsyncMock(return_value=[]),
        ):
            with pytest.raises(litellm.UnprocessableEntityError) as exc_info:
                await guardrail.async_pre_call_hook(
                    user_api_key_dict=MagicMock(),
                    cache=MagicMock(),
                    data=payload,
                    call_type="anthropic_messages",
                )

        guardrail._redis.setex.assert_not_called()
        error_body = exc_info.value.response.json()
        assert error_body["error"]["code"] == "final_payload_leak_check_blocked"
        assert error_body["error"]["details"] == {"rules": ["configured_canary"]}
        assert canary not in json.dumps(error_body, ensure_ascii=False)

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("payload", "call_type", "expected_analyzer_texts"),
        [
            (
                {
                    "model": "glm-5.1",
                    "messages": [
                        {
                            "role": "user",
                            "content": [
                                {"type": "text", "text": "Clean prompt"},
                                {
                                    "type": "image_url",
                                    "image_url": {"url": "RU_PROXY_FINAL_CANARY"},
                                },
                            ],
                        }
                    ],
                },
                None,
                ["Clean prompt"],
            ),
            (
                {
                    "model": "glm-5.1",
                    "messages": [
                        {
                            "role": "user",
                            "content": [
                                {"type": "text", "text": "Clean prompt"},
                                {
                                    "type": "file",
                                    "file": {"file_id": "RU_PROXY_FINAL_CANARY"},
                                },
                            ],
                        }
                    ],
                },
                None,
                ["Clean prompt"],
            ),
            (
                {
                    "model": "glm-5.1",
                    "messages": [
                        {
                            "role": "tool",
                            "tool_call_id": "RU_PROXY_FINAL_CANARY",
                            "content": "Clean tool result",
                        }
                    ],
                },
                None,
                ["Clean tool result"],
            ),
            (
                {
                    "model": "openai-gpt-5.4-mini",
                    "input": [
                        {
                            "type": "function_call_output",
                            "call_id": "RU_PROXY_FINAL_CANARY",
                            "output": "Clean function output",
                        }
                    ],
                },
                "responses",
                ["Clean function output"],
            ),
            (
                {
                    "model": "openai-gpt-5.4-mini",
                    "input": [
                        {
                            "role": "user",
                            "content": [
                                {"type": "input_text", "text": "Clean prompt"},
                                {
                                    "type": "input_image",
                                    "image_url": "RU_PROXY_FINAL_CANARY",
                                },
                            ],
                        }
                    ],
                },
                "responses",
                ["Clean prompt"],
            ),
            (
                {
                    "model": "claude-opus",
                    "messages": [
                        {
                            "role": "assistant",
                            "content": [
                                {
                                    "type": "tool_use",
                                    "id": "RU_PROXY_FINAL_CANARY",
                                    "name": "lookup_account",
                                    "input": {"query": "clean"},
                                }
                            ],
                        }
                    ],
                },
                "anthropic_messages",
                [],
            ),
            (
                {
                    "model": "claude-opus",
                    "messages": [
                        {
                            "role": "assistant",
                            "content": [
                                {
                                    "type": "tool_use",
                                    "id": "toolu_1",
                                    "name": "RU_PROXY_FINAL_CANARY",
                                    "input": {"query": "clean"},
                                }
                            ],
                        }
                    ],
                },
                "anthropic_messages",
                [],
            ),
        ],
        ids=[
            "chat-image-url",
            "chat-file-id",
            "chat-tool-call-id",
            "responses-call-id",
            "responses-input-image-url",
            "anthropic-tool-use-id",
            "anthropic-tool-use-name",
        ],
    )
    async def test_final_payload_leak_check_blocks_provider_bound_non_text_strings(
        self,
        payload,
        call_type,
        expected_analyzer_texts,
    ):
        canary = "RU_PROXY_FINAL_CANARY"
        guardrail = RuPIIGuardrail(
            final_payload_leak_check_canaries=(canary,),
        )
        guardrail._redis = _mock_redis()

        with patch.object(
            guardrail,
            "_analyze_text",
            AsyncMock(return_value=[]),
        ) as analyze_text:
            with pytest.raises(litellm.UnprocessableEntityError) as exc_info:
                await guardrail.async_pre_call_hook(
                    user_api_key_dict=MagicMock(),
                    cache=MagicMock(),
                    data=payload,
                    call_type=call_type,
                )

        assert [
            call.args[0] for call in analyze_text.await_args_list
        ] == expected_analyzer_texts
        guardrail._redis.setex.assert_not_called()
        error_body = exc_info.value.response.json()
        assert error_body["error"]["code"] == "final_payload_leak_check_blocked"
        assert error_body["error"]["details"] == {"rules": ["configured_canary"]}
        assert canary not in json.dumps(error_body, ensure_ascii=False)

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("payload", "expected_rule"),
        [
            ("-----BEGIN PRIVATE KEY-----\nredacted\n-----END PRIVATE KEY-----", "private_key_marker"),
            (
                "-----BEGIN PGP PRIVATE KEY BLOCK-----\nredacted\n"
                "-----END PGP PRIVATE KEY BLOCK-----",
                "private_key_marker",
            ),
            (
                "-----BEGIN SSH2 ENCRYPTED PRIVATE KEY-----\nredacted\n"
                "-----END SSH2 ENCRYPTED PRIVATE KEY-----",
                "private_key_marker",
            ),
            ("Authorization: Bearer abcdefghijklmnopqrstuvwxyz123456", "bearer_token"),
            (
                "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0."
                "SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c",
                "jwt_token",
            ),
            ("sk-1234567890abcdef1234567890abcdef", "provider_key"),
            ("sk-ant-1234567890abcdef1234567890abcdef", "provider_key"),
        ],
    )
    async def test_final_payload_leak_check_blocks_high_confidence_markers(
        self,
        payload,
        expected_rule,
    ):
        guardrail = RuPIIGuardrail()
        guardrail._redis = _mock_redis()
        data = {"model": "glm-5.1", "messages": [{"role": "user", "content": payload}]}

        with patch.object(
            guardrail,
            "_analyze_text",
            AsyncMock(return_value=[]),
        ) as analyze_text:
            with pytest.raises(litellm.UnprocessableEntityError) as exc_info:
                await guardrail.async_pre_call_hook(
                    user_api_key_dict=MagicMock(),
                    cache=MagicMock(),
                    data=data,
                )

        analyze_text.assert_awaited_once_with(payload)
        guardrail._redis.setex.assert_not_called()
        error_body = exc_info.value.response.json()
        assert error_body["error"]["code"] == "final_payload_leak_check_blocked"
        assert error_body["error"]["details"] == {"rules": [expected_rule]}
        assert payload not in json.dumps(error_body, ensure_ascii=False)

    @pytest.mark.asyncio
    async def test_final_payload_leak_check_runs_on_masked_provider_bound_text(self):
        canary = "RU_PROXY_CANARY_AFTER_MASK"
        phone = "+79031234567"
        text = f"Мой телефон {phone}. Canary {canary}."
        guardrail = RuPIIGuardrail(
            final_payload_leak_check_canaries=(canary,),
        )
        guardrail._redis = _mock_redis()
        data = {"model": "glm-5.1", "messages": [{"role": "user", "content": text}]}

        with patch.object(
            guardrail,
            "_analyze_text",
            AsyncMock(return_value=[_entity(text, phone)]),
        ) as analyze_text:
            with patch.object(
                guardrail,
                "_classify_final_payload_leak_check_texts",
                wraps=guardrail._classify_final_payload_leak_check_texts,
            ) as classify_final:
                with pytest.raises(litellm.UnprocessableEntityError) as exc_info:
                    await guardrail.async_pre_call_hook(
                        user_api_key_dict=MagicMock(),
                        cache=MagicMock(),
                        data=data,
                    )

        analyze_text.assert_awaited_once_with(text)
        guardrail._redis.setex.assert_not_called()
        assert data["messages"][0]["content"] == text
        assert "metadata" not in data
        final_texts = classify_final.call_args.args[0]
        assert f"Мой телефон <PHONE_NUMBER_1>. Canary {canary}." in final_texts
        assert phone not in "\n".join(final_texts)
        error_body = exc_info.value.response.json()
        assert error_body["error"]["code"] == "final_payload_leak_check_blocked"
        assert error_body["error"]["details"] == {"rules": ["configured_canary"]}
        assert canary not in json.dumps(error_body, ensure_ascii=False)

    @pytest.mark.asyncio
    async def test_final_payload_leak_check_blocks_canary_on_analyzer_fail_open(self):
        canary = "RU_PROXY_CANARY_ANALYZER_DOWN"
        guardrail = RuPIIGuardrail(
            failure_mode="fail_open",
            final_payload_leak_check_canaries=(canary,),
        )
        guardrail._redis = _mock_redis()
        data = {"messages": [{"role": "user", "content": canary}]}

        with patch.object(
            guardrail,
            "_analyze_text",
            AsyncMock(side_effect=Exception("connection error")),
        ) as analyze_text:
            with pytest.raises(litellm.UnprocessableEntityError) as exc_info:
                await guardrail.async_pre_call_hook(
                    user_api_key_dict=MagicMock(),
                    cache=MagicMock(),
                    data=data,
                )

        analyze_text.assert_awaited_once_with(canary)
        guardrail._redis.setex.assert_not_called()
        error_body = exc_info.value.response.json()
        assert error_body["error"]["code"] == "final_payload_leak_check_blocked"
        assert error_body["error"]["details"] == {"rules": ["configured_canary"]}
        assert canary not in json.dumps(error_body, ensure_ascii=False)

    @pytest.mark.asyncio
    async def test_final_payload_leak_check_off_allows_configured_canary(self):
        canary = "RU_PROXY_CANARY_ALLOWED_IN_DEV"
        guardrail = RuPIIGuardrail(
            final_payload_leak_check_mode="off",
            final_payload_leak_check_canaries=(canary,),
        )
        guardrail._redis = _mock_redis()
        data = {"messages": [{"role": "user", "content": canary}]}

        with patch.object(
            guardrail,
            "_analyze_text",
            AsyncMock(return_value=[]),
        ) as analyze_text:
            result = await guardrail.async_pre_call_hook(
                user_api_key_dict=MagicMock(),
                cache=MagicMock(),
                data=data,
            )

        assert result == data
        analyze_text.assert_awaited_once_with(canary)
        guardrail._redis.setex.assert_not_called()

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
    async def test_block_mode_rejects_bank_requisites_without_raw_values(self):
        guardrail = RuPIIGuardrail(pii_mode="block")
        guardrail._redis = _mock_redis()
        text = "БИК 044525225, расчетный счет 40702810900000000000"
        data = {"model": "glm-5.1", "messages": [{"role": "user", "content": text}]}

        with patch.object(
            guardrail,
            "_analyze_text",
            return_value=[
                _entity(text, "044525225", "RU_BIK"),
                _entity(text, "40702810900000000000", "RU_SETTLEMENT_ACCOUNT"),
            ],
        ):
            with pytest.raises(litellm.UnprocessableEntityError) as exc_info:
                await guardrail.async_pre_call_hook(
                    user_api_key_dict=MagicMock(),
                    cache=MagicMock(),
                    data=data,
                )

        assert data["messages"][0]["content"] == text
        guardrail._redis.setex.assert_not_called()
        error_body = exc_info.value.response.json()
        assert error_body["error"]["code"] == "pii_blocked"
        assert error_body["error"]["details"]["entities"] == [
            "RU_BIK",
            "RU_SETTLEMENT_ACCOUNT",
        ]
        serialized = json.dumps(error_body, ensure_ascii=False)
        assert "044525225" not in serialized
        assert "40702810900000000000" not in serialized

    @pytest.mark.asyncio
    async def test_block_mode_rejects_infrastructure_secrets_without_raw_values(self):
        guardrail = RuPIIGuardrail(pii_mode="block")
        guardrail._redis = _mock_redis()
        text = (
            "Internal endpoint 10.24.3.7 uses Authorization: Bearer "
            "abcdefghijklmnopqrstuvwxyz123456"
        )
        bearer = "Authorization: Bearer abcdefghijklmnopqrstuvwxyz123456"
        data = {"model": "glm-5.1", "messages": [{"role": "user", "content": text}]}

        with patch.object(
            guardrail,
            "_analyze_text",
            return_value=[
                _entity(text, "10.24.3.7", "INTERNAL_IP"),
                _entity(text, bearer, "BEARER_TOKEN"),
            ],
        ):
            with pytest.raises(litellm.UnprocessableEntityError) as exc_info:
                await guardrail.async_pre_call_hook(
                    user_api_key_dict=MagicMock(),
                    cache=MagicMock(),
                    data=data,
                )

        assert data["messages"][0]["content"] == text
        guardrail._redis.setex.assert_not_called()
        error_body = exc_info.value.response.json()
        assert error_body["error"]["code"] == "pii_blocked"
        assert error_body["error"]["details"]["entities"] == [
            "BEARER_TOKEN",
            "INTERNAL_IP",
        ]
        serialized = json.dumps(error_body, ensure_ascii=False)
        assert "10.24.3.7" not in serialized
        assert "abcdefghijklmnopqrstuvwxyz123456" not in serialized

    @pytest.mark.asyncio
    @pytest.mark.parametrize("entity_type,value", NER_GUARDRAIL_CASES)
    async def test_block_mode_rejects_every_ner_entity_type_without_raw_value(
        self,
        entity_type,
        value,
    ):
        guardrail = RuPIIGuardrail(pii_mode="block")
        guardrail._redis = _mock_redis()
        text = f"Чувствительное значение: {value}"
        data = {"messages": [{"role": "user", "content": text}]}

        with patch.object(
            guardrail,
            "_analyze_text",
            return_value=[_entity(text, value, entity_type)],
        ):
            with pytest.raises(litellm.UnprocessableEntityError) as exc_info:
                await guardrail.async_pre_call_hook(
                    user_api_key_dict=MagicMock(),
                    cache=MagicMock(),
                    data=data,
                )

        assert data["messages"][0]["content"] == text
        guardrail._redis.setex.assert_not_called()
        error_body = exc_info.value.response.json()
        assert error_body["error"]["details"]["entities"] == [entity_type]
        assert value not in json.dumps(error_body, ensure_ascii=False)

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

        audit_events = _json_log_events(caplog, "gateway_guardrail_audit")
        assert len(audit_events) == 1
        event = audit_events[0]
        assert event["status"] == "blocked"
        assert event["policy_result"] == "pii_blocked"
        assert event["block_reason"] == "pii_detected"
        assert event["error_code"] == "pii_blocked"
        assert event["entity_counts"] == {"PHONE_NUMBER": 1}
        assert event["redaction_count"] == 0

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
        guardrail.failure_mode = "fail_open"
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
    @pytest.mark.parametrize(
        "payload_factory",
        [
            lambda text: {"messages": [{"role": "user", "content": text}]},
            lambda text: {
                "messages": [
                    {
                        "role": "user",
                        "content": [{"type": "text", "text": text}],
                    }
                ]
            },
            lambda text: {
                "messages": [
                    {
                        "role": "assistant",
                        "content": "",
                        "tool_calls": [
                            {
                                "type": "function",
                                "function": {
                                    "name": "lookup_account",
                                    "arguments": text,
                                },
                            }
                        ],
                    }
                ]
            },
            lambda text: {"instructions": text, "input": "Clean input"},
            lambda text: {"input": text},
            lambda text: {
                "input": [
                    {
                        "type": "function_call_output",
                        "call_id": "call-1",
                        "output": text,
                    }
                ]
            },
            lambda text: {
                "system": text,
                "messages": [{"role": "user", "content": "hello"}],
            },
            lambda text: {
                "system": [{"type": "text", "text": text}],
                "messages": [{"role": "user", "content": "hello"}],
            },
            lambda text: {
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": "toolu_1",
                                "content": text,
                            }
                        ],
                    }
                ],
            },
            lambda text: {
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": "toolu_1",
                                "content": [{"type": "text", "text": text}],
                            }
                        ],
                    }
                ],
            },
        ],
        ids=[
            "chat-message",
            "chat-content-block",
            "tool-call-arguments",
            "responses-instructions",
            "responses-input-string",
            "responses-tool-output",
            "anthropic-system",
            "anthropic-system-block",
            "anthropic-tool-result",
            "anthropic-tool-result-block",
        ],
    )
    async def test_redis_save_error_fails_open_without_irreversible_placeholders(
        self,
        guardrail,
        payload_factory,
    ):
        guardrail.failure_mode = "fail_open"
        text = "Мой телефон +79031234567"
        guardrail._redis.setex.side_effect = RuntimeError("redis down")
        data = payload_factory(text)

        async def analyze_text(content):
            if "+79031234567" not in content:
                return []
            return [_entity(content, "+79031234567")]

        with patch.object(
            guardrail,
            "_analyze_text",
            AsyncMock(side_effect=analyze_text),
        ):
            result = await guardrail.async_pre_call_hook(
                user_api_key_dict=MagicMock(),
                cache=MagicMock(),
                data=data,
            )

        serialized = json.dumps(result, ensure_ascii=False)
        assert "<PHONE_NUMBER" not in serialized
        assert "+79031234567" in serialized
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
    async def test_unmasks_responses_api_response_and_cleans_mapping(self, guardrail):
        mapping = {
            "<PHONE_NUMBER_1>": "+79031234567",
            "<RU_INN_1>": "7707083893",
        }
        guardrail._redis.get.return_value = json.dumps(mapping)
        encrypted_content = "opaque-<PHONE_NUMBER_1>"
        response = _responses_api_response(
            [
                {
                    "id": "msg-1",
                    "type": "message",
                    "status": "completed",
                    "role": "assistant",
                    "content": [
                        {
                            "type": "output_text",
                            "text": "Телефон <PHONE_NUMBER_1>",
                            "annotations": [],
                        }
                    ],
                },
                {
                    "id": "call-1",
                    "type": "function_call",
                    "call_id": "fc-1",
                    "name": "lookup_inn",
                    "arguments": '{"inn":"<RU_INN_1>"}',
                    "status": "completed",
                },
                {
                    "id": "reasoning-1",
                    "type": "reasoning",
                    "summary": [
                        {
                            "type": "summary_text",
                            "text": "Проверен <PHONE_NUMBER_1>",
                        }
                    ],
                    "encrypted_content": encrypted_content,
                    "status": "completed",
                },
                {
                    "id": "compaction-1",
                    "type": "compaction",
                    "encrypted_content": encrypted_content,
                },
            ]
        )

        await guardrail.async_post_call_success_hook(
            data={"metadata": {"pii_request_id": "req-responses"}},
            user_api_key_dict=MagicMock(),
            response=response,
        )

        assert response.output[0].content[0].text == "Телефон +79031234567"
        assert response.output[1].arguments == '{"inn":"7707083893"}'
        assert response.output[2].summary[0].text == "Проверен +79031234567"
        assert response.output[2].encrypted_content == encrypted_content
        assert response.output[3].encrypted_content == encrypted_content
        guardrail._redis.delete.assert_awaited_once_with(
            "pii_mapping:req-responses"
        )

    @pytest.mark.asyncio
    async def test_unmasks_responses_api_dictionary_shape(self, guardrail):
        guardrail._redis.get.return_value = json.dumps(
            {"<PHONE_NUMBER_1>": "+79031234567"}
        )
        response = {
            "object": "response",
            "output": [
                {
                    "type": "message",
                    "content": [
                        {
                            "type": "output_text",
                            "text": "Телефон <PHONE_NUMBER_1>",
                        }
                    ],
                },
                {
                    "type": "custom_tool_call",
                    "input": "Проверь <PHONE_NUMBER_1>",
                },
                {
                    "type": "apply_patch_call_output",
                    "output": "Обновлён <PHONE_NUMBER_1>",
                },
                {
                    "type": "shell_call_output",
                    "output": [
                        {
                            "stdout": "stdout <PHONE_NUMBER_1>",
                            "stderr": "stderr <PHONE_NUMBER_1>",
                        }
                    ],
                },
            ],
        }

        await guardrail.async_post_call_success_hook(
            data={"metadata": {"pii_request_id": "req-dict"}},
            user_api_key_dict=MagicMock(),
            response=response,
        )

        assert response["output"][0]["content"][0]["text"] == (
            "Телефон +79031234567"
        )
        assert response["output"][1]["input"] == "Проверь +79031234567"
        assert response["output"][2]["output"] == "Обновлён +79031234567"
        assert response["output"][3]["output"][0] == {
            "stdout": "stdout +79031234567",
            "stderr": "stderr +79031234567",
        }
        guardrail._redis.delete.assert_awaited_once_with("pii_mapping:req-dict")

    @pytest.mark.asyncio
    async def test_responses_api_without_placeholders_still_cleans_mapping(
        self,
        guardrail,
    ):
        guardrail._redis.get.return_value = json.dumps(
            {"<PHONE_NUMBER_1>": "+79031234567"}
        )
        response = _responses_api_response(
            [
                {
                    "id": "msg-1",
                    "type": "message",
                    "status": "completed",
                    "role": "assistant",
                    "content": [
                        {
                            "type": "output_text",
                            "text": "Готово",
                            "annotations": [],
                        }
                    ],
                }
            ]
        )

        await guardrail.async_post_call_success_hook(
            data={"metadata": {"pii_request_id": "req-clean"}},
            user_api_key_dict=MagicMock(),
            response=response,
        )

        assert response.output[0].content[0].text == "Готово"
        guardrail._redis.delete.assert_awaited_once_with("pii_mapping:req-clean")

    @pytest.mark.asyncio
    async def test_unsupported_non_stream_response_still_cleans_mapping(
        self,
        guardrail,
        caplog,
    ):
        raw_value = "+79031234567"
        guardrail._redis.get.return_value = json.dumps(
            {"<PHONE_NUMBER_1>": raw_value}
        )

        caplog.set_level(logging.WARNING)
        await guardrail.async_post_call_success_hook(
            data={"metadata": {"pii_request_id": "req-unsupported"}},
            user_api_key_dict=MagicMock(),
            response=object(),
        )

        guardrail._redis.delete.assert_awaited_once_with(
            "pii_mapping:req-unsupported"
        )
        logs = "\n".join(record.getMessage() for record in caplog.records)
        assert "pii_guardrail_unsupported_response" in logs
        assert raw_value not in logs

    @pytest.mark.asyncio
    async def test_mapping_delete_failure_is_logged_without_raw_values(
        self,
        guardrail,
        caplog,
    ):
        raw_value = "+79031234567"
        guardrail._redis.get.return_value = json.dumps(
            {"<PHONE_NUMBER_1>": raw_value}
        )
        guardrail._redis.delete.side_effect = RuntimeError("redis unavailable")
        response = _responses_api_response(
            [
                {
                    "id": "msg-1",
                    "type": "message",
                    "status": "completed",
                    "role": "assistant",
                    "content": [
                        {
                            "type": "output_text",
                            "text": "Телефон <PHONE_NUMBER_1>",
                            "annotations": [],
                        }
                    ],
                }
            ]
        )

        caplog.set_level(logging.WARNING)
        await guardrail.async_post_call_success_hook(
            data={"metadata": {"pii_request_id": "req-delete-error"}},
            user_api_key_dict=MagicMock(),
            response=response,
        )

        assert response.output[0].content[0].text == f"Телефон {raw_value}"
        logs = "\n".join(record.getMessage() for record in caplog.records)
        assert "pii_guardrail_cleanup_failed" in logs
        assert raw_value not in logs

    @pytest.mark.asyncio
    @pytest.mark.parametrize("entity_type,value", NER_GUARDRAIL_CASES)
    async def test_restores_every_ner_entity_type(
        self,
        guardrail,
        entity_type,
        value,
    ):
        placeholder = f"<{entity_type}_1>"
        guardrail._redis.get.return_value = json.dumps({placeholder: value})
        response = litellm.ModelResponse(
            id="test",
            choices=[
                litellm.Choices(
                    index=0,
                    message=litellm.Message(
                        role="assistant",
                        content=f"Подтверждено: {placeholder}",
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

        assert response.choices[0].message.content == f"Подтверждено: {value}"

    @pytest.mark.asyncio
    async def test_restores_dictionary_substitution_response(self, guardrail):
        import litellm

        guardrail._redis.get.return_value = json.dumps({"Зетта Групп": "Т-Банк"})
        response = litellm.ModelResponse(
            id="test",
            choices=[
                litellm.Choices(
                    index=0,
                    message=litellm.Message(
                        role="assistant",
                        content="Договор с Зетта Групп проверен",
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

        assert response.choices[0].message.content == "Договор с Т-Банк проверен"

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

        guardrail.failure_mode = "fail_open"
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


async def _anext(stream):
    return await stream.__anext__()


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
    async def test_restores_contract_number_split_across_stream_chunks(
        self,
        guardrail,
    ):
        guardrail._redis.get.return_value = json.dumps(
            {"<CONTRACT_NUMBER_1>": "OV-2026/81"}
        )
        chunks = [
            litellm.ModelResponseStream(
                choices=[
                    litellm.StreamingChoices(
                        index=0,
                        delta={"content": "Договор <CONTRACT_"},
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
        _yielded, content, _reasoning = await _collect_stream_text(result_stream)

        assert content == "Договор OV-2026/81 подтвержден"
        assert request_data["metadata"]["pii_streaming_restoration_done"] is True
        guardrail._redis.delete.assert_awaited_once_with("pii_mapping:req-1")

    @pytest.mark.asyncio
    async def test_restores_dictionary_replacement_split_across_chunks(self, guardrail):
        guardrail._redis.get.return_value = json.dumps({"Зетта Групп": "Т-Банк"})
        chunks = [
            litellm.ModelResponseStream(
                choices=[
                    litellm.StreamingChoices(
                        index=0,
                        delta={"content": "Договор с Зетта "},
                    )
                ]
            ),
            litellm.ModelResponseStream(
                choices=[
                    litellm.StreamingChoices(
                        index=0,
                        delta={"content": "Групп проверен"},
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
        _yielded, content, _reasoning = await _collect_stream_text(result_stream)

        assert content == "Договор с Т-Банк проверен"
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
        guardrail.failure_mode = "fail_open"
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
            await _anext(result_stream)

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

        first_result = await _anext(result_stream)
        assert first_result.choices[0].delta.content == "Начало "

        with pytest.raises(RuntimeError, match="upstream stream failed"):
            await _anext(result_stream)

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
