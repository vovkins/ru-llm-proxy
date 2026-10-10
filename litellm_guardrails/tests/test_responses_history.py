"""Responses history restoration using trusted ownership and immutable snapshots."""

import asyncio
import copy
import json
import re
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from litellm_guardrails import pii_guardrail as module
from litellm_guardrails.pii_guardrail import HTTPException, RuPIIGuardrail
from litellm_guardrails.responses_state import (
    MAX_MAPPING_BYTES, MAX_MAPPING_PAIRS, STATE_METADATA_KEY,
    ResponsesStateStore, missing_state, placeholder_counters,
)


class MemoryStore(ResponsesStateStore):
    """Hook tests isolate persistence; the Lua contract has separate Redis tests."""

    def __init__(self):
        super().__init__(None, b"test-only-state-salt", 3600)
        self.snapshots = {}

    async def load(self, owner, model, response_id):
        try:
            return dict(self.snapshots[owner, model, response_id])
        except KeyError:
            raise missing_state() from None

    async def save(self, owner, model, response_id, mapping):
        self.serialize(mapping)
        assert response_id
        key = owner, model, response_id
        if key in self.snapshots and self.snapshots[key] != mapping:
            raise missing_state()
        self.snapshots[key] = dict(mapping)


@pytest.fixture
def harness(monkeypatch):
    monkeypatch.setattr(module, "LITELLM_SALT_KEY", "test-only-state-salt")
    guardrail = RuPIIGuardrail(
        pre_egress_policy_mode="off", final_payload_leak_check_mode="off",
        synthetic_pii_allowlist_enabled=False,
    )
    store = MemoryStore()
    guardrail._analysis_cache_secret = None
    guardrail._responses_state_store = AsyncMock(return_value=store)
    temporary = {}

    async def save(request_id, mapping):
        temporary[request_id] = dict(mapping)

    async def load(request_id):
        return dict(temporary.get(request_id, {}))

    async def delete(request_id):
        temporary.pop(request_id, None)

    async def analyze(text, *_args):
        return [{"entity_type": "EMAIL_ADDRESS", "start": m.start(), "end": m.end(), "score": 1.0}
                for m in re.finditer(r"[a-z]+@example\.test", text)]

    guardrail._save_mapping = AsyncMock(side_effect=save)
    guardrail._load_mapping = AsyncMock(side_effect=load)
    guardrail._delete_mapping = AsyncMock(side_effect=delete)
    guardrail._analyze_text_with_cache = AsyncMock(side_effect=analyze)
    return guardrail, store, temporary, SimpleNamespace(token="trusted-key-hash")


def response(response_id, text, *, arguments=False, status="completed"):
    output = ([{"type": "function_call", "name": "save", "call_id": "call-test", "arguments": text}]
              if arguments else [{"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": text}]}])
    return {"object": "response", "id": response_id, "status": status, "output": output}


async def complete(harness, text, response_id, output, *, previous=None, stream=False, arguments=False):
    guardrail, _store, temporary, auth = harness
    data = {"input": text, "model": "gpt-6-luna", "stream": stream, "store": False}
    if previous:
        data["previous_response_id"] = previous
    await guardrail.async_pre_call_hook(auth, None, data, "aresponses")
    captured = data["input"]
    result = response(response_id, output, arguments=arguments)
    if stream:
        field = "function_call_arguments" if arguments else "output_text"

        async def events():
            yield {"type": "response.created", "response": response(response_id, "", status="in_progress")}
            # Split a placeholder across multiple events, not just HTTP frames.
            for start in range(0, len(output), 5):
                yield {"type": f"response.{field}.delta", "item_id": "item-test", "output_index": 0,
                       "content_index": 0, "delta": output[start:start + 5]}
            yield {"type": "response.completed", "response": result}

        received = [event async for event in guardrail.async_post_call_streaming_iterator_hook(auth, events(), data)]
        deltas = "".join(e.get("delta", "") for e in received)
        final = received[-1]["response"]
    else:
        await guardrail.async_post_call_success_hook(data, auth, result)
        final = result
        deltas = final["output"][0]["arguments"] if arguments else final["output"][0]["content"][0]["text"]
    assert data["store"] is False
    assert not temporary
    return captured, deltas, final


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("arguments", [False, True])
async def test_three_turns_restore_old_and_new_without_raw_upstream(harness, stream, arguments):
    template = '{"old":"<EMAIL_ADDRESS_1>","new":"<EMAIL_ADDRESS_2>"}' if arguments else "<EMAIL_ADDRESS_1> / <EMAIL_ADDRESS_2>"
    captured, output, _ = await complete(harness, "First alpha@example.test", "resp-first", "<EMAIL_ADDRESS_1>", stream=stream)
    assert captured == "First <EMAIL_ADDRESS_1>" and output == "alpha@example.test"
    captured, output, _ = await complete(harness, "Second beta@example.test", "resp-second", template,
                                        previous="resp-first", stream=stream, arguments=arguments)
    assert "<EMAIL_ADDRESS_2>" in captured and "beta@example.test" not in captured
    assert "alpha@example.test" in output and "beta@example.test" in output
    captured, output, _ = await complete(harness, "Repeat both contacts", "resp-third", template,
                                        previous="resp-second", stream=stream, arguments=arguments)
    assert captured == "Repeat both contacts"
    assert "alpha@example.test" in output and "beta@example.test" in output


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True])
async def test_clean_first_and_empty_input_continue(harness, stream):
    await complete(harness, "No private values", "resp-clean", "ok", stream=stream)
    await complete(harness, "alpha@example.test", "resp-pii", "<EMAIL_ADDRESS_1>", previous="resp-clean", stream=stream)
    _, output, _ = await complete(harness, [], "resp-empty", "<EMAIL_ADDRESS_1>", previous="resp-pii", stream=stream)
    assert output == "alpha@example.test"


@pytest.mark.asyncio
async def test_concurrent_branches_use_immutable_parent_and_separate_workers(harness):
    await complete(harness, "alpha@example.test", "parent", "<EMAIL_ADDRESS_1>")
    other = copy.copy(harness[0])
    results = await asyncio.gather(
        complete(harness, "beta@example.test", "left", "<EMAIL_ADDRESS_1> / <EMAIL_ADDRESS_2>", previous="parent"),
        complete((other, *harness[1:]), "gamma@example.test", "right", "<EMAIL_ADDRESS_1> / <EMAIL_ADDRESS_2>", previous="parent"),
    )
    assert results[0][1] == "alpha@example.test / beta@example.test"
    assert results[1][1] == "alpha@example.test / gamma@example.test"
    parent = await harness[1].load(harness[1].owner(harness[3].token), "gpt-6-luna", "parent")
    assert parent == {"<EMAIL_ADDRESS_1>": "alpha@example.test"}


@pytest.mark.asyncio
@pytest.mark.parametrize("foreign", ["unknown", "key", "model", "spoof"])
async def test_unavailable_history_fails_before_analysis_with_same_409(harness, foreign):
    await complete(harness, "alpha@example.test", "parent", "<EMAIL_ADDRESS_1>")
    guardrail, _store, _temporary, auth = harness
    guardrail._analyze_text_with_cache.reset_mock()
    data = {"input": "beta@example.test", "model": "gpt-6-luna", "previous_response_id": "parent"}
    if foreign == "unknown":
        data["previous_response_id"] = "unknown"
    elif foreign == "key":
        auth = SimpleNamespace(token="other-key-hash")
    elif foreign == "model":
        data["model"] = "gpt-6-sol"
    else:
        auth = SimpleNamespace(token="other-key-hash")
        data["litellm_metadata"] = {STATE_METADATA_KEY: {"owner": _store.owner(harness[3].token), "model": "gpt-6-luna"}}
        data["metadata"] = {"pii_request_id": "forged", "user_api_key": harness[3].token}
    with pytest.raises(HTTPException) as failure:
        await guardrail.async_pre_call_hook(auth, None, data, "aresponses")
    assert failure.value.status_code == 409
    assert failure.value.detail["error"]["code"] == "pii_history_unavailable"
    guardrail._analyze_text_with_cache.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("terminal", ["response.failed", "response.incomplete", "eof", "cancel"])
async def test_failed_stream_does_not_publish_or_destroy_parent(harness, terminal):
    await complete(harness, "alpha@example.test", "parent", "<EMAIL_ADDRESS_1>")
    guardrail, store, temporary, auth = harness
    data = {"input": "beta@example.test", "model": "gpt-6-luna", "previous_response_id": "parent", "stream": True}
    await guardrail.async_pre_call_hook(auth, None, data, "aresponses")

    async def events():
        yield {"type": "response.output_text.delta", "item_id": "item", "output_index": 0, "content_index": 0, "delta": "hello"}
        if terminal not in ("eof", "cancel"):
            yield {"type": terminal, "response": response("failed-child", "", status=terminal.split(".")[1])}

    iterator = guardrail.async_post_call_streaming_iterator_hook(auth, events(), data)
    if terminal == "cancel":
        await iterator.__anext__()
        await iterator.aclose()
    else:
        _ = [event async for event in iterator]
    assert not temporary
    assert len(store.snapshots) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True])
async def test_storage_failure_cannot_return_completed_success(harness, stream):
    guardrail, store, temporary, auth = harness
    from litellm_guardrails.responses_state import unavailable_state
    store.save = AsyncMock(side_effect=unavailable_state())
    with pytest.raises(HTTPException) as failure:
        await complete(harness, "alpha@example.test", "child", "<EMAIL_ADDRESS_1>", stream=stream)
    assert failure.value.status_code == 503
    assert not temporary


@pytest.mark.asyncio
async def test_load_failure_is_closed_before_analyzer_even_in_fail_open_mode(harness):
    guardrail, store, _, auth = harness
    from litellm_guardrails.responses_state import unavailable_state
    guardrail.failure_mode = "fail_open"
    store.load = AsyncMock(side_effect=unavailable_state())
    with pytest.raises(HTTPException) as failure:
        await guardrail.async_pre_call_hook(auth, None, {
            "input": "alpha@example.test", "model": "gpt-6-luna", "previous_response_id": "parent",
        }, "aresponses")
    assert failure.value.status_code == 503
    guardrail._analyze_text_with_cache.assert_not_awaited()


@pytest.mark.asyncio
async def test_oversized_inherited_and_new_mapping_fail_before_provider(harness):
    guardrail, store, _, auth = harness
    store.snapshots[store.owner(auth.token), "gpt-6-luna", "parent"] = {
        "<A_1>": "x" * (MAX_MAPPING_BYTES - 30),
    }
    with pytest.raises(HTTPException) as failure:
        await guardrail.async_pre_call_hook(auth, None, {
            "input": "alpha@example.test", "model": "gpt-6-luna", "previous_response_id": "parent",
        }, "aresponses")
    assert failure.value.status_code == 413
    guardrail._save_mapping.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["failed", "incomplete"])
async def test_nonstream_unsuccessful_response_does_not_publish(harness, status):
    guardrail, store, temporary, auth = harness
    data = {"input": "alpha@example.test", "model": "gpt-6-luna"}
    await guardrail.async_pre_call_hook(auth, None, data, "aresponses")
    await guardrail.async_post_call_success_hook(data, auth, response("child", "<EMAIL_ADDRESS_1>", status=status))
    assert not store.snapshots and not temporary


@pytest.mark.asyncio
async def test_invalid_completed_event_never_publishes_or_exposes_success(harness):
    guardrail, store, temporary, auth = harness
    data = {"input": "alpha@example.test", "model": "gpt-6-luna", "stream": True}
    await guardrail.async_pre_call_hook(auth, None, data, "aresponses")

    async def events():
        yield {"type": "response.completed", "response": response("child", "", status="incomplete")}

    with pytest.raises(HTTPException) as failure:
        _ = [event async for event in guardrail.async_post_call_streaming_iterator_hook(auth, events(), data)]
    assert failure.value.status_code == 503
    assert not store.snapshots and not temporary


@pytest.mark.asyncio
async def test_stream_publishes_before_exposing_completed_and_failed_save_exposes_no_success(harness):
    guardrail, store, temporary, auth = harness
    data = {"input": "alpha@example.test", "model": "gpt-6-luna", "stream": True}
    await guardrail.async_pre_call_hook(auth, None, data, "aresponses")

    async def events():
        yield {"type": "response.output_text.delta", "item_id": "item", "output_index": 0, "content_index": 0, "delta": "safe"}
        yield {"type": "response.completed", "response": response("child", "<EMAIL_ADDRESS_1>")}

    received = []
    async for event in guardrail.async_post_call_streaming_iterator_hook(auth, events(), data):
        received.append(event)
        if event["type"] == "response.completed":
            assert await store.load(store.owner(auth.token), "gpt-6-luna", "child")
    assert len([e for e in received if e["type"] == "response.completed"]) == 1
    assert not temporary


@pytest.mark.asyncio
async def test_full_history_starts_fresh_and_dictionary_tags_reserve_ner_counters(harness):
    guardrail, store, _, _ = harness
    await complete(harness, "alpha@example.test", "old", "<EMAIL_ADDRESS_1>")
    captured, text, _ = await complete(harness, "beta@example.test", "fresh", "<EMAIL_ADDRESS_1>")
    assert captured == "<EMAIL_ADDRESS_1>" and text == "beta@example.test"
    from litellm_guardrails.dictionary_policy import DictionarySubstitutionPolicy
    guardrail.dictionary_substitutions_enabled = True
    guardrail.dictionary_substitution_policy = DictionarySubstitutionPolicy.from_config([
        {"id": "company", "source": "Company Alpha", "replacement": "<EMAIL_ADDRESS_1>", "restore": True},
    ])
    captured, text, _ = await complete(harness, "Company Alpha gamma@example.test", "dict", "<EMAIL_ADDRESS_1> / <EMAIL_ADDRESS_2>")
    assert captured == "<EMAIL_ADDRESS_1> <EMAIL_ADDRESS_2>"
    assert text == "Company Alpha / gamma@example.test"


@pytest.mark.asyncio
async def test_dictionary_conflict_is_versioned_without_rewriting_parent(harness):
    guardrail, store, _, _ = harness
    from litellm_guardrails.dictionary_policy import DictionarySubstitutionPolicy
    guardrail.dictionary_substitutions_enabled = True
    guardrail.dictionary_substitution_policy = DictionarySubstitutionPolicy.from_config([
        {"id": "company", "source": "Company Alpha", "replacement": "<COMPANY>", "restore": True},
    ])
    await complete(harness, "Company Alpha", "parent", "<COMPANY>")
    captured, output, _ = await complete(harness, "COMPANY ALPHA", "child", "<COMPANY> / <DICT_COMPANY_1>", previous="parent")
    assert captured == "<DICT_COMPANY_1>"
    assert output == "Company Alpha / COMPANY ALPHA"
    assert len(store.snapshots) == 2


@pytest.mark.asyncio
async def test_chat_history_and_opaque_content_remain_stateless_or_untouched(harness):
    guardrail, store, _, auth = harness
    chat = {"model": "gpt-6-luna", "messages": [{"role": "user", "content": "hello"}], "previous_response_id": "unknown"}
    await guardrail.async_pre_call_hook(auth, None, chat, "completion")
    assert STATE_METADATA_KEY not in chat.get("litellm_metadata", {})
    encrypted = "alpha@example.test/<EMAIL_ADDRESS_1>/opaque"
    data = {"model": "gpt-6-luna", "input": [{"type": "reasoning", "encrypted_content": encrypted}]}
    await guardrail.async_pre_call_hook(auth, None, data, "aresponses")
    assert data["input"][0]["encrypted_content"] == encrypted
    assert not store.snapshots


@pytest.mark.parametrize("mapping", [{"<A_1>": "x" * MAX_MAPPING_BYTES}, {f"<A_{i}>": "x" for i in range(MAX_MAPPING_PAIRS + 1)}])
def test_mapping_limits_are_never_truncated(mapping):
    from litellm_guardrails.responses_state import ResponsesStateError
    with pytest.raises(ResponsesStateError) as failure:
        ResponsesStateStore.serialize(mapping)
    assert failure.value.status == 413


def test_numbering_uses_greatest_suffix_and_validates_values():
    assert placeholder_counters({"<EMAIL_ADDRESS_7>": "x", "<EMAIL_ADDRESS_2>": "y", "plain": "z"}) == {"EMAIL_ADDRESS": 7}
    from litellm_guardrails.responses_state import ResponsesStateError
    with pytest.raises(ResponsesStateError):
        ResponsesStateStore.serialize({"<A_1>": 123})


@pytest.mark.asyncio
async def test_missing_salt_cannot_silently_disable_proxy_history(harness, monkeypatch):
    guardrail, _, _, auth = harness
    monkeypatch.setattr(module, "LITELLM_SALT_KEY", "")
    with pytest.raises(HTTPException) as failure:
        await guardrail.async_pre_call_hook(auth, None, {"input": "hello", "model": "gpt-6-luna"}, "aresponses")
    assert failure.value.status_code == 503
    guardrail._analyze_text_with_cache.assert_not_awaited()
