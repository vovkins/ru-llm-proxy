"""Safe standard logging results for the pinned LiteLLM custom guardrail."""

import asyncio
import copy
import json
import sys
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import litellm
import pytest
from litellm.litellm_core_utils.litellm_logging import StandardLoggingPayloadSetup
from litellm.proxy.spend_tracking import spend_tracking_utils

import litellm_guardrails.pii_guardrail as pii_guardrail
from litellm_guardrails.env_assignment_masking import RuEnvAssignmentGuardrail
from litellm_guardrails import guardrail_reporting
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
        {"category": "config", "rule_id": "kubeconfig_payload"},
    ]
    with patch.object(g, "_classify_pre_egress_policy_targets", return_value=findings):
        with pytest.raises(Exception):
            await g.async_pre_call_hook(MagicMock(), MagicMock(), data, call_type)
    (entry,) = entries(data)
    assert entry["guardrail_status"] == "guardrail_intervened"
    assert entry["guardrail_action"] == "pre_egress_policy_blocked"
    assert entry["violation_categories"] == ["env_secret_assignment", "kubeconfig_payload"]
    assert entry["guardrail_response"]["rule_counts"] == {
        "env_secret_assignment": 1, "kubeconfig_payload": 1,
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
async def test_interrupted_stream_records_failure_not_restoration_success():
    g = guardrail()
    data = {"stream": True, "metadata": {}}

    async def interrupted():
        raise RuntimeError("PRIVATE_UPSTREAM_ERROR")
        yield

    with pytest.raises(RuntimeError):
        _ = [item async for item in g.async_post_call_streaming_iterator_hook(
            MagicMock(), interrupted(), data,
        )]
    (entry,) = entries(data)
    assert entry["guardrail_action"] == "upstream_failure"
    assert entry["guardrail_status"] == "guardrail_failed_to_respond"
    assert entry["ru_guardrail_report"]["failure_operation"] == "upstream_read"
    assert "PRIVATE_UPSTREAM_ERROR" not in json.dumps(entry)


@pytest.mark.asyncio
async def test_cancelled_stream_records_one_non_success_card():
    g = guardrail()
    data = {"stream": True, "metadata": {}}

    async def chunks():
        yield litellm.ModelResponse(
            stream=True,
            choices=[{"index": 0, "delta": {"content": "ok"}, "finish_reason": None}],
        )

    stream = g.async_post_call_streaming_iterator_hook(MagicMock(), chunks(), data)
    await anext(stream)
    await stream.aclose()
    (entry,) = entries(data)
    assert entry["guardrail_action"] == "cancelled"
    assert entry["guardrail_status"] == "guardrail_failed_to_respond"
    assert entry["ru_guardrail_report"]["failure_operation"] == "stream_consumption"


@pytest.mark.asyncio
async def test_stream_redis_failure_does_not_emit_restored_card():
    g = guardrail()
    g._load_mapping = AsyncMock(side_effect=RuntimeError("PRIVATE_REDIS_ERROR"))
    data = {"stream": True, "metadata": {"pii_request_id": "server-id"}}

    async def chunks():
        yield litellm.ModelResponse(
            stream=True,
            choices=[{"index": 0, "delta": {"content": "ok"}, "finish_reason": "stop"}],
        )

    with pytest.raises(Exception):
        _ = [item async for item in g.async_post_call_streaming_iterator_hook(
            MagicMock(), chunks(), data,
        )]
    (entry,) = entries(data)
    assert entry["guardrail_status"] == "guardrail_failed_to_respond"
    assert entry["guardrail_action"] == "fail_closed"
    assert entry["ru_guardrail_report"]["failure_operation"] == "stream_mapping_load"
    assert "PRIVATE_REDIS_ERROR" not in json.dumps(entry)


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
async def test_nonstream_post_failure_reconciles_the_native_error_row():
    g = guardrail(failure_mode="fail_closed")
    g.guardrail_name = "ru-pii-mask-post"
    g._load_mapping = AsyncMock(side_effect=ConnectionError("private"))
    data = {"model": "mock-chat", "metadata": {"pii_request_id": "server-id"}}
    response = litellm.ModelResponse(
        choices=[{"index": 0, "message": {"role": "assistant", "content": "ok"}}],
    )
    with patch.object(guardrail_reporting, "reconcile_spend_information") as reconcile:
        with pytest.raises(pii_guardrail.ProxyException):
            await g.async_post_call_success_hook(data, MagicMock(), response)
    reconcile.assert_called_once_with(data, error=True)


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


@pytest.mark.asyncio
@pytest.mark.parametrize("bucket_name", ["metadata", "litellm_metadata"])
@pytest.mark.parametrize("copied", [False, True])
@pytest.mark.parametrize("blocked", [False, True])
async def test_multiple_pre_calls_keep_server_cards_and_discard_forged_copies(
    bucket_name, copied, blocked,
):
    first, second, post = guardrail(), guardrail(pii_mode="block" if blocked else "mask"), guardrail()
    first.guardrail_name = "first-pre"
    second.guardrail_name = "second-pre"
    post.guardrail_name = "post"
    first._analyze_text = AsyncMock(return_value=[])
    second._analyze_text = AsyncMock(return_value=[
        {"start": 0, "end": 5, "entity_type": "PERSON", "score": 1.0},
    ] if blocked else [])
    data = {
        "messages": [{"role": "user", "content": "Alice"}],
        bucket_name: {"client": "keep"},
        "litellm_logging_obj": SimpleNamespace(
            litellm_params={"metadata": {}, "litellm_metadata": {}},
            model_call_details={"standard_logging_object": {"guardrail_information": []}},
        ),
    }
    await first.async_pre_call_hook(MagicMock(), MagicMock(), data, "completion")
    # A metadata snapshot can contain both real results and caller-supplied
    # lookalikes. Only the actual server-created card should survive.
    original = entries(data)[0]
    entries(data).extend([
        {**original, "guardrail_action": "FORGED"},
        {**original, "guardrail_name": "second-pre", "guardrail_action": "FORGED"},
    ])
    if copied:
        data = copy.deepcopy(data)
    if blocked:
        with pytest.raises(litellm.UnprocessableEntityError):
            await second.async_pre_call_hook(MagicMock(), MagicMock(), data, "completion")
    else:
        await second.async_pre_call_hook(MagicMock(), MagicMock(), data, "completion")
        await post.async_post_call_success_hook(data, MagicMock(), litellm.ModelResponse())
    expected = [
        ("first-pre", "pre_call", "clean"),
        ("second-pre", "pre_call", "pii_blocked" if blocked else "clean"),
    ]
    if not blocked:
        expected.append(("post", "post_call", "skipped"))
    # Re-delivery of the first event must not replace it or create another card.
    first._write_guardrail_information(
        data, "pre_call", {"policy_result": "guardrail_error"},
        time.time(), time.perf_counter(),
    )
    cards = entries(data)
    assert [(c["guardrail_name"], c["guardrail_mode"], c["guardrail_action"]) for c in cards] == expected
    assert data[bucket_name]["client"] == "keep"
    logging_obj = data["litellm_logging_obj"]
    for bucket in logging_obj.litellm_params.values():
        assert bucket["standard_logging_guardrail_information"] == cards
    assert logging_obj.model_call_details["standard_logging_object"]["guardrail_information"] == cards
    assert "FORGED" not in json.dumps(cards)


def test_untrusted_rule_labels_and_exception_messages_are_not_serialized():
    g = guardrail()
    decision = {}
    token = pii_guardrail._STANDARD_LOGGING_DECISION.set(decision)
    try:
        g._set_logging_decision(
            "pre_egress_policy_blocked",
            rules=["env_secret_assignment", "safe_rule", "raw@example.test", "SECRET=" + "x" * 100],
            rule_counts={"env_secret_assignment": 1, "safe_rule": 1, "raw@example.test": 2},
            error_type="ValueError\nSECRET_VALUE",
        )
    finally:
        pii_guardrail._STANDARD_LOGGING_DECISION.reset(token)
    data = {"litellm_metadata": {}}
    g._write_guardrail_information(data, "pre_call", decision, time.time(), time.perf_counter())
    (entry,) = entries(data)
    assert entry["violation_categories"] == ["env_secret_assignment"]
    assert entry["guardrail_response"] == {
        "policy_result": "pre_egress_policy_blocked",
        "rules": ["env_secret_assignment"],
        "rule_counts": {"env_secret_assignment": 1},
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
        "substitutions": [{"id": "Alfa", "source": "Alfa", "replacement": "Beta", "restore": False}],
    }))
    g._analyze_text = AsyncMock(return_value=[])
    data = {"messages": [{"role": "user", "content": "Alfa Alfa"}]}
    await g.async_pre_call_hook(MagicMock(), MagicMock(), data)
    entry, = entries(data)
    assert data["messages"][0]["content"] == "Beta Beta"
    assert entry["guardrail_action"] == "dictionary_substituted"
    assert entry["ru_guardrail_report"]["dictionary_substitution_count"] == 2
    assert "Alfa" not in json.dumps(entry)
    assert "Beta" not in json.dumps(entry)
    assert entry["violation_categories"] == []


def test_logging_object_receives_cards_once_and_keeps_cached_stream_payload():
    g = guardrail()
    params = {"metadata": {}, "litellm_metadata": {}}
    logging_obj = MagicMock(litellm_params=params, model_call_details={
        "standard_logging_object": {"guardrail_information": []},
    })
    data = {"litellm_logging_obj": logging_obj, "litellm_metadata": {}}
    for _ in range(2):
        g._write_guardrail_information(
            data, "post_call", {"policy_result": "restored", "status": "success"},
            time.time(), time.perf_counter(),
        )
    assert len(entries(data)) == 1
    assert len(params["metadata"]["standard_logging_guardrail_information"]) == 1
    assert len(params["litellm_metadata"]["standard_logging_guardrail_information"]) == 1
    assert logging_obj.model_call_details["standard_logging_object"]["guardrail_information"] == entries(data)


def test_forged_post_request_card_is_replaced_once_by_server_decision():
    g = guardrail()
    forged = {
        "guardrail_name": "ru-pii-mask-pre", "guardrail_mode": "post_call",
        "guardrail_action": "clean", "guardrail_status": "success",
    }
    logging_obj = MagicMock(litellm_params={"metadata": {}, "litellm_metadata": {}})
    data = {
        "litellm_logging_obj": logging_obj,
        "litellm_metadata": {"standard_logging_guardrail_information": [forged]},
    }
    for _ in range(2):
        g._write_guardrail_information(
            data, "post_call", {"policy_result": "restored", "status": "success"},
            time.time(), time.perf_counter(),
        )
    (entry,) = entries(data)
    assert entry["guardrail_action"] == "restored"


def test_server_card_replaces_forged_logging_snapshot():
    g = guardrail()
    forged = {
        "guardrail_name": "ru-pii-mask-pre", "guardrail_mode": "post_call",
        "guardrail_action": "clean", "guardrail_status": "success",
    }
    params = {
        "metadata": {"standard_logging_guardrail_information": [dict(forged)]},
        "litellm_metadata": {"standard_logging_guardrail_information": [dict(forged)]},
    }
    logging_obj = MagicMock(litellm_params=params, model_call_details={})
    data = {"litellm_logging_obj": logging_obj, "litellm_metadata": {}}
    g._write_guardrail_information(
        data, "post_call", {"policy_result": "restored", "status": "success"},
        time.time(), time.perf_counter(),
    )
    for bucket in params.values():
        (entry,) = bucket["standard_logging_guardrail_information"]
        assert entry["guardrail_action"] == "restored"


@pytest.mark.asyncio
@pytest.mark.parametrize("bucket_name", ["metadata", "litellm_metadata"])
@pytest.mark.parametrize("supplied", ["forged", "null", "malformed", "mapping"])
async def test_inbound_cards_cannot_suppress_or_corrupt_a_block(bucket_name, supplied):
    forged = {
        "guardrail_name": "ru-pii-mask-pre", "guardrail_mode": "pre_call",
        "guardrail_action": "clean", "guardrail_status": "success",
    }
    value = {
        "forged": [forged], "null": None,
        "malformed": ["not-a-card"], "mapping": {"not": "a-list"},
    }[supplied]
    g = guardrail(pii_mode="block")
    g._analyze_text = AsyncMock(return_value=[
        {"start": 0, "end": 5, "entity_type": "PERSON", "score": 1.0},
    ])
    data = {
        "messages": [{"role": "user", "content": "Alice"}],
        bucket_name: {"standard_logging_guardrail_information": value},
    }
    with pytest.raises(litellm.UnprocessableEntityError):
        await g.async_pre_call_hook(MagicMock(), MagicMock(), data)
    (entry,) = entries(data)
    assert entry["guardrail_action"] == "pii_blocked"
    assert entry["guardrail_status"] == "guardrail_intervened"


@pytest.mark.parametrize("value", [None, {"not": "a-list"}, ["bad", None], ("bad",)])
def test_malformed_existing_cards_do_not_suppress_current_decision(value):
    g = guardrail()
    data = {"litellm_metadata": {"standard_logging_guardrail_information": value}}
    g._write_guardrail_information(
        data, "pre_call", {"policy_result": "clean", "status": "success"},
        time.time(), time.perf_counter(),
    )
    (entry,) = entries(data)
    assert entry["guardrail_action"] == "clean"


@pytest.mark.parametrize("store_prompts", [False, True])
def test_dictionary_id_is_absent_under_both_spend_redaction_modes(store_prompts):
    g = guardrail()
    decision = {}
    g._set_logging_decision(
        "dictionary_substituted", decision=decision,
        rules=["PRIVATE_VALUE"], rule_counts={"PRIVATE_VALUE": 2},
        dictionary_substitution_count=2,
    )
    data = {"litellm_metadata": {}}
    g._write_guardrail_information(data, "pre_call", decision, time.time(), time.perf_counter())
    with patch.object(spend_tracking_utils, "_should_store_prompts_and_responses_in_spend_logs",
                      return_value=store_prompts):
        (stored,) = spend_tracking_utils._sanitize_guardrail_information_for_spend_logs(entries(data))
    assert "PRIVATE_VALUE" not in json.dumps(stored)
    assert stored["ru_guardrail_report"]["dictionary_substitution_count"] == 2
    if not store_prompts:
        assert stored["guardrail_response"] == spend_tracking_utils.REDACTED_BY_LITELM_STRING


@pytest.mark.asyncio
async def test_spend_reconciliation_uses_existing_call_id_and_redacted_cards():
    g = guardrail()
    data = {"litellm_metadata": {}}
    g._write_guardrail_information(
        data, "pre_call", {"policy_result": "clean", "status": "success"},
        time.time(), time.perf_counter(),
    )
    logging_obj = MagicMock(litellm_call_id="test-call-id")
    logging_obj.model_call_details = {"standard_logging_object": {"id": "provider-response-id"}}
    data["litellm_logging_obj"] = logging_obj
    db = AsyncMock()
    db.execute_raw.return_value = 1
    with patch.dict(sys.modules, {"litellm.proxy.proxy_server": MagicMock(prisma_client=MagicMock(db=db))}), patch.object(
        spend_tracking_utils, "_should_store_prompts_and_responses_in_spend_logs",
        return_value=False,
    ):
        guardrail_reporting.reconcile_spend_information(data)
        guardrail_reporting.reconcile_spend_information(data)
        await asyncio.sleep(.01)
    db.execute_raw.assert_awaited_once()
    sql, payload, provider_id, call_id = db.execute_raw.await_args.args
    assert 'UPDATE "LiteLLM_SpendLogs"' in sql
    assert provider_id == "provider-response-id"
    assert call_id == "test-call-id"
    assert json.loads(payload)[0]["guardrail_response"] == "REDACTED_BY_LITELM"


@pytest.mark.asyncio
async def test_reconciliation_coalesces_later_post_cards_in_one_task():
    g = guardrail()
    data = {
        "litellm_metadata": {},
        # A caller-controlled lookalike must not disable internal scheduling.
        "_ru_guardrail_spend_reconciliation_started": True,
    }
    g._write_guardrail_information(
        data, "pre_call", {"policy_result": "masked", "status": "success"},
        time.time(), time.perf_counter(),
    )
    logging_obj = MagicMock(litellm_call_id="test-call-id", model_call_details={})
    data["litellm_logging_obj"] = logging_obj
    db = AsyncMock()
    first_update = asyncio.Event()
    release_first = asyncio.Event()

    async def execute_raw(*_args):
        if db.execute_raw.await_count == 1:
            first_update.set()
            await release_first.wait()
        return 1

    db.execute_raw.side_effect = execute_raw
    proxy = MagicMock(
        disable_spend_logs=False, general_settings={},
        prisma_client=MagicMock(db=db),
    )
    with patch.dict(sys.modules, {"litellm.proxy.proxy_server": proxy}), patch.object(
        spend_tracking_utils, "_should_store_prompts_and_responses_in_spend_logs",
        return_value=False,
    ):
        guardrail_reporting.reconcile_spend_information(data)
        await asyncio.wait_for(first_update.wait(), timeout=1)
        post = guardrail()
        post.guardrail_name = "second-post"
        post._write_guardrail_information(
            data, "post_call", {"policy_result": "restored", "status": "success"},
            time.time(), time.perf_counter(),
        )
        guardrail_reporting.reconcile_spend_information(data)
        release_first.set()
        for _ in range(100):
            if db.execute_raw.await_count == 2:
                break
            await asyncio.sleep(.01)
    assert db.execute_raw.await_count == 2
    payload = json.loads(db.execute_raw.await_args_list[1].args[1])
    assert {(card["guardrail_name"], card["guardrail_mode"]) for card in payload} == {
        ("ru-pii-mask-pre", "pre_call"), ("second-post", "post_call"),
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("spend_disabled,error_disabled,error", [
    (True, False, False),
    (False, True, True),
])
async def test_disabled_native_rows_do_not_start_reconciliation(
    spend_disabled, error_disabled, error,
):
    g = guardrail()
    data = {"litellm_metadata": {}}
    g._write_guardrail_information(
        data, "pre_call", {"policy_result": "clean", "status": "success"},
        time.time(), time.perf_counter(),
    )
    data["litellm_logging_obj"] = MagicMock(
        litellm_call_id="test-call-id", model_call_details={},
    )
    db = AsyncMock()
    proxy = MagicMock(
        disable_spend_logs=spend_disabled,
        general_settings={"disable_error_logs": error_disabled},
        prisma_client=MagicMock(db=db),
    )
    with patch.dict(sys.modules, {"litellm.proxy.proxy_server": proxy}), patch.object(
        guardrail_reporting.asyncio, "create_task", wraps=asyncio.create_task,
    ) as create_task:
        guardrail_reporting.reconcile_spend_information(data, error=error)
        await asyncio.sleep(0)
    create_task.assert_not_called()
    db.execute_raw.assert_not_awaited()


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
@pytest.mark.parametrize("ending", ["complete", "eof", "upstream_error", "cancelled"])
@pytest.mark.parametrize("cleanup_fails", [False, True])
async def test_stream_primary_failure_precedes_mapping_cleanup(ending, cleanup_fails):
    g = guardrail()
    g._load_mapping = AsyncMock(return_value={"<PERSON_1>": "Alice"})
    g._delete_mapping = AsyncMock(
        side_effect=RuntimeError("PRIVATE_CLEANUP_ERROR") if cleanup_fails else None,
    )
    data = {"stream": True, "metadata": {"pii_request_id": "server-id"}}
    closed = False

    async def chunks():
        nonlocal closed
        try:
            # Use the wire shape: ModelResponse can default finish_reason to stop.
            yield {"choices": [{"index": 0, "delta": {"content": "<PERSON_1>"}, "finish_reason": None}]}
            if ending == "complete":
                yield {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}
            elif ending == "upstream_error":
                raise ConnectionError("PRIVATE_UPSTREAM_ERROR")
        finally:
            closed = True

    stream = g.async_post_call_streaming_iterator_hook(MagicMock(), chunks(), data)
    first = await anext(stream)
    assert first["choices"][0]["delta"]["content"] == "Alice"
    if ending == "cancelled":
        await stream.aclose()
    elif ending == "upstream_error":
        with pytest.raises(ConnectionError):
            _ = [item async for item in stream]
    else:
        _ = [item async for item in stream]
    assert closed
    g._delete_mapping.assert_awaited_once_with("server-id")
    (entry,) = entries(data)
    expected_action, expected_operation = {
        "complete": ("fail_open", "mapping_delete") if cleanup_fails else ("restored", None),
        "eof": ("incomplete", "upstream_protocol"),
        "upstream_error": ("upstream_failure", "upstream_read"),
        "cancelled": ("cancelled", "stream_consumption"),
    }[ending]
    assert entry["guardrail_action"] == expected_action
    assert entry["guardrail_status"] == (
        "success" if expected_action == "restored" else "guardrail_failed_to_respond"
    )
    report = entry["ru_guardrail_report"]
    assert report.get("failure_operation") == expected_operation
    if ending == "upstream_error":
        assert report["error_type"] == "ConnectionError"
    assert "PRIVATE_" not in json.dumps(entry)


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
