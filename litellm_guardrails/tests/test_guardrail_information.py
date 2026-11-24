"""Safe standard logging results for the pinned LiteLLM custom guardrail."""

import asyncio
import json
import time
from unittest.mock import AsyncMock, MagicMock, patch

import litellm
import pytest
from litellm.litellm_core_utils.litellm_logging import StandardLoggingPayloadSetup
from litellm.proxy.spend_tracking import spend_tracking_utils

import litellm_guardrails.pii_guardrail as pii_guardrail
from litellm_guardrails.env_assignment_masking import RuEnvAssignmentGuardrail
from litellm_guardrails.pii_guardrail import AnalyzerOverloadedError, RuPIIGuardrail


def entries(data):
    bucket = data.get("litellm_metadata", data.get("metadata", {}))
    return bucket["standard_logging_guardrail_information"]


def guardrail(**kwargs):
    g = RuPIIGuardrail(
        dictionary_substitutions_enabled=False, **kwargs,
    )
    g.guardrail_name = "ru-pii-mask-pre"
    g._redis = AsyncMock()
    g._redis.get.return_value = None
    return g


@pytest.mark.asyncio
@pytest.mark.parametrize("shape,call_type", [
    ({"messages": [{"role": "user", "content": "hello"}]}, "completion"),
    ({"input": "hello"}, "aresponses"),
    ({"messages": [{"role": "user", "content": "hello"}]}, "anthropic_messages"),
])
async def test_clean_has_one_safe_pre_call_result(shape, call_type):
    g = guardrail()
    data = {"model": "mock-chat", "metadata": {"client": "safe"}, **shape}
    with patch.object(g, "_analyze_text", return_value=[]):
        assert await g.async_pre_call_hook(MagicMock(), MagicMock(), data, call_type) is data
    (entry,) = entries(data)
    assert entry["guardrail_name"] == "ru-pii-mask-pre"
    assert entry["guardrail_provider"] == "ru-llm-proxy"
    assert entry["guardrail_mode"] == "pre_call"
    assert entry["guardrail_status"] == "success"
    assert entry["guardrail_action"] == "clean"
    assert entry["guardrail_response"] == {"policy_result": "clean"}
    assert entry["start_time"] <= entry["end_time"]
    assert entry["duration"] >= 0
    assert data["metadata"]["client"] == "safe"


@pytest.mark.asyncio
async def test_masking_counts_and_payload_do_not_expose_source():
    g = guardrail()
    g._redis.pipeline.return_value.__aenter__ = AsyncMock()
    data = {
        "model": "mock-chat",
        "messages": [{"role": "user", "content": "alice@example.test; bob@example.test"}],
        "litellm_metadata": {},
        "metadata": {"client": "keep"},
    }

    async def analyze(text):
        return [
            {"start": text.index(value), "end": text.index(value) + len(value),
             "entity_type": "EMAIL_ADDRESS", "score": 1.0}
            for value in ("alice@example.test", "bob@example.test")
        ]

    with patch.object(g, "_analyze_text", side_effect=analyze):
        await g.async_pre_call_hook(MagicMock(), MagicMock(), data, "completion")
    (entry,) = entries(data)
    assert entry["guardrail_status"] == "guardrail_intervened"
    assert entry["masked_entity_count"] == {"EMAIL_ADDRESS": 2}
    assert entry["violation_categories"] == ["EMAIL_ADDRESS"]
    assert "alice@example.test" not in json.dumps(entry)
    assert "bob@example.test" not in json.dumps(entry)
    assert data["metadata"]["client"] == "keep"


@pytest.mark.asyncio
@pytest.mark.parametrize("shape,call_type", [
    ({"messages": [{"role": "user", "content": "SECRET_PROMPT"}]}, "completion"),
    ({"input": "SECRET_PROMPT", "litellm_metadata": {}}, "aresponses"),
    ({"messages": [{"role": "user", "content": "SECRET_PROMPT"}]}, "anthropic_messages"),
])
async def test_policy_block_reports_multiple_rules_once_without_prompt(shape, call_type):
    g = guardrail()
    data = {"model": "mock-chat", "metadata": {"client": "keep"}, **shape}
    findings = [
        {"category": "config", "rule_id": "env_secret_assignment"},
        {"category": "config", "rule_id": "kubeconfig_secret"},
    ]
    with patch.object(g, "_classify_pre_egress_policy_targets", return_value=findings):
        with pytest.raises(Exception):
            await g.async_pre_call_hook(MagicMock(), MagicMock(), data, call_type)
    (entry,) = entries(data)
    assert entry["guardrail_status"] == "guardrail_intervened"
    assert entry["guardrail_action"] == "pre_egress_policy_blocked"
    assert entry["violation_categories"] == ["env_secret_assignment", "kubeconfig_secret"]
    assert entry["guardrail_response"]["rule_counts"] == {
        "env_secret_assignment": 1, "kubeconfig_secret": 1,
    }
    assert "SECRET_PROMPT" not in json.dumps(entry)
    assert data["metadata"]["client"] == "keep"


@pytest.mark.asyncio
async def test_fail_open_is_technical_failure_not_success():
    g = guardrail(failure_mode="fail_open")
    data = {"model": "mock-chat", "messages": [{"role": "user", "content": "SECRET_PROMPT"}]}
    with patch.object(g, "_analyze_text", side_effect=RuntimeError("SECRET_PROMPT")):
        assert await g.async_pre_call_hook(MagicMock(), MagicMock(), data) is data
    (entry,) = entries(data)
    assert entry["guardrail_status"] == "guardrail_failed_to_respond"
    assert entry["guardrail_action"] == "fail_open"
    assert "SECRET_PROMPT" not in json.dumps(entry)


@pytest.mark.asyncio
async def test_fail_closed_records_failure_without_exception_text():
    g = guardrail()
    data = {"model": "mock-chat", "messages": [{"role": "user", "content": "SECRET_PROMPT"}]}
    with patch.object(g, "_analyze_text", side_effect=RuntimeError("SECRET_PROMPT")):
        with pytest.raises(RuntimeError):
            await g.async_pre_call_hook(MagicMock(), MagicMock(), data)
    (entry,) = entries(data)
    assert entry["guardrail_status"] == "guardrail_failed_to_respond"
    assert entry["guardrail_action"] == "fail_closed"
    assert entry["guardrail_response"]["error_type"] == "RuntimeError"
    assert "SECRET_PROMPT" not in json.dumps(entry)


@pytest.mark.asyncio
async def test_analyzer_overload_remains_technical_even_with_fail_open():
    g = guardrail(failure_mode="fail_open")
    data = {"model": "mock-chat", "input": "SECRET_PROMPT", "litellm_metadata": {}}
    with patch.object(g, "_analyze_text", side_effect=AnalyzerOverloadedError(reason="queue_full")):
        with pytest.raises(Exception):
            await g.async_pre_call_hook(MagicMock(), MagicMock(), data, "aresponses")
    (entry,) = entries(data)
    assert entry["guardrail_status"] == "guardrail_failed_to_respond"
    assert entry["guardrail_action"] == "analyzer_overloaded"
    assert "SECRET_PROMPT" not in json.dumps(entry)


@pytest.mark.asyncio
async def test_pii_block_reports_entity_type_but_not_detected_value():
    g = guardrail(pii_mode="block")
    data = {"model": "mock-chat", "messages": [{"role": "user", "content": "secret@example.test"}]}
    entity = {"start": 0, "end": len("secret@example.test"), "entity_type": "EMAIL_ADDRESS", "score": 1.0}
    with patch.object(g, "_analyze_text", return_value=[entity]):
        with pytest.raises(litellm.UnprocessableEntityError):
            await g.async_pre_call_hook(MagicMock(), MagicMock(), data, "completion")
    (entry,) = entries(data)
    assert entry["guardrail_status"] == "guardrail_intervened"
    assert entry["violation_categories"] == ["EMAIL_ADDRESS"]
    assert entry["masked_entity_count"] == {}
    assert "secret@example.test" not in json.dumps(entry)


@pytest.mark.asyncio
async def test_post_call_separate_from_pre_call_and_no_duplicate_stream_success():
    g = guardrail()
    g.guardrail_name = "ru-pii-mask-post"
    data = {"metadata": {}, "messages": [{"role": "user", "content": "hello"}]}
    response = litellm.ModelResponse(
        choices=[{"index": 0, "message": {"role": "assistant", "content": "ok"}}],
    )
    await g.async_post_call_success_hook(data, MagicMock(), response)
    (entry,) = entries(data)
    assert entry["guardrail_mode"] == "post_call"
    assert entry["guardrail_status"] == "success"
    assert entry["guardrail_action"] == "skipped"
    data["stream"] = True
    data["metadata"]["pii_streaming_restoration_done"] = True
    await g.async_post_call_success_hook(data, MagicMock(), response)
    assert len(entries(data)) == 1


@pytest.mark.asyncio
async def test_stream_without_mapping_records_once_when_consumed():
    g = guardrail()
    g.guardrail_name = "ru-pii-mask-post"
    data = {"stream": True, "metadata": {}}

    async def chunks():
        yield litellm.ModelResponse(
            stream=True,
            choices=[{"index": 0, "delta": {"content": "ok"}, "finish_reason": "stop"}],
        )

    output = [chunk async for chunk in g.async_post_call_streaming_iterator_hook(
        MagicMock(), chunks(), data,
    )]
    assert len(output) == 1
    (entry,) = entries(data)
    assert entry["guardrail_mode"] == "post_call"
    assert entry["guardrail_status"] == "success"
    assert entry["duration"] >= 0
    response = litellm.ModelResponse(
        choices=[{"index": 0, "message": {"role": "assistant", "content": "ok"}}],
    )
    await g.async_post_call_success_hook(data, MagicMock(), response)
    assert len(entries(data)) == 1


@pytest.mark.asyncio
async def test_post_call_mapping_load_failure_is_not_success():
    g = guardrail(failure_mode="fail_open")
    g.guardrail_name = "ru-pii-mask-post"
    g._load_mapping = AsyncMock(side_effect=RuntimeError("SECRET_MAPPING"))
    data = {"model": "mock-chat", "metadata": {"pii_request_id": "server-id"}}
    response = litellm.ModelResponse(
        choices=[{"index": 0, "message": {"role": "assistant", "content": "ok"}}],
    )
    await g.async_post_call_success_hook(data, MagicMock(), response)
    (entry,) = entries(data)
    assert entry["guardrail_mode"] == "post_call"
    assert entry["guardrail_status"] == "guardrail_failed_to_respond"
    assert entry["guardrail_action"] == "fail_open"
    assert "SECRET_MAPPING" not in json.dumps(entry)


@pytest.mark.asyncio
async def test_post_call_restoration_failure_is_not_a_policy_block():
    g = guardrail()
    g.guardrail_name = "ru-pii-mask-post"
    g._load_mapping = AsyncMock(return_value={"<EMAIL_ADDRESS_1>": "SECRET_VALUE"})
    g._delete_mapping = AsyncMock()
    data = {"model": "mock-chat", "metadata": {"pii_request_id": "server-id"}}
    response = litellm.ModelResponse(
        choices=[{"index": 0, "message": {"role": "assistant", "content": "<EMAIL_ADDRESS_1>"}}],
    )
    with patch.object(g, "_replace_placeholders", side_effect=ValueError("SECRET_VALUE")):
        with pytest.raises(ValueError):
            await g.async_post_call_success_hook(data, MagicMock(), response)
    (entry,) = entries(data)
    assert entry["guardrail_status"] == "guardrail_failed_to_respond"
    assert entry["guardrail_action"] == "post_call_failure"
    assert "SECRET_VALUE" not in json.dumps(entry)


@pytest.mark.asyncio
async def test_post_call_restored_response_is_not_copied_into_guardrail_information():
    g = guardrail()
    g.guardrail_name = "ru-pii-mask-post"
    g._load_mapping = AsyncMock(return_value={"<EMAIL_ADDRESS_1>": "PRIVATE_EMAIL_VALUE"})
    g._delete_mapping = AsyncMock()
    data = {"model": "mock-chat", "metadata": {"pii_request_id": "server-id"}}
    response = litellm.ModelResponse(
        choices=[{"index": 0, "message": {"role": "assistant", "content": "<EMAIL_ADDRESS_1>"}}],
    )
    await g.async_post_call_success_hook(data, MagicMock(), response)
    assert response.choices[0].message.content == "PRIVATE_EMAIL_VALUE"
    (entry,) = entries(data)
    assert entry["guardrail_status"] == "success"
    assert entry["guardrail_action"] == "restored"
    assert "PRIVATE_EMAIL_VALUE" not in json.dumps(entry)


@pytest.mark.asyncio
async def test_post_call_cleanup_failure_is_technical_even_after_restoration():
    g = guardrail(failure_mode="fail_open")
    g.guardrail_name = "ru-pii-mask-post"
    g._load_mapping = AsyncMock(return_value={"<EMAIL_ADDRESS_1>": "PRIVATE_EMAIL_VALUE"})
    g._delete_mapping = AsyncMock(side_effect=RuntimeError("PRIVATE_MAPPING"))
    data = {"model": "mock-chat", "metadata": {"pii_request_id": "server-id"}}
    response = litellm.ModelResponse(
        choices=[{"index": 0, "message": {"role": "assistant", "content": "<EMAIL_ADDRESS_1>"}}],
    )
    await g.async_post_call_success_hook(data, MagicMock(), response)
    (entry,) = entries(data)
    assert response.choices[0].message.content == "PRIVATE_EMAIL_VALUE"
    assert entry["guardrail_status"] == "guardrail_failed_to_respond"
    assert entry["guardrail_response"]["failure_operation"] == "mapping_delete"
    assert "PRIVATE_MAPPING" not in json.dumps(entry)


def test_report_fields_are_allowlisted_and_timed():
    g = guardrail()
    data = {"litellm_metadata": {}, "metadata": {"client": "keep"}}
    decision = {
        "policy_result": "pre_egress_policy_blocked",
        "status": "guardrail_intervened",
        "rules": ["env_secret_assignment"],
        "rule_counts": {"env_secret_assignment": 2},
        "entity_counts": {},
    }
    g._write_guardrail_information(data, "pre_call", decision, time.time(), time.perf_counter())
    (entry,) = entries(data)
    assert entry["guardrail_response"] == {
        "policy_result": "pre_egress_policy_blocked",
        "rules": ["env_secret_assignment"],
        "rule_counts": {"env_secret_assignment": 2},
    }
    assert data["metadata"] == {"client": "keep"}


@pytest.mark.asyncio
async def test_pre_and_post_use_separate_cards_on_same_request():
    pre, post = guardrail(), guardrail()
    post.guardrail_name = "ru-pii-mask-post"
    data = {
        "model": "mock-chat", "litellm_metadata": {},
        "metadata": {"client": "preserved"},
        "messages": [{"role": "user", "content": "hello"}],
    }
    with patch.object(pre, "_analyze_text", return_value=[]):
        await pre.async_pre_call_hook(MagicMock(), MagicMock(), data, "completion")
    response = litellm.ModelResponse(
        choices=[{"index": 0, "message": {"role": "assistant", "content": "ok"}}],
    )
    await post.async_post_call_success_hook(data, MagicMock(), response)
    assert [(entry["guardrail_name"], entry["guardrail_mode"]) for entry in entries(data)] == [
        ("ru-pii-mask-pre", "pre_call"), ("ru-pii-mask-post", "post_call"),
    ]
    assert data["metadata"] == {"client": "preserved"}


def test_untrusted_rule_labels_and_exception_messages_are_not_serialized():
    g = guardrail()
    decision = {}
    token = pii_guardrail._STANDARD_LOGGING_DECISION.set(decision)
    try:
        g._set_logging_decision(
            "pre_egress_policy_blocked",
            rules=["safe_rule", "raw@example.test", "SECRET=" + "x" * 100],
            rule_counts={"safe_rule": 1, "raw@example.test": 2},
            error_type="ValueError\nSECRET_VALUE",
        )
    finally:
        pii_guardrail._STANDARD_LOGGING_DECISION.reset(token)
    data = {"litellm_metadata": {}}
    g._write_guardrail_information(data, "pre_call", decision, time.time(), time.perf_counter())
    (entry,) = entries(data)
    assert entry["violation_categories"] == ["safe_rule"]
    assert entry["guardrail_response"] == {
        "policy_result": "pre_egress_policy_blocked",
        "rules": ["safe_rule"],
        "rule_counts": {"safe_rule": 1},
    }


def test_pinned_litellm_merges_internal_bucket_for_spend_logging():
    g = guardrail()
    data = {"metadata": {"client_key": "client-value"}, "litellm_metadata": {}}
    g._write_guardrail_information(
        data, "pre_call",
        {"policy_result": "clean", "status": "success"},
        time.time(), time.perf_counter(),
    )
    combined = StandardLoggingPayloadSetup.merge_litellm_metadata(data)
    assert combined["standard_logging_guardrail_information"] == entries(data)
    assert combined["client_key"] == "client-value"


def test_pinned_spend_log_redaction_keeps_guardrail_card_fields():
    g = guardrail()
    data = {"litellm_metadata": {}}
    g._write_guardrail_information(
        data, "pre_call",
        {"policy_result": "pre_egress_policy_blocked",
         "status": "guardrail_intervened",
         "rules": ["env_secret_assignment"],
         "entity_counts": {}},
        time.time(), time.perf_counter(),
    )
    with patch.object(spend_tracking_utils, "_should_store_prompts_and_responses_in_spend_logs", return_value=False):
        (stored,) = spend_tracking_utils._sanitize_guardrail_information_for_spend_logs(entries(data))
    assert stored["guardrail_status"] == "guardrail_intervened"
    assert stored["guardrail_action"] == "pre_egress_policy_blocked"
    assert stored["violation_categories"] == ["env_secret_assignment"]
    assert stored["guardrail_response"] == spend_tracking_utils.REDACTED_BY_LITELM_STRING
    assert stored["ru_guardrail_report"]["rules"] == ["env_secret_assignment"]


@pytest.mark.asyncio
async def test_overlapping_block_and_mask_keep_separate_counts_after_redaction():
    arrived = asyncio.Barrier(2)
    text = "Alfa Bravo Delta Alfa"
    spans = [(0, 4, "PERSON"), (5, 10, "PASSWORD"), (11, 16, "ORGANIZATION"), (17, 21, "PERSON")]

    async def analyze(_):
        await arrived.wait()
        return [dict(start=i, end=j, entity_type=t, score=1.) for i, j, t in spans]

    async def run(mode):
        g = guardrail(pii_mode=mode)
        g._analyze_text = analyze
        data = {"messages": [{"role": "user", "content": text}]}
        if mode == "block":
            with pytest.raises(litellm.UnprocessableEntityError):
                await g.async_pre_call_hook(MagicMock(), MagicMock(), data)
        else:
            await g.async_pre_call_hook(MagicMock(), MagicMock(), data)
        with patch.object(spend_tracking_utils, "_should_store_prompts_and_responses_in_spend_logs", return_value=False):
            entry, = spend_tracking_utils._sanitize_guardrail_information_for_spend_logs(entries(data))
        assert "Bravo" not in json.dumps(entry)
        return entry

    blocked, masked = await asyncio.gather(run("block"), run("mask"))
    expected = {"PERSON": 2, "PASSWORD": 1, "ORGANIZATION": 1}
    assert blocked["ru_guardrail_report"]["detected_entity_count"] == expected
    assert blocked["masked_entity_count"] == {}
    assert masked["masked_entity_count"] == expected
    assert masked["ru_guardrail_report"]["analysis"] == "complete"


@pytest.mark.asyncio
async def test_env_mask_reports_on_returned_copy_without_double_count():
    g = RuEnvAssignmentGuardrail(guardrail_name="env", dictionary_substitutions_enabled=False)
    g._redis = AsyncMock()
    # Simulate a recognizer which also finds the generated ENV alias.
    async def analyze(text):
        start = text.index("ENV_MASK")
        return [{"entity_type": "PASSWORD", "start": start, "end": len(text), "score": 1.}]
    g._analyze_text = analyze
    original = {"messages": [{"role": "user", "content": "PASSWORD=private_value"}]}
    result = await g.async_pre_call_hook(MagicMock(), MagicMock(), original)
    assert result is not original
    entry, = entries(result)
    assert entry["masked_entity_count"] == {"PASSWORD": 1}
    assert entry["ru_guardrail_report"]["detected_entity_count"] == {"PASSWORD": 1}
    assert "private_value" not in json.dumps(entry)


@pytest.mark.asyncio
async def test_irreversible_rule_without_mapping_reports_substitution():
    g = RuPIIGuardrail(guardrail_name="pre", dictionary_substitutions_json=json.dumps({
        "substitutions": [{"id": "example_swap", "source": "Alfa", "replacement": "Beta", "restore": False}],
    }))
    g._analyze_text = AsyncMock(return_value=[])
    data = {"messages": [{"role": "user", "content": "Alfa Alfa"}]}
    await g.async_pre_call_hook(MagicMock(), MagicMock(), data)
    entry, = entries(data)
    assert data["messages"][0]["content"] == "Beta Beta"
    assert entry["guardrail_action"] == "dictionary_substituted"
    assert entry["ru_guardrail_report"]["rule_counts"] == {"example_swap": 2}


@pytest.mark.asyncio
async def test_refusal_before_analysis_and_early_exception_reset_context():
    g = guardrail()
    with pytest.raises(pii_guardrail.HTTPException) as caught:
        data = {"input": [{"type": "input_file", "file_id": "test-reference"}]}
        await g.async_pre_call_hook(MagicMock(), MagicMock(), data)
    assert caught.value.status_code == 422
    entry, = entries(data)
    assert entry["guardrail_status"] == "guardrail_intervened"
    assert entry["ru_guardrail_report"]["error_code"] == "attachment_reference_unsupported"
    assert entry["ru_guardrail_report"]["analysis"] == "not_started"
    with patch.object(g, "_iter_request_text_targets", side_effect=ValueError("private value")):
        with pytest.raises(ValueError):
            await g.async_pre_call_hook(MagicMock(), MagicMock(), data := {})
    assert entries(data)[0]["guardrail_action"] == "guardrail_error"
    assert pii_guardrail._STANDARD_LOGGING_DECISION.get() is None


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True])
async def test_closed_mapping_failure_survives_exception_translation(stream):
    g = guardrail(failure_mode="fail_closed")
    g._load_mapping = AsyncMock(side_effect=ConnectionError("private"))
    g._delete_mapping = AsyncMock(side_effect=RuntimeError("secondary"))
    data = {"metadata": {"pii_request_id": "server-id"}}
    with pytest.raises(pii_guardrail.ProxyException):
        if stream:
            async def chunks():
                yield litellm.ModelResponse(stream=True)
            await anext(g.async_post_call_streaming_iterator_hook(MagicMock(), chunks(), data))
        else:
            await g.async_post_call_success_hook(data, MagicMock(), litellm.ModelResponse())
    entry, = entries(data)
    assert entry["guardrail_action"] == "fail_closed"
    assert entry["ru_guardrail_report"]["failure_operation"] == ("stream_mapping_load" if stream else "mapping_load")
    assert entry["ru_guardrail_report"]["error_type"] == "ConnectionError"


@pytest.mark.asyncio
@pytest.mark.parametrize("upstream_error", [False, True])
async def test_stream_close_and_transport_failure_are_not_restoration_errors(upstream_error):
    g = guardrail()
    async def chunks():
        yield litellm.ModelResponse(stream=True)
        raise ConnectionError("upstream")
    data = {"stream": True}
    iterator = g.async_post_call_streaming_iterator_hook(MagicMock(), chunks(), data)
    await anext(iterator)
    if upstream_error:
        with pytest.raises(ConnectionError):
            await anext(iterator)
    else:
        await iterator.aclose()
    entry, = entries(data)
    assert entry["guardrail_action"] == ("upstream_failure" if upstream_error else "cancelled")


@pytest.mark.asyncio
async def test_broken_writer_preserves_restoration_cleanup_and_policy_refusal():
    g = guardrail(pii_mode="block")
    g.add_standard_logging_guardrail_information_to_request_data = MagicMock(side_effect=RuntimeError("sink"))
    data = {"metadata": {"pii_request_id": "server-id"}}
    g._load_mapping = AsyncMock(return_value={"<PERSON_1>": "private"})
    g._delete_mapping = AsyncMock()
    response = litellm.ModelResponse(choices=[{"index": 0, "message": {"content": "<PERSON_1>"}}])
    await g.async_post_call_success_hook(data, MagicMock(), response)
    assert response.choices[0].message.content == "private"
    g._delete_mapping.assert_awaited_once_with("server-id")
    with pytest.raises(pii_guardrail.HTTPException) as caught:
        await g.async_pre_call_hook(MagicMock(), MagicMock(), {"input": [{"type": "input_file", "file_id": "a"}]})
    assert caught.value.status_code == 422
