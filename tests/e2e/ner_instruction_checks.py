"""Check real Analyzer decisions and synthetic echo flows in the NER gate."""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from itertools import combinations
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from presidio.evaluation.corpus import DEFAULT_CORPUS_PATH, load_corpus
from presidio.evaluation.metrics import evaluate_predictions
from presidio.evaluation.tiny2_comparison import coverage
from presidio.evaluation.run_baseline import AnalyzerClient
from tests.e2e.ner_model_limits import (
    accepted_coverage_cases,
    check_case_result,
    check_login_filter,
)
from tests.e2e.ner_test_support import run_checks, write_report

CORPUS = DEFAULT_CORPUS_PATH.with_name("instruction_regressions.jsonl")
NAME_CORPUS = DEFAULT_CORPUS_PATH.with_name("person_organization_regressions.jsonl")
CONTEXT_CORPUS = DEFAULT_CORPUS_PATH.with_name("name_context_regressions.jsonl")
FIELD_HOLDOUT_CORPUS = DEFAULT_CORPUS_PATH.with_name("name_field_holdout.jsonl")
NAME_TYPES = frozenset({"PERSON", "ORGANIZATION"})


def _name_cases():
    return load_corpus(NAME_CORPUS, required_entity_types=NAME_TYPES)


def _context_cases():
    return load_corpus(CONTEXT_CORPUS, required_entity_types=NAME_TYPES)


def _field_holdout_cases():
    return load_corpus(FIELD_HOLDOUT_CORPUS, required_entity_types=NAME_TYPES)


def _canary_values(cases):
    negatives = [case.text for case in cases if not case.expected]
    # Paired service IDs and contract numbers intentionally share values;
    # global canaries must not reject a legitimate negative example.
    return sorted(
        {
            value
            for case in cases
            for entity in case.expected
            if (value := case.text[entity.start : entity.end])
            and not any(value in text for text in negatives)
        }
    )


def _request(base_url, path, payload, *, stream=False, timeout=60):
    request = urllib.request.Request(
        base_url + path,
        data=json.dumps(payload, ensure_ascii=False).encode(),
        headers={
            "Authorization": "Bearer sk-test-master",
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        if not stream:
            return json.load(response)
        assert response.headers.get_content_type() == "text/event-stream"
        return [
            json.loads(line[5:].strip()) if line[5:].strip() != "[DONE]" else "[DONE]"
            for raw in response
            if (line := raw.decode().strip()).startswith("data:")
        ]


def _restored_text(response, api, stream):
    if not stream:
        if api == "chat/completions":
            return response["choices"][0]["message"]["content"]
        return "".join(
            content["text"]
            for item in response["output"]
            if item["type"] == "message"
            for content in item["content"]
            if content["type"] == "output_text"
        )
    events = [event for event in response if isinstance(event, dict)]
    if api == "chat/completions":
        choices = [choice for event in events for choice in event.get("choices", [])]
        assert "[DONE]" in response
        assert any(choice.get("finish_reason") == "stop" for choice in choices)
        return "".join(
            choice.get("delta", {}).get("content") or "" for choice in choices
        )
    assert any(event.get("type") == "response.completed" for event in events)
    return "".join(
        event["delta"]
        for event in events
        if event.get("type") == "response.output_text.delta"
    )


def check_entity_filters(analyzer_url):
    examples = {
        "PERSON": ("Клиент Иван Петров подписал документ.", "Иван Петров"),
        "ORGANIZATION": ("Документы подготовлены для ООО Вектор.", "ООО Вектор"),
        "CONTRACT_NUMBER": (
            "Госконтракт № 0173100004521000123.",
            "0173100004521000123",
        ),
    }
    cases = []
    for count in range(1, len(examples) + 1):
        for entity_types in combinations(examples, count):
            cases.append(
                (
                    "-".join(entity_types),
                    " ".join(examples[entity][0] for entity in entity_types),
                    {entity: examples[entity][1] for entity in entity_types},
                )
            )
    cases.extend(
        [
            (
                "mixed-regex-bert",
                " ".join(text for text, _value in examples.values())
                + " КПП получателя: 770801001.",
                {
                    **{entity: value for entity, (_text, value) in examples.items()},
                    "RU_KPP": "770801001",
                },
            ),
            ("regex-only", "КПП получателя: 770801001.", {"RU_KPP": "770801001"}),
            ("location", "Встреча состоится в Москве.", {"LOCATION": "Москве"}),
        ]
    )
    for case_id, text, values in cases:
        response = _request(
            analyzer_url,
            "/api/v1/analyze",
            {"text": text, "entities": list(values), "score_threshold": 0.35},
        )
        results = response["entities"]
        assert response["text"] == text, f"{case_id}: original text changed"
        assert {result["entity_type"] for result in results} <= set(
            values
        ), f"{case_id}: unrequested type returned"
        for entity_type, value in values.items():
            start = text.index(value)
            assert any(
                result["entity_type"] == entity_type
                and result["start"] <= start
                and result["end"] >= start + len(value)
                for result in results
            ), f"{case_id}: filtered sensitive value not fully covered"
    for entity_type in examples:
        response = _request(
            analyzer_url,
            "/api/v1/analyze",
            {"text": "Верни ровно MODEL_OK.", "entities": [entity_type]},
        )
        assert response["entities"] == [], f"{entity_type}: unexpected detection"
    return len(cases) + len(examples)


def check_instructions(analyzer_url, proxy_url, block_proxy_url, capture_url):
    entity_filter_cases = check_entity_filters(analyzer_url)
    cases = load_corpus(CORPUS)
    client = AnalyzerClient(analyzer_url, timeout_seconds=60)
    predictions = {case.case_id: client.analyze(case) for case in cases}
    metrics = evaluate_predictions(cases, predictions)
    verdicts = {
        case.case_id: check_case_result(case, predictions[case.case_id], "instruction_regressions")
        for case in cases
    }
    observed_limitations = [key for key, value in verdicts.items()
                            if value["assessment"] == "accepted_limitation"]
    for case in cases:
        results = predictions[case.case_id]
        if "instruction_login_context" in case.tags:
            filtered = _request(
                analyzer_url, "/api/v1/analyze",
                {"text": case.text, "entities": ["LOGIN"], "score_threshold": 0.35},
            )["entities"]
            check_login_filter(case, results, filtered)
    assert metrics["aggregate"]["critical_coverage_recall"] == 1.0

    flows = 0
    login_block_flows = 0
    for case in cases:
        for api in ("chat/completions", "responses"):
            for stream in (False, True):
                _request(capture_url, "/capture/reset", {})
                payload = {
                    "model": (
                        "mock-chat" if api == "chat/completions" else "mock-responses"
                    ),
                    "stream": stream,
                }
                if api == "chat/completions":
                    payload["messages"] = [{"role": "user", "content": case.text}]
                else:
                    payload["input"] = case.text
                response = _request(proxy_url, "/v1/" + api, payload, stream=stream)
                assert (
                    _restored_text(response, api, stream) == case.text
                ), f"{case.case_id}: {api} stream={stream}: echo changed"
                with urllib.request.urlopen(
                    capture_url + "/capture", timeout=5
                ) as captured:
                    capture = json.load(captured)
                assert capture["provider_requests"] == 1
                assert not capture[
                    "provider_saw_canary"
                ], f"{case.case_id}: sensitive egress"
                expected_masked = verdicts[case.case_id]["expected_masked"]
                assert capture["provider_saw_pii_placeholder"] == expected_masked
                flows += 1
                if "instruction_login_context" not in case.tags:
                    continue
                _request(capture_url, "/capture/reset", {})
                if expected_masked:
                    try:
                        _request(block_proxy_url, "/v1/" + api, payload, stream=stream)
                    except urllib.error.HTTPError as error:
                        assert error.code == 422, f"{case.case_id}: unexpected block status"
                    else:
                        raise AssertionError(f"{case.case_id}: sensitive input not blocked")
                else:
                    response = _request(block_proxy_url, "/v1/" + api, payload, stream=stream)
                    assert _restored_text(response, api, stream) == case.text
                with urllib.request.urlopen(capture_url + "/capture", timeout=5) as captured:
                    capture = json.load(captured)
                assert capture["provider_requests"] == (0 if expected_masked else 1)
                login_block_flows += 1
    print(
        json.dumps(
            {
                "status": "ok",
                "analyzer_cases": len(cases),
                "entity_filter_cases": entity_filter_cases,
                "echo_flows": flows,
                "login_block_flows": login_block_flows,
                "holdout_cases": sum("holdout" in case.tags for case in cases),
                "accepted_exact_limitations": observed_limitations,
                "metrics": metrics["aggregate"],
            },
            sort_keys=True,
        )
    )


def check_dialogue_arguments(analyzer_url, proxy_url, block_proxy_url, capture_url):
    from dkb_corpus_checks import payload_for

    cases = [case for case in load_corpus(CORPUS) if "dialogue" in case.tags]
    client = AnalyzerClient(analyzer_url, timeout_seconds=60)
    verdicts = {case.case_id: check_case_result(case, client.analyze(case), "instruction_regressions")
                for case in cases}
    flows = blocked = clean = 0
    for case in cases:
        for api in ("chat/completions", "responses"):
            for stream in (False, True):
                payload = payload_for(case.text, api, stream, arguments=True)
                _request(capture_url, "/capture/reset", {})
                response = _request(proxy_url, "/v1/" + api, payload, stream=stream)
                assert _restored_text(response, api, stream) == case.text, (
                    case.case_id, api, stream, "dialogue argument restoration changed"
                )
                with urllib.request.urlopen(capture_url + "/capture", timeout=5) as captured:
                    result = json.load(captured)
                assert result["provider_requests"] == 1
                assert not result["provider_saw_canary"], case.case_id
                expected_masked = verdicts[case.case_id]["expected_masked"]
                assert result["provider_saw_pii_placeholder"] == expected_masked, (
                    case.case_id, api, stream, "unexpected masking of dialogue arguments"
                )
                flows += 1
                _request(capture_url, "/capture/reset", {})
                if expected_masked:
                    try:
                        _request(block_proxy_url, "/v1/" + api, payload, stream=stream)
                    except urllib.error.HTTPError as error:
                        assert error.code == 422, (case.case_id, error.code)
                    else:
                        raise AssertionError((case.case_id, "sensitive dialogue arguments not blocked"))
                    expected_requests = 0
                    blocked += 1
                else:
                    response = _request(block_proxy_url, "/v1/" + api, payload, stream=stream)
                    assert _restored_text(response, api, stream) == case.text
                    expected_requests = 1
                    clean += 1
                with urllib.request.urlopen(capture_url + "/capture", timeout=5) as captured:
                    result = json.load(captured)
                assert result["provider_requests"] == expected_requests
                assert not result["provider_saw_canary"]
    print(json.dumps({"status": "ok", "dialogue_argument_cases": len(cases),
                      "echo_flows": flows, "blocked_flows": blocked, "clean_block_flows": clean}))


def check_names(
    analyzer_url, proxy_url, block_proxy_url, capture_url, *, context=False,
    field_holdout=False,
):
    cases = (
        _field_holdout_cases() if field_holdout
        else _context_cases() if context else _name_cases()
    )
    client = AnalyzerClient(analyzer_url, timeout_seconds=60)
    predictions = {case.case_id: client.analyze(case) for case in cases}
    metrics = evaluate_predictions(cases, predictions)
    corpus = "name_field_holdout" if field_holdout else (
        "name_context_regressions" if context else "person_organization_regressions")
    verdicts = {case.case_id: check_case_result(case, predictions[case.case_id], corpus) for case in cases}
    limitations = [key for key, value in verdicts.items() if value["assessment"] == "accepted_limitation"]
    filters = flows = blocked = clean_blocked = 0
    filter_coverage = []
    for case in cases:
        results = predictions[case.case_id]
        name_reference = results
        if context or field_holdout:
            combined = _request(
                analyzer_url,
                "/api/v1/analyze",
                {
                    "text": case.text,
                    "entities": sorted(NAME_TYPES),
                    "score_threshold": 0.35,
                },
            )["entities"]
            assert combined == [
                result for result in results if result["entity_type"] in NAME_TYPES
            ], f"{case.case_id}: combined name filter differs from full scan"
            filters += 1
        for entity_type in sorted(NAME_TYPES):
            filtered = _request(
                analyzer_url,
                "/api/v1/analyze",
                {
                    "text": case.text,
                    "entities": [entity_type],
                    "score_threshold": 0.35,
                },
            )["entities"]
            assert filtered == [
                result
                for result in name_reference
                if result["entity_type"] == entity_type
            ], f"{case.case_id}: filtered analysis differs"
            if case.expected and verdicts[case.case_id]["assessment"] == "accepted_limitation":
                filter_coverage.append({"case_id": case.case_id, "entities": [entity_type],
                                        "coverage": coverage(case, filtered)["values"]})
            filters += 1
        for api in ("chat/completions", "responses"):
            for stream in (False, True):
                payload = {
                    "model": (
                        "mock-chat" if api == "chat/completions" else "mock-responses"
                    ),
                    "stream": stream,
                }
                if api == "chat/completions":
                    payload["messages"] = [{"role": "user", "content": case.text}]
                else:
                    payload["input"] = case.text
                _request(capture_url, "/capture/reset", {})
                response = _request(proxy_url, "/v1/" + api, payload, stream=stream)
                assert (
                    _restored_text(response, api, stream) == case.text
                ), f"{case.case_id}: restoration changed"
                with urllib.request.urlopen(
                    capture_url + "/capture", timeout=5
                ) as captured:
                    capture = json.load(captured)
                assert capture["provider_requests"] == 1
                assert not capture[
                    "provider_saw_canary"
                ], f"{case.case_id}: sensitive egress"
                expected_masked = verdicts[case.case_id]["expected_masked"]
                assert capture["provider_saw_pii_placeholder"] == expected_masked
                flows += 1
                _request(capture_url, "/capture/reset", {})
                if expected_masked:
                    try:
                        _request(block_proxy_url, "/v1/" + api, payload, stream=stream)
                    except urllib.error.HTTPError as error:
                        assert error.code == 422, f"{case.case_id}: unexpected block status"
                    else:
                        raise AssertionError(f"{case.case_id}: sensitive request was not blocked")
                    blocked += 1
                else:
                    response = _request(block_proxy_url, "/v1/" + api, payload, stream=stream)
                    assert _restored_text(response, api, stream) == case.text
                    clean_blocked += 1
                with urllib.request.urlopen(
                    capture_url + "/capture", timeout=5
                ) as captured:
                    capture = json.load(captured)
                assert capture["provider_requests"] == (0 if expected_masked else 1)
                assert not capture["provider_saw_canary"]
    print(
        json.dumps(
            {
                "status": "ok",
                "corpus": (
                    "name_field_holdout" if field_holdout
                    else "name_context" if context else "names"
                ),
                "name_cases": len(cases),
                "name_filter_cases": filters,
                "echo_flows": flows,
                "blocked_flows": blocked,
                "clean_block_flows": clean_blocked,
                "exact_limitations": sorted(limitations),
                "accepted_filter_coverage": filter_coverage,
                "metrics": metrics["aggregate"],
            },
            sort_keys=True,
        )
    )


def check_accepted_coverage(analyzer_url):
    cases = accepted_coverage_cases()
    client = AnalyzerClient(analyzer_url, timeout_seconds=60)
    predictions = {case.case_id: client.analyze(case) for case in cases}
    verdicts = {case.case_id: check_case_result(case, predictions[case.case_id], "accepted_coverage")
                for case in cases}
    report = {
        "status": "passed",
        "contract": "owner_accepted_known_coverage_gaps_not_complete_protection",
        "cases": [{"case_id": key, **value} for key, value in verdicts.items()],
        "raw_quality": evaluate_predictions(cases, predictions),
        "provider_requests": 0,
        "note": "Analyzer-only subset of the frozen comparison, not an independent corpus or egress test.",
    }
    folder = os.environ.get("NER_TEST_REPORT_DIR")
    if folder:
        policy = os.environ.get("PRESIDIO_ANALYZER_BASE64_POLICY", "content")
        write_report(Path(folder) / ("accepted-coverage-" + policy + ".json"), report)
    print(json.dumps({"status": "ok", "accepted_coverage_cases": len(cases),
                      "raw_quality": report["raw_quality"]["aggregate"]}, sort_keys=True))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--canaries", action="store_true")
    parser.add_argument("--analyzer-url")
    parser.add_argument("--proxy-url")
    parser.add_argument("--block-proxy-url")
    parser.add_argument("--capture-url")
    args = parser.parse_args()
    if args.canaries:
        print(
            ",".join(
                _canary_values(
                    (*load_corpus(CORPUS), *_name_cases(), *_context_cases(), *_field_holdout_cases())
                )
            )
        )
        return
    if not all(
        (args.analyzer_url, args.proxy_url, args.block_proxy_url, args.capture_url)
    ):
        parser.error("Analyzer, mask/block proxy and capture URLs are required")
    urls = (args.analyzer_url, args.proxy_url, args.block_proxy_url, args.capture_url)
    checks = [
        ("dialogue-arguments", lambda: check_dialogue_arguments(*urls)),
        ("instructions", lambda: check_instructions(*urls)),
        ("names", lambda: check_names(*urls)),
        ("name-context", lambda: check_names(*urls, context=True)),
        ("name-holdout", lambda: check_names(*urls, field_holdout=True)),
        ("accepted-coverage", lambda: check_accepted_coverage(urls[0])),
    ]
    cases = (*load_corpus(CORPUS), *_name_cases(), *_context_cases(), *_field_holdout_cases(),
             *accepted_coverage_cases())
    policy = os.environ.get("PRESIDIO_ANALYZER_BASE64_POLICY", "content")
    if policy not in {"content", "logistic"}:
        parser.error("Unsupported Base64 policy")
    folder = os.environ.get("NER_TEST_REPORT_DIR")
    report = {"status": "running", "base64_policy": policy, "checks": []}
    try:
        run_checks(checks, report["checks"], case_ids=[case.case_id for case in cases])
        report["status"] = "passed"
    except BaseException:
        report["status"] = "failed"
        raise
    finally:
        write_report(Path(folder) / f"instructions-{policy}.json" if folder else None, report)


if __name__ == "__main__":
    main()
