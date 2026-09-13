"""Lightweight contracts for the 400-user Locust profile."""

from __future__ import annotations

import json
from argparse import Namespace
from collections import Counter
import stat
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests" / "load"))

import load_support  # noqa: E402
import manage_keys  # noqa: E402
import resilience_checks  # noqa: E402
import sample_metrics  # noqa: E402
import stateful_checks  # noqa: E402
import summarize_run  # noqa: E402


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


def test_unique_input_variation_bypasses_full_text_cache_without_changing_size():
    conversation = load_support.ConversationState(
        user_index=7,
        api="chat",
        context_mode="one-shot",
        stream=False,
        sizes=(100,),
        model="mock-chat",
        input_variation="unique",
    )

    first = conversation.next_request()
    second = conversation.next_request()

    assert first.payload != second.payload
    assert _whitespace_units(first.payload["messages"]) == 100
    assert _whitespace_units(second.payload["messages"]) == 100
    assert first.expected_marker == second.expected_marker


def test_repeat_input_variation_preserves_warm_cache_workload():
    conversation = load_support.ConversationState(
        user_index=7,
        api="responses",
        context_mode="one-shot",
        stream=False,
        sizes=(10,),
        model="mock-chat",
    )

    assert conversation.next_request().payload == conversation.next_request().payload


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
            "error_code": "",
            "error_type": "",
            "retry_after_seconds": "",
            "request": "private-user@example.test",
            "response": "private response",
            "key": "sk-must-not-appear",
        }
    )
    summary = writer.close()
    serialized = "\n".join(path.read_text() for path in tmp_path.iterdir())

    assert summary["success_count"] == 1
    assert summary["successful_requests_per_second"] > 0
    assert summary["failure_rate"] == 0
    assert summary["error_kind_counts"] == {}
    assert summary["error_code_counts"] == {}
    assert summary["error_type_counts"] == {}
    assert summary["retry_after_seconds_counts"] == {}
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


def test_safe_report_aggregates_only_bounded_error_metadata(tmp_path):
    writer = load_support.SafeReportWriter(
        tmp_path,
        node="errors",
        metadata={"profile": "steady"},
    )
    writer.record(
        {
            "timestamp": 1,
            "status_code": 503,
            "total_ms": 10,
            "error_kind": "http_503",
            "error_code": "analyzer_overloaded",
            "error_type": "service_unavailable",
            "retry_after_seconds": "2",
        }
    )

    summary = writer.close()

    assert summary["error_kind_counts"] == {"http_503": 1}
    assert summary["error_code_counts"] == {"analyzer_overloaded": 1}
    assert summary["error_type_counts"] == {"service_unavailable": 1}
    assert summary["retry_after_seconds_counts"] == {"2": 1}


def test_safe_report_summarizes_affinity_without_keys_or_markers(tmp_path):
    writer = load_support.SafeReportWriter(
        tmp_path,
        node="affinity",
        metadata={"profile": "steady"},
    )
    for user_index, deployments in (
        (0, ("load-mock-a", "load-mock-a")),
        (1, ("load-mock-b", "load-mock-a")),
    ):
        for deployment_id in deployments:
            writer.record(
                {
                    "timestamp": 1,
                    "user_index": user_index,
                    "status_code": 200,
                    "total_ms": 10,
                    "deployment_id": deployment_id,
                }
            )

    summary = writer.close()

    assert summary["initial_deployment_counts"] == {
        "load-mock-a": 1,
        "load-mock-b": 1,
    }
    assert summary["deployment_transition_count"] == 1


def test_provider_capture_summary_drops_repeated_paths_and_unknown_fields():
    summary = stateful_checks.safe_provider_capture(
        {
            "deployment_id": "load-mock-a",
            "provider_requests": 3,
            "provider_request_paths": [
                "/v1/chat/completions",
                "/v1/chat/completions",
                "/v1/responses",
            ],
            "provider_saw_pii_placeholder": True,
            "provider_saw_synthetic_marker": False,
            "request_body": "private-user@example.test",
        }
    )

    assert summary == {
        "deployment_id": "load-mock-a",
        "provider_requests": 3,
        "provider_request_path_counts": {
            "/v1/chat/completions": 2,
            "/v1/responses": 1,
        },
        "provider_saw_pii_placeholder": True,
        "provider_saw_synthetic_marker": False,
    }


def test_admin_churn_checks_key_without_calling_a_model(monkeypatch):
    calls = []
    model_checks = 0

    def fake_request(_base_url, path, **kwargs):
        nonlocal model_checks
        calls.append((path, kwargs.get("payload")))
        if path == "/key/generate":
            return stateful_checks.HTTPResult(200, {}, b'{"key":"sk-temporary"}')
        if path == "/models":
            model_checks += 1
            status = 200 if model_checks == 1 else 403
            return stateful_checks.HTTPResult(status, {}, b"{}")
        return stateful_checks.HTTPResult(200, {}, b"{}")

    moments = iter((0.0, 0.0, 0.0, 6.0))
    monkeypatch.setattr(stateful_checks, "http_request", fake_request)
    monkeypatch.setattr(stateful_checks.time, "monotonic", lambda: next(moments))
    args = Namespace(
        base_url="http://proxy.invalid",
        master_key="sk-master",
        revocation_timeout_seconds=5,
        revocation_required_denials=1,
        revocation_poll_seconds=0,
        churn_interval_seconds=0,
    )
    counters = Counter()

    stateful_checks.churn_worker(args, deadline=5, counters=counters)

    assert counters == {
        "created": 1,
        "read": 1,
        "used": 1,
        "deleted": 1,
        "revoked": 1,
    }
    assert [payload for path, payload in calls if path == "/models"] == [None, None]
    assert not any(path.startswith("/v1/") for path, _payload in calls)


def test_resilience_fault_windows_include_recovery_grace():
    events = [
        {"fault_started_at": 100, "recovered_at": 110},
        {"fault_started_at": 200, "recovered_at": 215},
    ]

    assert resilience_checks.fault_windows(events, 5) == [
        (99, 115),
        (199, 220),
    ]
    assert resilience_checks.timestamp_in_windows(114, [(99, 115)])
    assert not resilience_checks.timestamp_in_windows(116, [(99, 115)])


def test_resilience_measures_application_recovery_for_every_fault():
    events = [
        {"scenario": "analyzer", "fault_started_at": 100, "recovered_at": 110},
        {"scenario": "redis", "fault_started_at": 200, "recovered_at": 210},
    ]
    samples = [
        {"timestamp": "111", "user_index": "0", "error_kind": ""},
        {"timestamp": "115", "user_index": "1", "error_kind": ""},
        {"timestamp": "211", "user_index": "0", "error_kind": ""},
        {"timestamp": "230", "user_index": "1", "error_kind": ""},
    ]

    assert resilience_checks.application_recovery(events, samples, 2, 5) == [
        {
            "scenario": "analyzer",
            "component_recovered_at": 110,
            "application_recovered_at": 115,
            "application_recovery_seconds": 5,
            "recovered_user_count": 2,
            "expected_user_count": 2,
            "fault_window": [99, 120],
        },
        {
            "scenario": "redis",
            "component_recovered_at": 210,
            "application_recovered_at": 230,
            "application_recovery_seconds": 20,
            "recovered_user_count": 2,
            "expected_user_count": 2,
            "fault_window": [199, 235],
        },
    ]


def test_resilience_reports_incomplete_recovery_before_next_fault():
    recovery = resilience_checks.application_recovery(
        [
            {"scenario": "redis", "fault_started_at": 100, "recovered_at": 110},
            {"scenario": "postgres", "fault_started_at": 200, "recovered_at": 210},
        ],
        [{"timestamp": "115", "user_index": "0", "error_kind": ""}],
        expected_users=2,
        grace_seconds=5,
    )

    assert recovery[0]["application_recovered_at"] is None
    assert recovery[0]["recovered_user_count"] == 1
    assert recovery[0]["fault_window"] == [99, 199]
    assert resilience_checks.scenario_for_timestamp(150, recovery) == "redis"
    assert resilience_checks.scenario_for_timestamp(205, recovery) == "postgres"


def test_resilience_affinity_allows_transitions_only_after_redis_fault():
    samples = [
        {
            "timestamp": "10",
            "user_index": "0",
            "deployment_id": "load-mock-a",
            "error_kind": "",
        },
        {
            "timestamp": "20",
            "user_index": "0",
            "deployment_id": "load-mock-b",
            "error_kind": "",
        },
        {
            "timestamp": "30",
            "user_index": "1",
            "deployment_id": "load-mock-a",
            "error_kind": "",
        },
        {
            "timestamp": "50",
            "user_index": "1",
            "deployment_id": "load-mock-b",
            "error_kind": "",
        },
    ]

    assert resilience_checks.affinity_transitions(samples, 40) == {
        "after_redis_fault": 1,
        "before_redis_fault": 1,
    }


def test_resilience_selects_one_replica_and_requires_redundancy(monkeypatch):
    monkeypatch.setattr(
        resilience_checks,
        "service_container_ids",
        lambda _compose_file, service: (
            ["container-a", "container-b"]
            if service == "load-litellm"
            else ["container-a"]
        ),
    )

    assert resilience_checks.select_fault_target(
        Path("compose.yml"), "litellm"
    ) == ("container-b", 2)
    with pytest.raises(RuntimeError, match="requires at least 2"):
        resilience_checks.select_fault_target(Path("compose.yml"), "analyzer")


def test_resilience_admin_probe_uses_only_key_administration(monkeypatch):
    calls = []

    def fake_request(_base_url, path, **kwargs):
        calls.append((path, kwargs.get("payload")))
        return stateful_checks.HTTPResult(200, {}, b'{"key":"sk-probe"}')

    monkeypatch.setattr(resilience_checks, "http_request", fake_request)

    result = resilience_checks.admin_round_trip(
        "http://proxy.invalid",
        "sk-master",
    )

    assert result == {"create_status": 200, "delete_status": 200}
    assert [path for path, _payload in calls] == ["/key/generate", "/key/delete"]
    assert not any(path.startswith("/v1/") for path, _payload in calls)


def test_key_state_is_atomic_and_owner_only(tmp_path):
    path = tmp_path / "keys.json"
    manage_keys.write_state(path, {"schema_version": 1, "keys": ["sk-test"]})

    assert manage_keys.read_state(path)["keys"] == ["sk-test"]
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert not list(tmp_path.glob(".keys-*"))


def test_local_calibration_keys_are_bounded_and_do_not_call_litellm(
    tmp_path, monkeypatch
):
    key_file = tmp_path / "keys.json"
    monkeypatch.setattr(
        manage_keys,
        "request_json",
        lambda *_args, **_kwargs: pytest.fail("LiteLLM must not be called"),
    )
    args = Namespace(key_file=key_file, count=3, model="mock-chat")

    manage_keys.create_local_keys(args)

    state = manage_keys.read_state(key_file)
    assert len(state["keys"]) == 3
    assert all(key.startswith("sk-load-local-") for key in state["keys"])


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


def test_resource_summary_aggregates_replicas_per_timestamp():
    samples = [
        {
            "timestamp": 1,
            "service": "load-presidio-analyzer",
            "container_id": "first",
            "cpu_percent": 80.0,
            "memory_used_bytes": 100,
            "pids": 5,
        },
        {
            "timestamp": 1,
            "service": "load-presidio-analyzer",
            "container_id": "second",
            "cpu_percent": 90.0,
            "memory_used_bytes": 120,
            "pids": 6,
        },
        {
            "timestamp": 2,
            "service": "load-presidio-analyzer",
            "container_id": "first",
            "cpu_percent": 40.0,
            "memory_used_bytes": 110,
            "pids": 5,
        },
    ]

    summary = summarize_run.resource_summary(samples)["load-presidio-analyzer"]

    assert summary == {
        "replicas_seen": 2,
        "peak_total_cpu_percent": 170.0,
        "peak_total_memory_bytes": 220,
        "peak_single_replica_memory_bytes": 120,
        "peak_pids_per_replica": 6,
    }


@pytest.mark.parametrize(
    ("value", "expected"),
    (("1KiB", 1024), ("1.5MiB", 1572864), ("2GB", 2_000_000_000)),
)
def test_metric_sampler_parses_docker_byte_units(value, expected):
    assert sample_metrics.parse_bytes(value) == expected


def test_metric_sampler_tolerates_transient_docker_placeholders():
    assert sample_metrics.integer("--") == 0
    assert sample_metrics.percentage("--") == 0.0


def test_error_metadata_keeps_only_bounded_code_and_type():
    class Response:
        @staticmethod
        def json():
            return {
                "error": {
                    "code": "analyzer_overloaded",
                    "type": "service_unavailable",
                    "message": "private-user@example.test",
                }
            }

    assert load_support.response_error_metadata(Response()) == (
        "analyzer_overloaded",
        "service_unavailable",
    )


def test_prometheus_summary_aggregates_only_allowlisted_counters(tmp_path):
    first = tmp_path / "analyzer-metrics-1.prom"
    second = tmp_path / "analyzer-metrics-2.prom"
    first.write_text(
        'ru_presidio_analyzer_requests_total{outcome="success"} 2\n'
        'unsafe_request_content{value="secret"} 1\n',
        encoding="utf-8",
    )
    second.write_text(
        'ru_presidio_analyzer_requests_total{outcome="success"} 3\n',
        encoding="utf-8",
    )

    assert summarize_run.prometheus_counters((first, second)) == {
        'ru_presidio_analyzer_requests_total{outcome="success"}': 5.0
    }
