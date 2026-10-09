"""Real Analyzer corpus coverage, captured egress, exact echo and block flows."""

import argparse
import json
import urllib.error
import urllib.request
from pathlib import Path

from ner_instruction_checks import _request, _restored_text


CORPUS = Path(__file__).resolve().parents[1] / "fixtures/dkb"


def capture(url):
    with urllib.request.urlopen(url + "/capture", timeout=5) as response:
        return json.load(response)


def reset(url):
    _request(url, "/capture/reset", {})


def payload_for(text, api, stream, arguments):
    payload = {"model": "mock-chat" if api == "chat/completions" else "mock-responses", "stream": stream}
    if api == "chat/completions":
        payload["messages"] = [{"role": "user", "content": text}]
        if arguments:
            payload["messages"] += [
                {"role": "assistant", "tool_calls": [{"id": "call-dkb", "type": "function", "function": {"name": "inspect", "arguments": json.dumps({"source": text}, ensure_ascii=False)}}]},
                {"role": "tool", "tool_call_id": "call-dkb", "content": "ok"},
                {"role": "user", "content": text},
            ]
    else:
        payload["input"] = text
        if arguments:
            payload["input"] = [
                {"role": "user", "content": [{"type": "input_text", "text": text}]},
                {"type": "function_call", "call_id": "call-dkb", "name": "inspect", "arguments": json.dumps({"source": text}, ensure_ascii=False)},
                {"type": "function_call_output", "call_id": "call-dkb", "output": "ok"},
                {"role": "user", "content": [{"type": "input_text", "text": text}]},
            ]
    return payload


def check_chunk_boundaries(analyzer_url, proxy_url, block_proxy_url, capture_url):
    cases = [
        ("long", "Base64", "BASE64_DATA", "", "A" * 140000, False),
        ("offset", "payload_b64", "BASE64_DATA", " " * 115000, "A" * 20000, False),
        ("multiline", "password_b64", "PASSWORD", " " * 115000,
         "A" * 10000 + "\n    " + "B" * 10000, True),
    ]
    flows = blocked = 0
    for case_id, key, entity, prefix, value, quoted in cases:
        text = prefix + (f'{key}: "{value}"' if quoted else f"{key}: {value}")
        arguments_text = json.dumps({"ordinary": prefix, key: value})
        encoded_value = json.dumps(value)[1:-1]
        for source, expected_value in ((text, value), (arguments_text, encoded_value)):
            entities = _request(analyzer_url, "/api/v1/analyze", {
                "text": source, "score_threshold": 0.35,
            }, timeout=180)["entities"]
            start = source.index(expected_value)
            assert any(
                r["entity_type"] == entity and r["start"] == start
                and r["end"] == start + len(expected_value)
                for r in entities
            ), (case_id, "complete source span missing")
        for api in ("chat/completions", "responses"):
            for stream in (False, True):
                for arguments in (False, True):
                    context = (case_id, api, stream, arguments)
                    payload = payload_for(text, api, stream, arguments)
                    if arguments:
                        if api == "chat/completions":
                            payload["messages"][1]["tool_calls"][0]["function"]["arguments"] = arguments_text
                        else:
                            payload["input"][1]["arguments"] = arguments_text
                    reset(capture_url)
                    response = _request(proxy_url, "/v1/" + api, payload, stream=stream, timeout=180)
                    assert _restored_text(response, api, stream) == text, (context, "restoration")
                    captured = capture(capture_url)
                    assert captured["provider_requests"] == 1, context
                    assert captured["provider_saw_pii_placeholder"], context
                    assert not captured["provider_saw_base64_boundary_value"], (context, "partial or full sensitive egress")
                    flows += 1
                    reset(capture_url)
                    try:
                        _request(block_proxy_url, "/v1/" + api, payload, stream=stream, timeout=180)
                    except urllib.error.HTTPError as error:
                        assert error.code == 422, (context, error.code)
                    else:
                        raise AssertionError((context, "block request succeeded"))
                    assert capture(capture_url)["provider_requests"] == 0, context
                    blocked += 1
    print(json.dumps({"status": "ok", "base64_boundary_cases": len(cases), "echo_flows": flows, "blocked_flows": blocked}))


def check(analyzer_url, proxy_url, block_proxy_url, capture_url):
    cases = json.loads((CORPUS / "manifest.json").read_text())
    flows = blocked = annotations = json_annotations = final_blocked = 0
    for case in cases:
        text = (CORPUS / case["file"]).read_bytes().decode("utf-8")
        entities = _request(analyzer_url, "/api/v1/analyze", {"text": text, "score_threshold": 0.35})["entities"]
        for expected in case["expected"]:
            assert any(
                entity["start"] <= expected["start"] and entity["end"] >= expected["end"]
                and entity["entity_type"] in expected["entities"]
                for entity in entities
            ), (case["file"], expected["start"], expected["entities"])
            annotations += 1
        arguments_text = json.dumps({"source": text}, ensure_ascii=False)
        cursor = arguments_text.index(": ") + 3
        offsets = [cursor]
        for character in text:
            cursor += len(json.dumps(character, ensure_ascii=False)[1:-1])
            offsets.append(cursor)
        argument_entities = _request(
            analyzer_url, "/api/v1/analyze",
            {"text": arguments_text, "score_threshold": 0.35},
        )["entities"]
        for expected in case["expected"]:
            for index in range(expected["start"], expected["end"]):
                if text[index].isspace():
                    continue
                assert any(
                    entity["start"] <= offsets[index]
                    and entity["end"] >= offsets[index + 1]
                    for entity in argument_entities
                ), (case["file"], index, "unprotected JSON character")
            json_annotations += 1
        for api in ("chat/completions", "responses"):
            for stream in (False, True):
                for arguments in (False, True):
                    context = (case["file"], api, stream, arguments)
                    payload = payload_for(text, api, stream, arguments)
                    reset(capture_url)
                    try:
                        response = _request(proxy_url, "/v1/" + api, payload, stream=stream)
                    except urllib.error.HTTPError as error:
                        detail = json.load(error).get("error", {})
                        expected_code = case["mask_block_code"]
                        # LiteLLM exposes its standard HTTP code, not the internal audit code.
                        assert error.code == 422 and expected_code and "confirmed raw leak marker" in detail.get("message", ""), (context, error.code, detail.get("code"))
                        assert capture(capture_url)["provider_requests"] == 0, context
                        final_blocked += 1
                    else:
                        assert case["mask_block_code"] is None, (context, "expected final block")
                        assert _restored_text(response, api, stream) == text, (context, "restoration")
                        captured = capture(capture_url)
                        assert captured["provider_requests"] == 1, context
                        assert not captured["provider_saw_dkb_canary"], (context, "sensitive egress")
                        assert captured["provider_saw_pii_placeholder"], context
                        flows += 1
                    reset(capture_url)
                    try:
                        _request(block_proxy_url, "/v1/" + api, payload, stream=stream)
                    except urllib.error.HTTPError as error:
                        assert error.code == 422, (context, error.code)
                    else:
                        raise AssertionError((context, "block request succeeded"))
                    assert capture(capture_url)["provider_requests"] == 0, context
                    blocked += 1
    print(json.dumps({"status": "ok", "dkb_files": len(cases), "annotations": annotations, "json_annotations": json_annotations, "echo_flows": flows, "blocked_flows": blocked, "final_blocked_flows": final_blocked}))
    check_chunk_boundaries(analyzer_url, proxy_url, block_proxy_url, capture_url)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    for name in ("analyzer", "proxy", "block-proxy", "capture"):
        parser.add_argument("--" + name + "-url", required=True)
    args = parser.parse_args()
    check(args.analyzer_url, args.proxy_url, args.block_proxy_url, args.capture_url)
