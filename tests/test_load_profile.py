"""Lightweight contracts for the 400-user Locust profile."""

from __future__ import annotations

import json
from argparse import Namespace
import stat
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests" / "load"))

import load_support  # noqa: E402
import manage_keys  # noqa: E402


def _whitespace_units(value: object) -> int:
    if isinstance(value, str):
        return len(value.split())
    if isinstance(value, list):
        return sum(_whitespace_units(item) for item in value)
    if isinstance(value, dict):
        if isinstance(value.get("text"), str):
            return len(value["text"].split())
        if isinstance(value.get("content"), str):
            return len(value["content"].split())
        return sum(
            _whitespace_units(item)
            for item in value.values()
            if isinstance(item, (list, dict))
        )
    return 0


def test_context_sizes_are_bounded_unique_and_ordered():
    assert load_support.parse_context_sizes("1000,8000,50000,1000000") == (
        1_000,
        8_000,
        50_000,
        1_000_000,
    )
    for invalid in ("", "8000,1000", "1000,1000", "0", "1000001"):
        with pytest.raises(ValueError):
            load_support.parse_context_sizes(invalid)


def test_large_context_guard_requires_an_explicit_concurrency_override():
    with pytest.raises(ValueError, match="LOAD_ALLOW_LARGE_CONCURRENT"):
        load_support.validate_large_context_safety(
            (1_000, 256_000), 400, allow_large_concurrent=False
        )

    load_support.validate_large_context_safety(
        (1_000, 1_000_000), 400, allow_large_concurrent=True
    )


def test_full_history_grows_to_requested_context_size_for_both_apis():
    for api in ("chat", "responses"):
        conversation = load_support.ConversationState(
            user_index=7,
            api=api,
            context_mode="full-history",
            stream=False,
            sizes=(10, 25, 50),
            model="mock-chat",
        )
        requests = [conversation.next_request() for _ in range(3)]

        request_field = "messages" if api == "chat" else "input"
        assert [
            _whitespace_units(request.payload[request_field]) for request in requests
        ] == [10, 25, 50]
        assert all(request.expected_marker in json.dumps(request.payload) for request in requests)


def test_previous_response_profile_sends_only_delta_and_preserves_response_id():
    conversation = load_support.ConversationState(
        user_index=3,
        api="responses",
        context_mode="previous-response",
        stream=True,
        sizes=(10, 25),
        model="mock-chat",
    )

    first = conversation.next_request()
    conversation.update_response_id("resp-safe-identifier")
    second = conversation.next_request()

    assert "previous_response_id" not in first.payload
    assert second.payload["previous_response_id"] == "resp-safe-identifier"
    assert _whitespace_units(second.payload["input"]) == 15


def test_encrypted_state_is_forwarded_as_opaque_non_user_content():
    conversation = load_support.ConversationState(
        user_index=2,
        api="responses",
        context_mode="encrypted-state",
        stream=False,
        sizes=(10, 20),
        model="mock-chat",
    )
    conversation.next_request()
    conversation.update_response_id("resp-safe")
    request = conversation.next_request()

    reasoning = request.payload["input"][0]
    assert reasoning == {
        "type": "reasoning",
        "id": "reasoning-2-1",
        "encrypted_content": "opaque-provider-state-not-user-content",
        "summary": [],
    }


def test_key_pool_assigns_disjoint_deterministic_shards(tmp_path):
    key_file = tmp_path / "keys.json"
    key_file.write_text(
        json.dumps({"keys": [f"sk-test-{index}" for index in range(8)]}),
        encoding="utf-8",
    )

    first = load_support.KeyPool(key_file, shard_index=0, shard_count=2)
    second = load_support.KeyPool(key_file, shard_index=1, shard_count=2)
    first_values = {first.acquire()[1] for _ in range(4)}
    second_values = {second.acquire()[1] for _ in range(4)}

    assert first_values.isdisjoint(second_values)
    assert first_values | second_values == {f"sk-test-{index}" for index in range(8)}


def test_safe_report_drops_request_response_and_secret_values(tmp_path):
    writer = load_support.SafeReportWriter(
        tmp_path,
        node="test",
        metadata={"profile": "smoke", "model": "mock-chat"},
    )
    writer.record(
        {
            "timestamp": 1,
            "api": "responses",
            "context_mode": "full-history",
            "stream": "false",
            "context_tokens": 1_000,
            "status_code": 200,
            "total_ms": 12.5,
            "ttft_ms": "",
            "validation_result": "restored",
            "error_kind": "",
            "request": "private-user@example.test",
            "response": "private response",
            "key": "sk-must-not-appear",
        }
    )
    summary = writer.close()
    serialized = "\n".join(path.read_text() for path in tmp_path.iterdir())

    assert summary["success_count"] == 1
    assert summary["validation_counts"] == {"restored": 1}
    assert summary["synthetic_input_units_per_second"] > 0
    assert "private-user" not in serialized
    assert "private response" not in serialized
    assert "sk-must-not-appear" not in serialized


def test_safe_report_rejects_path_traversal_in_node_name(tmp_path):
    with pytest.raises(ValueError, match="safe filename"):
        load_support.SafeReportWriter(
            tmp_path,
            node="../outside",
            metadata={"profile": "smoke"},
        )


def test_key_state_is_atomic_and_owner_only(tmp_path):
    path = tmp_path / "keys.json"
    manage_keys.write_state(path, {"schema_version": 1, "keys": ["sk-test"]})

    assert manage_keys.read_state(path)["keys"] == ["sk-test"]
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert not list(tmp_path.glob(".keys-*"))


def test_key_cleanup_persists_only_the_not_yet_deleted_batches(tmp_path, monkeypatch):
    path = tmp_path / "keys.json"
    keys = [f"sk-test-{index}" for index in range(75)]
    manage_keys.write_state(path, {"schema_version": 1, "keys": keys})
    calls = []

    def fail_second_batch(_base_url, _path, **kwargs):
        calls.append(kwargs["payload"]["keys"])
        if len(calls) == 2:
            raise RuntimeError("bounded test failure")
        return {}

    monkeypatch.setattr(manage_keys, "request_json", fail_second_batch)
    args = Namespace(
        key_file=path,
        base_url="http://litellm.invalid",
        master_key="sk-test-master",
    )

    with pytest.raises(RuntimeError, match="bounded test failure"):
        manage_keys.delete_keys(args)

    assert manage_keys.read_state(path)["keys"] == keys[50:]
    assert calls == [keys[:50], keys[50:]]
