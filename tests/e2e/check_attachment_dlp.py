"""Check the final attachment bytes and no-egress refusals on the smoke stack."""

import base64
import hashlib
import json
from urllib.error import HTTPError
from urllib.request import Request, urlopen


PROXY = "http://litellm:4000"
MOCK = "http://127.0.0.1:8080"
HEADERS = {
    "Authorization": "Bearer sk-test-master",
    "Content-Type": "application/json",
    "anthropic-version": "2023-06-01",
}
EMAIL = "loaduser0482@example.test"
MARKER = "ATTACHMENT_SMOKE_FIXTURE"
PATHS = ("/v1/chat/completions", "/v1/responses", "/v1/messages")


def request(url, payload=None):
    body = None if payload is None else json.dumps(payload).encode()
    try:
        with urlopen(Request(url, data=body, headers=HEADERS), timeout=25) as reply:
            return reply.status, json.load(reply)
    except HTTPError as error:
        return error.code, json.load(error)


def attachment_payload(path, text, *, json_file=False, reference=False):
    filename = "fixture.json" if json_file else "fixture.txt"
    if path == "/v1/messages":
        source = {"type": "text", "media_type": "text/plain", "data": text}
        if reference:
            source = {"type": "file", "file_id": "file-synthetic"}
        block = {"type": "document", "title": filename, "source": source}
        return {"model": "mock-claude", "max_tokens": 128, "messages": [{
            "role": "user", "content": [block],
        }]}
    mime_type = "application/json" if json_file else "text/plain"
    wire = base64.b64encode(text.encode()).decode()
    file = {"filename": filename, "file_data": f"data:{mime_type};base64,{wire}"}
    if reference:
        file = {"file_id": "file-synthetic"}
    if path == "/v1/responses":
        return {"model": "mock-chat", "store": False, "input": [{
            "role": "user", "content": [{"type": "input_file", **file}],
        }]}
    return {"model": "mock-chat", "messages": [{
        "role": "user", "content": [{"type": "file", "file": file}],
    }]}


def response_text(path, body):
    if path == "/v1/responses":
        return body["output"][0]["content"][0]["text"]
    if path == "/v1/messages":
        return body["content"][0]["text"]
    return body["choices"][0]["message"]["content"]


def expect_refusal(path, payload, expected_status, code):
    request(MOCK + "/capture/reset", {})
    status, body = request(PROXY + path, payload)
    assert status == expected_status and code in json.dumps(body), (path, status, body)
    _, observation = request(MOCK + "/capture")
    assert observation["provider_requests"] == observation["analyzer_requests"] == 0, observation


def check_masked_attachment(path, json_file):
    text = f"{MARKER} {path} Т-Банк {EMAIL}"
    if json_file:
        text = json.dumps({"text": text, "count": 42}, ensure_ascii=False)
    expected = text.replace("Т-Банк", "Зетта Групп").replace(EMAIL, "<EMAIL_ADDRESS_1>")
    expected_hash = hashlib.sha256(expected.encode()).hexdigest()
    request(MOCK + "/capture/reset", {})
    status, body = request(PROXY + path, attachment_payload(path, text, json_file=json_file))
    assert status == 200, (path, status, body)
    _, observation = request(MOCK + "/capture")
    assert observation["provider_requests"] == observation["analyzer_requests"] == 1, observation
    assert observation["provider_attachment_hashes"] == [expected_hash], (path, observation)
    assert response_text(path, body) == text, (path, body)


def main():
    config = "API_KEY=fixture-key\nPASSWORD=fixture-password"
    for path in PATHS:
        for json_file in (False, True):
            check_masked_attachment(path, json_file)
            original = json.dumps({"content": config}) if json_file else config
            expect_refusal(
                path, attachment_payload(path, original, json_file=json_file),
                422, "pre_egress_policy_blocked",
            )
        expect_refusal(
            path, attachment_payload(path, "", reference=True),
            422, "attachment_reference_unsupported",
        )
    for path in ("/v1/files", "/files", "/openai/v1/files"):
        expect_refusal(path, {}, 403, "attachment_upload_disabled")
    print("Attachment DLP smoke passed (18 cases)")


if __name__ == "__main__":
    main()
