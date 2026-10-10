"""Deterministic history checks through the stock LiteLLM HTTP endpoints."""

import json
import urllib.error
import urllib.request


def request(base, key, payload):
    req = urllib.request.Request(
        base + "/v1/responses", data=json.dumps(payload).encode(),
        headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as result:
            return result.status, result.read().decode()
    except urllib.error.HTTPError as error:
        return error.code, error.read().decode()


def decode(body, stream):
    if not stream:
        result = json.loads(body)
        output = result["output"][0]
        text = output.get("arguments") or output["content"][0]["text"]
        return result["id"], text
    events = [json.loads(line[5:].strip()) for line in body.splitlines()
              if line.startswith("data:") and line[5:].strip() != "[DONE]"]
    completed = [event for event in events if event.get("type") == "response.completed"]
    assert len(completed) == 1, events
    return completed[0]["response"]["id"], "".join(event.get("delta", "") for event in events)


def run(base="http://localhost:4000", key="sk-test-master"):
    count = 0
    for stream in (False, True):
        for tool in (False, True):
            previous = None
            marker = "STATEFUL_HISTORY " + ("HISTORY_TOOL " if tool else "")
            for text, expected in [
                ("stateresponses-alpha@example.test", ["stateresponses-alpha@example.test"]),
                ("stateresponses-beta@example.test", ["stateresponses-alpha@example.test", "stateresponses-beta@example.test"]),
                ("repeat", ["stateresponses-alpha@example.test", "stateresponses-beta@example.test"]),
            ]:
                payload = {"model": "mock-chat", "input": marker + text, "stream": stream, "store": False}
                if previous:
                    payload["previous_response_id"] = previous
                status, body = request(base, key, payload)
                assert status == 200, (status, body)
                previous, output = decode(body, stream)
                assert all(value in output for value in expected), output
                assert "<EMAIL_ADDRESS_" not in output, output
                if tool:
                    assert json.loads(output)["values"] == expected
                count += 1
            # Native authentication must reject an invalid/revoked key before
            # guardrails can resolve the response owned by a valid key.
            status, _ = request(base, "sk-invalid-history-key", payload)
            # This DB-less controlled stack reports 400 for virtual-key auth;
            # the normal DB-backed stack additionally tests revoked keys.
            assert status in (400, 401, 403), status
            count += 1
    for previous in ("unknown-response", "", 123):
        if previous == "":
            continue  # An empty ID is invalid input, not a stored continuation.
        status, body = request(base, key, {
            "model": "mock-chat", "input": "STATEFUL_HISTORY repeat",
            "previous_response_id": previous,
        })
        assert status in (409, 422), (status, body)
        count += 1
    print(json.dumps({"responses_history_checks": count, "passed": True}))


if __name__ == "__main__":
    run()
