"""Lightweight contract tests for the manual long-context benchmark."""

import json
import sys
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import benchmark_long_context as benchmark  # noqa: E402


def test_synthetic_segment_has_exact_size_and_safe_canary_positions():
    text, canary_count = benchmark.synthetic_segment(
        1_000,
        seed=3,
        include_canaries=True,
        window_stride=320,
    )
    tokens = text.split()

    assert len(tokens) == 1_000
    assert canary_count == 4
    assert [
        index
        for index, value in enumerate(tokens)
        if value == benchmark.SYNTHETIC_PII_TOKEN
    ] == [0, 320, 500, 999]


def test_growing_history_preserves_previous_segments():
    payloads = benchmark.build_payloads(
        (10, 25, 50),
        profile="growing-history",
        api="responses",
        include_canaries=True,
        window_stride=4,
    )

    assert payloads[1]["request_value"][0] == payloads[0]["request_value"][0]
    assert payloads[2]["request_value"][:2] == payloads[1]["request_value"]
    for expected_size, payload in zip((10, 25, 50), payloads):
        analyzer_text = benchmark._analyzer_text(payload["request_value"])
        assert len(analyzer_text.split()) == expected_size
        assert len(analyzer_text) == payload["input_character_count"]


def test_growing_history_uses_unique_equal_sized_segments():
    payloads = benchmark.build_payloads(
        (1_000, 2_000, 3_000, 4_000),
        profile="growing-history",
        api="chat",
        include_canaries=True,
        window_stride=320,
    )

    segments = [payload["request_value"][-1]["content"] for payload in payloads]

    assert len(set(segments)) == len(segments)
    assert all(len(segment.split()) == 1_000 for segment in segments)


def test_repeated_field_profile_builds_large_cached_history_without_raw_report_data():
    payload = benchmark.build_payloads(
        (10_000,),
        profile="repeated-field",
        api="chat",
        include_canaries=True,
        window_stride=320,
        repeated_field_tokens=1_000,
    )[0]

    fields = payload["request_value"]
    assert payload["request_field_count"] == 10
    assert len(fields) == 10
    assert len({field["content"] for field in fields}) == 1
    assert len(benchmark._analyzer_text(fields).split()) == 10_000


def test_repeated_field_profile_requires_exact_field_boundaries():
    with pytest.raises(ValueError, match="must be divisible"):
        benchmark.build_payloads(
            (1_001,),
            profile="repeated-field",
            api="responses",
            include_canaries=False,
            window_stride=320,
            repeated_field_tokens=1_000,
        )


def test_report_schema_never_contains_generated_request_data():
    parser = benchmark.build_parser()
    args = parser.parse_args(
        [
            "--layer",
            "analyzer",
            "--url",
            "http://127.0.0.1:5001",
            "--profile",
            "one-shot",
            "--sizes",
            "1000,8000",
            "--dry-run",
            "--metadata",
            "cpu_profile=local",
        ]
    )

    report = benchmark.run(args)
    serialized = json.dumps(report, ensure_ascii=False)

    assert report["schema_version"] == 1
    assert report["parameters"]["sizes"] == [1_000, 8_000]
    assert report["parameters"]["generator_unit"] == "whitespace_token"
    assert report["parameters"]["concurrency"] == 1
    assert report["parameters"]["load_shape"] == "sequential"
    assert report["parameters"]["unique_requests"] is False
    assert report["environment"] == {"cpu_profile": "local"}
    assert report["summary"]["generated_count"] == 2
    assert sorted(report["summary"]["by_requested_token_count"]) == ["1000", "8000"]
    assert benchmark.SYNTHETIC_PII_TOKEN not in serialized
    assert "request_value" not in serialized


def test_unique_request_suffix_copies_generated_payload_and_bypasses_cache_key():
    source = [{"role": "user", "content": "synthetic payload"}]

    first = benchmark._with_unique_request_suffix(source, 0)
    second = benchmark._with_unique_request_suffix(source, 1)

    assert first != second
    assert first[0]["content"].endswith("load-sample-000001")
    assert second[0]["content"].endswith("load-sample-000002")
    assert source == [{"role": "user", "content": "synthetic payload"}]


def test_burst_load_series_honors_concurrency_limit():
    lock = threading.Lock()
    active = 0
    observed_peak = 0

    def measure():
        nonlocal active, observed_peak
        with lock:
            active += 1
            observed_peak = max(observed_peak, active)
        time.sleep(0.02)
        with lock:
            active -= 1
        return {
            "status": "success",
            "total_seconds": 0.02,
            "ttft_seconds": None,
        }

    results = benchmark.run_load_series(
        measure,
        repetitions=4,
        concurrency=2,
        request_rate=None,
    )

    assert [result["request_index"] for result in results] == [1, 2, 3, 4]
    assert observed_peak == 2
    assert benchmark._max_observed_concurrency(results) == 2
    assert all(result["completed_offset_seconds"] > 0 for result in results)


def test_constant_rate_load_series_records_schedule_and_client_delay():
    results = benchmark.run_load_series(
        lambda: {
            "status": "success",
            "total_seconds": 0.0,
            "ttft_seconds": None,
        },
        repetitions=3,
        concurrency=3,
        request_rate=20.0,
    )

    assert [result["scheduled_offset_seconds"] for result in results] == [
        0.0,
        0.05,
        0.1,
    ]
    assert all(result["client_start_delay_seconds"] >= 0 for result in results)
    assert results[-1]["started_offset_seconds"] >= 0.09


def test_summary_reports_throughput_and_observed_concurrency():
    summary = benchmark._summarize_measurements(
        [
            {
                "status": "success",
                "total_seconds": 1.0,
                "ttft_seconds": None,
                "client_start_delay_seconds": 0.0,
                "started_offset_seconds": 0.0,
                "completed_offset_seconds": 1.0,
            },
            {
                "status": "http_error",
                "http_status": 503,
                "failure_reason": "queue_timeout",
                "total_seconds": 1.0,
                "ttft_seconds": None,
                "client_start_delay_seconds": 0.0,
                "started_offset_seconds": 0.0,
                "completed_offset_seconds": 1.0,
            },
        ]
    )

    assert summary["completed_rps"] == 2.0
    assert summary["successful_rps"] == 1.0
    assert summary["max_observed_concurrency"] == 2
    assert summary["http_status_counts"] == {"503": 1}
    assert summary["failure_reason_counts"] == {"queue_timeout": 1}


def test_real_provider_layer_requires_explicit_confirmation():
    parser = benchmark.build_parser()
    args = parser.parse_args(
        [
            "--layer",
            "codex-lb",
            "--url",
            "http://127.0.0.1:4000",
            "--profile",
            "one-shot",
            "--model",
            "gpt-test",
            "--dry-run",
        ]
    )

    with pytest.raises(ValueError, match="allow-real-provider"):
        benchmark.run(args)


def test_report_metadata_rejects_secret_like_keys():
    with pytest.raises(ValueError, match="sensitive metadata key"):
        benchmark.parse_metadata(["api_key=must-not-be-recorded"])


def test_summary_keeps_latency_percentiles_separate_by_size():
    results = [
        {"requested_token_count": 1_000, "status": "success", "total_seconds": 1},
        {"requested_token_count": 1_000, "status": "success", "total_seconds": 2},
        {"requested_token_count": 8_000, "status": "success", "total_seconds": 8},
    ]

    summary = benchmark.summarize(results)

    assert "latency_seconds" not in summary
    assert summary["by_requested_token_count"]["1000"]["latency_seconds"]["p50"] == 1.0
    assert summary["by_requested_token_count"]["8000"]["latency_seconds"]["p50"] == 8.0


def test_http_error_fields_keep_only_safe_identifiers():
    response = MagicMock(status_code=500)
    response.json.return_value = {
        "error": {
            "type": "guardrail_error",
            "code": "500",
            "message": "request and secret must never be stored",
        }
    }

    assert benchmark._safe_http_error_fields(response) == {
        "error_type": "guardrail_error",
        "error_code": "500",
    }
    assert benchmark._safe_error_fields_from_payload(response.json()) == {
        "error_type": "guardrail_error",
        "error_code": "500",
    }


def test_overload_report_keeps_only_bounded_reason_and_retry_after():
    response = MagicMock(status_code=503)
    response.headers = {"retry-after": "2"}
    response.json.return_value = {
        "detail": {
            "code": "analyzer_overloaded",
            "reason": "queue_timeout",
            "message": "raw request must not be copied",
        }
    }

    assert benchmark._safe_http_error_fields(response) == {
        "error_code": "analyzer_overloaded",
        "failure_reason": "queue_timeout",
    }
    assert benchmark._safe_retry_after_seconds(response) == 2

    response.headers = {"retry-after": "unsafe-value"}
    assert benchmark._safe_retry_after_seconds(response) is None


def test_overload_report_finds_reason_in_litellm_proxy_shape():
    payload = {
        "error": {
            "message": "must not be copied",
            "type": "analyzer_overloaded",
            "param": {
                "analyzer_overload": {
                    "code": "analyzer_overloaded",
                    "details": {
                        "reason": "queue_full",
                        "retry_after_seconds": 2,
                    },
                }
            },
            "code": "503",
        }
    }

    assert benchmark._safe_error_fields_from_payload(payload) == {
        "error_type": "analyzer_overloaded",
        "error_code": "503",
        "failure_reason": "queue_full",
    }


def test_docker_resource_snapshot_parses_bounded_metrics(monkeypatch):
    calls = []

    def run(command, **_kwargs):
        calls.append(command)
        if command[1] == "stats":
            return benchmark.subprocess.CompletedProcess(
                command,
                0,
                stdout=(
                    '{"Name":"presidio-analyzer","CPUPerc":"123.45%",'
                    '"MemUsage":"1.25GiB / 7.75GiB"}\n'
                ),
                stderr="",
            )
        return benchmark.subprocess.CompletedProcess(
            command,
            0,
            stdout="1500000000\n",
            stderr="",
        )

    monkeypatch.setattr(benchmark.subprocess, "run", run)

    snapshots = benchmark.docker_resource_snapshot(("presidio-analyzer",))

    assert snapshots == [
        {
            "container": "presidio-analyzer",
            "cpu_percent": 123.45,
            "memory_working_set_bytes": 1_342_177_280,
            "process_rss_bytes": 1_500_000_000,
        }
    ]
    assert calls[0][:3] == ["docker", "stats", "--no-stream"]
    assert calls[1][:3] == ["docker", "exec", "presidio-analyzer"]


def test_resource_sampler_reports_peaks_without_raw_samples():
    sampler = benchmark.DockerResourceSampler(("analyzer",), 1.0)
    sampler._samples["analyzer"] = [
        {
            "container": "analyzer",
            "cpu_percent": 50.0,
            "memory_working_set_bytes": 100,
            "process_rss_bytes": 150,
            "sampled_at_seconds": 0.5,
        },
        {
            "container": "analyzer",
            "cpu_percent": 125.5,
            "memory_working_set_bytes": 120,
            "process_rss_bytes": 175,
            "sampled_at_seconds": 1.5,
        },
    ]

    assert sampler.report() == {
        "containers": {
            "analyzer": {
                "sample_count": 2,
                "peak_cpu_percent": 125.5,
                "peak_cpu_at_seconds": 1.5,
                "peak_memory_working_set_bytes": 120,
                "peak_memory_working_set_at_seconds": 1.5,
                "peak_process_rss_bytes": 175,
                "peak_process_rss_at_seconds": 1.5,
            }
        },
        "sampling_errors": {},
    }


def test_resource_container_names_are_strictly_validated():
    assert benchmark.parse_container_names(
        ["presidio-analyzer", "presidio-analyzer", "ru_llm.proxy"]
    ) == ("presidio-analyzer", "ru_llm.proxy")
    with pytest.raises(ValueError, match="invalid Docker container name"):
        benchmark.parse_container_names(["$(unsafe)"])


def test_sizes_are_bounded_to_one_million_tokens():
    assert benchmark.parse_sizes("50000,1000,8000,8000") == (1_000, 8_000, 50_000)
    with pytest.raises(Exception, match="safety limit"):
        benchmark.parse_sizes("1000001")


def test_ttft_recognizes_only_non_empty_output_deltas():
    assert benchmark._is_output_delta("data: [DONE]") is False
    assert benchmark._is_output_delta(
        'data: {"type":"response.created","response":{}}'
    ) is False
    assert benchmark._is_output_delta(
        'data: {"type":"response.output_text.delta","delta":"ok"}'
    ) is True
    assert benchmark._is_output_delta(
        'data: {"choices":[{"delta":{"content":"ok"}}]}'
    ) is True
