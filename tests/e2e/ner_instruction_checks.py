"""Check real Analyzer decisions and synthetic echo flows in the NER gate."""

from __future__ import annotations

import argparse
import json
import sys
import urllib.request
from itertools import combinations
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from presidio.evaluation.corpus import DEFAULT_CORPUS_PATH, load_corpus
from presidio.evaluation.metrics import evaluate_predictions
from presidio.evaluation.run_baseline import AnalyzerClient

CORPUS = DEFAULT_CORPUS_PATH.with_name("instruction_regressions.jsonl")


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


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--canaries", action="store_true")
    parser.add_argument("--analyzer-url")
    parser.add_argument("--proxy-url")
    parser.add_argument("--capture-url")
    args = parser.parse_args()
    if args.canaries:
        print(
            ",".join(
                sorted(
                    {
                        case.text[entity.start : entity.end]
                        for case in load_corpus(CORPUS)
                        for entity in case.expected
                    }
                )
            )
        )
        return
    if not all((args.analyzer_url, args.proxy_url, args.capture_url)):
        parser.error("Analyzer, proxy and capture URLs are required")
    check_instructions(args.analyzer_url, args.proxy_url, args.capture_url)


if __name__ == "__main__":
    main()
