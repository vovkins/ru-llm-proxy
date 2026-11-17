"""HTTP regressions for the stock Responses-to-Chat bridge and local safeguards."""

import json
import urllib.request

import redis

from check_responses_history import request


TOOLS = [{"type": "function", "name": "record_values", "description": "Record values",
          "parameters": {"type": "object", "properties": {"values": {
              "type": "array", "items": {"type": "string"}}}, "required": ["values"]}}]
SEQUENCE_FAILURES = []


def capture():
    with urllib.request.urlopen("http://mock-upstream:8080/capture", timeout=5) as response:
        return json.load(response)


def response_output(body, stream, tool, *, expected_status="completed"):
    events = []
    if stream:
        events = [json.loads(line[5:].strip()) for line in body.splitlines()
                  if line.startswith("data:") and line[5:].strip() != "[DONE]"]
        assert not any(event.get("type") == "error" for event in events), events
        terminal = [event for event in events if event.get("type") in {
            "response.completed", "response.incomplete", "response.failed"}]
        assert len(terminal) == 1 and terminal[0]["type"] == "response." + expected_status, events
        response = terminal[0]["response"]
    else:
        response = json.loads(body)
    assert response["status"] == expected_status, response
    kind = "function_call" if tool else "message"
    items = [item for item in response["output"] if item["type"] == kind]
    assert len(items) == 1, response
    item = items[0]
    text = item["arguments"] if tool else "".join(part["text"] for part in item["content"])
    if stream:
        delta_kind = "response.function_call_arguments.delta" if tool else "response.output_text.delta"
        delta = "".join(event.get("delta", "") for event in events
                        if event.get("type") == delta_kind)
        # Stock Chat bridging reuses index 0 for reasoning and text/tools; the
        # final output array position is not a reliable delta discriminator.
        assert delta.encode() == text.encode(), (delta, text)
        assert all(type(event.get("sequence_number")) is int for event in events), events
        sequence = [event["sequence_number"] for event in events]
        if sequence != sorted(set(sequence)):
            SEQUENCE_FAILURES.append({"tool": tool, "events": [
                {"type": event["type"], "sequence_number": event["sequence_number"]}
                for event in events if "sequence_number" in event]})
    return response, text, events


def decode(body, stream, tool):
    response, text, _ = response_output(body, stream, tool)
    assert "<EMAIL_ADDRESS_" not in text, text
    return response["id"], json.loads(text)["values"] if tool else text.split(" / ")


def check_chat_edge(payload, expected, reasoning, finish):
    chat_payload = {"model": payload["model"], "stream": payload["stream"],
                    "messages": [{"role": "user", "content": payload["input"]}]}
    if payload.get("tools"):
        chat_payload.update(tools=[{"type": "function", "function": {
            key: value for key, value in tool.items() if key != "type"
        }} for tool in payload["tools"]], tool_choice="required")
    req = urllib.request.Request("http://localhost:4000/v1/chat/completions",
        data=json.dumps(chat_payload).encode(), headers={
            "Authorization": "Bearer sk-test-master", "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as result:
        assert result.status == 200
        body = result.read().decode()
    if payload["stream"]:
        events = [json.loads(line[5:].strip()) for line in body.splitlines()
                  if line.startswith("data:") and line[5:].strip() != "[DONE]"]
        assert not any("error" in event for event in events), events
        choices = [choice for event in events for choice in event.get("choices", [])]
        assert [choice["finish_reason"] for choice in choices if choice.get("finish_reason")] == [finish]
        text = "".join(call.get("function", {}).get("arguments", "") for choice in choices
                       for call in choice.get("delta", {}).get("tool_calls", [])) if payload.get("tools") else \
               "".join(choice.get("delta", {}).get("content") or "" for choice in choices)
        actual_reasoning = "".join(choice.get("delta", {}).get("reasoning_content") or "" for choice in choices)
    else:
        choice = json.loads(body)["choices"][0]
        assert choice["finish_reason"] == finish, choice
        message = choice["message"]
        text = message["tool_calls"][0]["function"]["arguments"] if payload.get("tools") else message["content"]
        actual_reasoning = message.get("reasoning_content") or ""
    assert text.encode() == expected.encode(), (text, expected)
    assert actual_reasoning == reasoning, (actual_reasoning, reasoning)


def check_messages_tool_only(client):
    value = "stateresponses-messages-bridge@example.test"
    payload = {
        "model": "mock-chat-bridge", "stream": False, "max_tokens": 50,
        "messages": [{"role": "user", "content": "STOCK_BRIDGE_ECHO " + value}],
        "tools": [{
            "name": TOOLS[0]["name"], "description": TOOLS[0]["description"],
            "input_schema": TOOLS[0]["parameters"],
        }],
    }
    req = urllib.request.Request(
        "http://localhost:4000/v1/messages", data=json.dumps(payload).encode(),
        headers={"Authorization": "Bearer sk-test-master", "Content-Type": "application/json",
                 "Anthropic-Version": "2023-06-01"},
    )
    with urllib.request.urlopen(req, timeout=30) as result:
        assert result.status == 200
        body = json.load(result)
    assert body["stop_reason"] == "tool_use"
    assert [block["type"] for block in body["content"]] == ["tool_use"]
    tool = body["content"][0]
    assert (tool["id"], tool["name"]) == ("call_bridge", TOOLS[0]["name"])
    assert tool["input"] == {"values": [value]}, "Messages tool arguments were not restored"
    assert not client.keys("pii_mapping:*"), "Messages retained a request mapping"


def run():
    SEQUENCE_FAILURES.clear()
    client = redis.Redis.from_url("redis://redis:6379")
    before = capture()["provider_requests"]
    count = provider_calls = chat_count = 0
    for stream in (False, True):
        for tool in (False, True):
            history = []
            for slot in ("alpha", "beta"):
                history.append({"role": "user", "content": "STOCK_BRIDGE_ECHO "
                                + f"stateresponses-bridge-{slot}@example.test"})
                payload = {"model": "mock-chat-bridge", "input": history, "stream": stream, "store": False}
                if tool:
                    payload.update(tools=TOOLS, tool_choice="required")
                status, body = request("http://localhost:4000", "sk-test-master", payload)
                assert status == 200, (status, body)
                previous, values = decode(body, stream, tool)
                expected = [f"stateresponses-bridge-{name}@example.test"
                            for name in ("alpha", "beta")[:len(history)]]
                assert values == expected, (values, expected)
                assert not client.keys("pii_mapping:*"), "Temporary mappings survived success"
                count += 1
                provider_calls += 1
            rejected = {"model": "mock-chat-bridge", "input": "STOCK_BRIDGE_ECHO "
                        "stateresponses-bridge-gamma@example.test", "stream": stream,
                        "previous_response_id": previous}
            sent = capture()["provider_requests"]
            status, body = request("http://localhost:4000", "sk-test-master", rejected)
            assert status == 400, (status, body)
            assert "responses_chat_bridge_history_unsupported" in body, body
            assert "previous_response_id" in body, body
            assert previous not in body and "gamma@example.test" not in body, body
            assert capture()["provider_requests"] == sent, "Rejected continuation reached provider"
            assert not client.keys("pii_mapping:*"), "Temporary mappings survived rejection"
            count += 1
    named = {"model": "mock-chat-bridge", "input": "STOCK_BRIDGE_ECHO "
             "stateresponses-bridge-alpha@example.test", "stream": True, "tools": TOOLS,
             "tool_choice": {"type": "function", "name": "record_values"}}
    sent = capture()["provider_requests"]
    status, body = request("http://localhost:4000", "sk-test-master", named)
    assert status == 400, (status, body)
    assert "responses_chat_bridge_named_tool_unsupported" in body, body
    assert "record_values" not in body and "alpha@example.test" not in body, body
    assert capture()["provider_requests"] == sent, "Rejected tool choice reached provider"
    assert not client.keys("pii_mapping:*"), "Temporary mappings survived tool rejection"
    count += 1
    named["stream"] = False
    status, body = request("http://localhost:4000", "sk-test-master", named)
    assert status == 200, (status, body)
    _, values = decode(body, False, True)
    assert values == ["stateresponses-bridge-alpha@example.test"], values
    assert not client.keys("pii_mapping:*"), "Temporary mappings survived named tool success"
    count += 1
    provider_calls += 1
    for stream in (False, True):
        for tool in (False, True):
            modes = ["BRIDGE_REASONING", "BRIDGE_LENGTH", "BRIDGE_LENGTH BRIDGE_PARTIAL_MARKER"]
            if tool:
                modes.append("BRIDGE_LENGTH BRIDGE_PARTIAL_JSON")
            for mode in modes:
                email = "stateresponses-bridge-alpha@example.test"
                payload = {"model": "mock-chat-bridge", "input": "STOCK_BRIDGE_ECHO " + mode + " " + email,
                           "stream": stream, "store": False}
                if tool:
                    payload.update(tools=TOOLS, tool_choice="required")
                status, body = request("http://localhost:4000", "sk-test-master", payload)
                assert status == 200, (mode, status, body)
                response, text, events = response_output(body, stream, tool,
                    expected_status="incomplete" if "BRIDGE_LENGTH" in mode else "completed")
                if "BRIDGE_PARTIAL_MARKER" in mode:
                    expected = "<EMAIL_ADDRESS_1>"[:-3]
                    if tool:
                        expected = '{"values":["' + expected
                elif "BRIDGE_PARTIAL_JSON" in mode:
                    expected = '{"values":["' + email + '"'
                else:
                    expected = json.dumps({"values": [email]}) if tool else email
                assert text.encode() == expected.encode(), (mode, text, expected)
                if tool and "PARTIAL" in mode:
                    try:
                        json.loads(text)
                    except json.JSONDecodeError:
                        pass
                    else:
                        raise AssertionError("Truncated arguments were silently completed")
                if mode == "BRIDGE_REASONING":
                    reasoning = "".join(part.get("text", "") for item in response["output"]
                                        if item["type"] == "reasoning"
                                        for field in ("content", "summary") for part in item.get(field, []))
                    assert reasoning == "checking " + email, response
                    if stream:
                        deltas = "".join(event.get("delta", "") for event in events
                                         if event.get("type") in {"response.reasoning_text.delta",
                                                                  "response.reasoning_summary_text.delta"})
                        assert deltas == reasoning, (deltas, reasoning)
                assert not client.keys("pii_mapping:*"), "Temporary mappings survived bridge edge case"
                print(json.dumps({"edge_case": mode, "stream": stream, "tool": tool,
                                  "api": "responses", "status": response["status"]}), flush=True)
                count += 1
                provider_calls += 1
                check_chat_edge(payload, expected, "checking " + email if mode == "BRIDGE_REASONING" else "",
                                "length" if "BRIDGE_LENGTH" in mode else "tool_calls" if tool else "stop")
                assert not client.keys("pii_mapping:*"), "Temporary mappings survived Chat edge case"
                print(json.dumps({"edge_case": mode, "stream": stream, "tool": tool,
                                  "api": "chat", "finish_reason": "length" if "BRIDGE_LENGTH" in mode else
                                  "tool_calls" if tool else "stop"}), flush=True)
                chat_count += 1
                provider_calls += 1
    for stream in (False, True):
        for tool in (False, True):
            for mode in ("BRIDGE_REASONING", "BRIDGE_LENGTH"):
                payload = {"model": "mock-chat-bridge", "input": "STOCK_BRIDGE_ECHO BRIDGE_CLEAN " + mode,
                           "stream": stream, "store": False}
                if tool:
                    payload.update(tools=TOOLS, tool_choice="required")
                status, body = request("http://localhost:4000", "sk-test-master", payload)
                assert status == 200, (status, body)
                _, text, _ = response_output(body, stream, tool,
                    expected_status="incomplete" if mode == "BRIDGE_LENGTH" else "completed")
                expected = json.dumps({"values": ["safe-bridge-value"]}) if tool else "safe-bridge-value"
                assert text == expected, (text, expected)
                assert not client.keys("pii_mapping:*"), "Clean request unexpectedly retained a mapping"
                count += 1
                provider_calls += 1
                check_chat_edge(payload, expected, "checking safe-bridge-value" if mode == "BRIDGE_REASONING" else "",
                                "length" if mode == "BRIDGE_LENGTH" else "tool_calls" if tool else "stop")
                assert not client.keys("pii_mapping:*"), "Clean Chat request retained a mapping"
                chat_count += 1
                provider_calls += 1
    check_messages_tool_only(client)
    provider_calls += 1
    result = capture()
    assert result["provider_requests"] == before + provider_calls, result
    assert result["provider_request_paths"][before:] == ["/v1/chat/completions"] * provider_calls, result
    assert result["provider_saw_pii_placeholder"], result
    assert not result["provider_saw_synthetic_marker"], result
    print(json.dumps({"responses_chat_bridge_checks": count, "chat_edge_checks": chat_count,
                      "messages_tool_only_checks": 1,
                      "sequence_failures": SEQUENCE_FAILURES,
                      "passed": not SEQUENCE_FAILURES}), flush=True)
    assert not SEQUENCE_FAILURES, "Responses bridge event numbering violated strict ordering"


if __name__ == "__main__":
    run()
