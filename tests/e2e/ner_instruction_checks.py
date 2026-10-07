"""Check real Analyzer decisions and synthetic echo flows in the NER gate."""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from itertools import combinations
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from presidio.evaluation.corpus import DEFAULT_CORPUS_PATH, load_corpus
from presidio.evaluation.metrics import evaluate_predictions
from presidio.evaluation.run_baseline import AnalyzerClient

CORPUS = DEFAULT_CORPUS_PATH.with_name("instruction_regressions.jsonl")
NAME_CORPUS = DEFAULT_CORPUS_PATH.with_name("person_organization_regressions.jsonl")
CONTEXT_CORPUS = DEFAULT_CORPUS_PATH.with_name("name_context_regressions.jsonl")
FIELD_HOLDOUT_CORPUS = DEFAULT_CORPUS_PATH.with_name("name_field_holdout.jsonl")
NAME_TYPES = frozenset({"PERSON", "ORGANIZATION"})
# Model limitations remain explicit; these are not exemptions in production code.
NAME_EXACT_LIMITATIONS = {
    "person_uppercase": (["PERSON"], ["ORGANIZATION"]),
}
CONTEXT_EXACT_LIMITATIONS = {
    "client_uppercase": (["PERSON"], ["ORGANIZATION"]),
}
# The original control exposed a header boundary regression. Its annotation is
# unchanged and must now pass exactly; it is no longer an independent control.
FIELD_HOLDOUT_EXACT_LIMITATIONS = {}


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


def _request(base_url, path, payload, *, stream=False):
    request = urllib.request.Request(
        base_url + path,
        data=json.dumps(payload, ensure_ascii=False).encode(),
        headers={
            "Authorization": "Bearer sk-test-master",
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(request, timeout=60) as response:
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


def check_instructions(analyzer_url, proxy_url, capture_url):
    entity_filter_cases = check_entity_filters(analyzer_url)
    cases = load_corpus(CORPUS)
    client = AnalyzerClient(analyzer_url, timeout_seconds=60)
    predictions = {case.case_id: client.analyze(case) for case in cases}
    metrics = evaluate_predictions(cases, predictions)
    for case in cases:
        results = predictions[case.case_id]
        if not case.expected:
            assert not results, f"{case.case_id}: unexpected masking"
        for expected in case.expected:
            assert any(
                result["start"] <= expected.start and result["end"] >= expected.end
                for result in results
            ), f"{case.case_id}: sensitive value not fully covered"
    assert metrics["aggregate"]["critical_coverage_recall"] == 1.0

    flows = 0
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
                assert capture["provider_saw_pii_placeholder"] == bool(case.expected)
                flows += 1
    print(
        json.dumps(
            {
                "status": "ok",
                "analyzer_cases": len(cases),
                "entity_filter_cases": entity_filter_cases,
                "echo_flows": flows,
                "holdout_cases": sum("holdout" in case.tags for case in cases),
                "metrics": metrics["aggregate"],
            },
            sort_keys=True,
        )
    )


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
    limitations = {
        row["case_id"]: (row["missing_entity_types"], row["unexpected_target_types"])
        for row in metrics["cases"]
        if row["missing_entity_types"] or row["unexpected_target_types"]
    }
    expected_limitations = (
        FIELD_HOLDOUT_EXACT_LIMITATIONS if field_holdout
        else CONTEXT_EXACT_LIMITATIONS if context else NAME_EXACT_LIMITATIONS
    )
    assert (
        limitations == expected_limitations
    ), "name corpus: unexpected exact-span/type change"
    assert metrics["aggregate"]["critical_coverage_recall"] == 1.0
    filters = flows = blocked = 0
    for case in cases:
        results = predictions[case.case_id]
        if not case.expected:
            assert not results, f"{case.case_id}: unexpected masking of negative name case"
        for expected in case.expected:
            assert any(
                result["start"] <= expected.start and result["end"] >= expected.end
                for result in results
            ), f"{case.case_id}: no complete sensitive span"
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
                assert capture["provider_saw_pii_placeholder"] == bool(results)
                flows += 1
                if not results:
                    continue
                _request(capture_url, "/capture/reset", {})
                try:
                    _request(block_proxy_url, "/v1/" + api, payload, stream=stream)
                except urllib.error.HTTPError as error:
                    assert error.code == 422, f"{case.case_id}: unexpected block status"
                else:
                    raise AssertionError(
                        f"{case.case_id}: sensitive request was not blocked"
                    )
                with urllib.request.urlopen(
                    capture_url + "/capture", timeout=5
                ) as captured:
                    assert json.load(captured)["provider_requests"] == 0
                blocked += 1
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
                "exact_limitations": sorted(limitations),
                "metrics": metrics["aggregate"],
            },
            sort_keys=True,
        )
    )


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
    check_instructions(args.analyzer_url, args.proxy_url, args.capture_url)
    check_names(
        args.analyzer_url, args.proxy_url, args.block_proxy_url, args.capture_url
    )
    check_names(
        args.analyzer_url,
        args.proxy_url,
        args.block_proxy_url,
        args.capture_url,
        context=True,
    )
    check_names(
        args.analyzer_url,
        args.proxy_url,
        args.block_proxy_url,
        args.capture_url,
        field_holdout=True,
    )


if __name__ == "__main__":
    main()
