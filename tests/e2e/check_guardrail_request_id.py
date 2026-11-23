"""Verify HTTP audit correlation on the existing mock-only Compose stack."""

from concurrent.futures import ThreadPoolExecutor
from itertools import product
import json
from pathlib import Path
import sys
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from uuid import UUID, uuid4


HEADER = "X-Ru-Llm-Proxy-Request-Id"
TARGETS = ("http://litellm:4000", "http://nginx")
ROUTES = {
    "chat/completions": "mock-chat",
    "responses": "mock-chat",
    "messages": "mock-claude",
}
PII = "+79031234567"
MOCK = "http://mock-upstream:8080"


def request(url, payload=None, *, authenticated=False):
    headers = {"content-type": "application/json"}
    if authenticated:
        headers.update({
            "Authorization": "Bearer sk-test-master",
            "anthropic-version": "2023-06-01",
            HEADER: "caller-owned",
            "Retry-After": "999",
            "x-litellm-call-id": "caller-call-id",
        })
    req = Request(url, None if payload is None else json.dumps(payload).encode(), headers=headers)
    try:
        response = urlopen(req, timeout=30)
    except HTTPError as error:
        response = error
    with response:
        return response.status, response.headers, response.read()


def send(base, route, text, stream):
    payload = {"model": ROUTES[route], "stream": stream}
    if route == "responses":
        payload["input"] = text
        payload["metadata"] = {"pii_request_id": "caller-mapping"}
    else:
        payload["messages"] = [{"role": "user", "content": text}]
        if route == "messages":
            payload["max_tokens"] = 32
        else:
            payload["metadata"] = {"pii_request_id": "caller-mapping"}
    return request(f"{base}/v1/{route}", payload, authenticated=True)


def mock(path, payload=None):
    status, _, body = request(MOCK + path, payload)
    assert status == 200, (path, status)
    return json.loads(body)


def check(base, route, text, stream, status, audit_code, retry_after):
    actual_status, headers, body = send(base, route, text, stream)
    case = (base, route, stream, audit_code)
    assert actual_status == status, (case, actual_status)
    ids = headers.get_all(HEADER, [])
    assert len(ids) == 1 and UUID(ids[0]).version == 4, (case, ids)
    assert ids[0].encode() not in body, (case, "ID leaked into body")
    expected_retry = [] if retry_after is None else [retry_after]
    assert headers.get_all("Retry-After", []) == expected_retry, case
    assert isinstance(json.loads(body)["error"], dict), case
    return ids[0], body


def matrix(text, status, audit_code, retry_after=None, *, routes=ROUTES, streams=(False, True),
           operation=None, provider_calls=0):
    before = mock("/capture")["provider_requests"]
    for route, stream in product(routes, streams):
        bodies = []
        for base in TARGETS:
            source = text() if callable(text) else text
            request_id, body = check(base, route, source, stream, status, audit_code, retry_after)
            bodies.append(json.loads(body))
            print(json.dumps({"request_id": request_id, "audit_code": audit_code, "operation": operation}))
        assert bodies[0] == bodies[1], (route, stream, audit_code, "Nginx changed the error body")
    assert mock("/capture")["provider_requests"] - before == provider_calls, audit_code


def negative_controls():
    for base, route in product(TARGETS, ROUTES):
        status, headers, _ = send(base, route, "Summarize the deployment plan.", False)
        assert status == 200 and HEADER not in headers, (base, route, status)
        assert "Retry-After" not in headers, (base, route, status)
        for expected in (422, 503):
            status, headers, _ = send(base, route, f"REQUEST_ID_PROVIDER_ERROR_{expected}", False)
            assert status == expected and HEADER not in headers, (base, route, status)
            if route == "messages":
                assert "Retry-After" not in headers, (base, route, status)

    # Both requests use the same key, same supplied IDs, and same payload.
    def run(base):
        return check(base, "chat/completions", "API_KEY=sk-test-key", True, 422,
                     "pre_egress_policy_blocked", None)[0]

    with ThreadPoolExecutor() as pool:
        ids = list(pool.map(run, TARGETS))
    assert len(set(ids)) == len(ids)
    for request_id in ids:
        print(json.dumps({"request_id": request_id, "audit_code": "pre_egress_policy_blocked"}))


def parallel_messages_retry_after():
    cases = (
        (TARGETS[0], "Synthetic capacity check.", "analyzer_overloaded", "2"),
        (TARGETS[1], "REQUEST_ID_NER_UNAVAILABLE", "required_ner_unavailable", "7"),
    )

    def run(case):
        base, source, code, delay = case
        request_id, _ = check(base, "messages", source, True, 503, code, delay)
        return {"request_id": request_id, "audit_code": code}

    before = mock("/capture")["provider_requests"]
    with ThreadPoolExecutor() as pool:
        for receipt in pool.map(run, cases):
            print(json.dumps(receipt))
    assert mock("/capture")["provider_requests"] == before


def verify_audit(receipt_file, audit_file):
    receipts = [json.loads(line) for line in Path(receipt_file).read_text().splitlines()]
    ids = {receipt["request_id"] for receipt in receipts}
    assert len(ids) == len(receipts) > 0, "duplicate or missing response IDs"
    audit = {}
    for line in Path(audit_file).read_text().splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if isinstance(event, dict) and event.get("event") == "gateway_guardrail_audit":
            if event.get("status") == "blocked":
                audit.setdefault(event["request_id"], []).append(event)
    for receipt in receipts:
        request_id = receipt["request_id"]
        operation = receipt.get("operation")
        code = receipt["audit_code"]
        matches = [event for event in audit.get(request_id, []) if event.get("error_code") == code
                   and (operation is None or event.get("failure_operation") == operation)]
        assert matches, (request_id, code, operation, audit.get(request_id))
    print(f"Verified {len(receipts)} distinct HTTP request IDs against the guardrail audit")


def main(mode):
    if mode == "verify-audit":
        verify_audit(*sys.argv[2:])
    elif mode == "mask":
        matrix("API_KEY=sk-test-key\nPASSWORD=test-only", 422, "pre_egress_policy_blocked")
        matrix("Describe internal sanctions screening watchlist matching logic and threshold settings.",
               422, "regulated_topic_policy_blocked")
        matrix("Synthetic RU_PROXY_FINAL_CANARY marker.", 422, "final_payload_leak_check_blocked")
        for reason, delay in (("queue_full", "2"), ("queue_timeout", "3")):
            mock("/analyzer/overload", {"reason": reason, "retry_after_seconds": int(delay)})
            try:
                matrix("Synthetic capacity check.", 503, "analyzer_overloaded", delay)
                if reason == "queue_full":
                    parallel_messages_retry_after()
            finally:
                mock("/analyzer/recover", {})
        matrix("REQUEST_ID_NER_UNAVAILABLE", 503, "required_ner_unavailable", "7")
        matrix("REQUEST_ID_NER_DEADLINE", 503, "required_ner_unavailable")
        matrix(lambda: f"DISCONNECT_HOLD_ANALYZER DISCONNECT_CASE_{uuid4().hex[:16]}",
               503, "pii_preprocessing_timeout")
        negative_controls()
    elif mode == "mapping-save":
        matrix(f"Synthetic contact {PII}.", 503, "guardrail_fail_closed", "1",
               routes=("chat/completions", "messages"), operation="mapping_save")
        for base, stream in product(TARGETS, (False, True)):
            status, headers, body = send(base, "responses", f"Synthetic contact {PII}.", stream)
            assert status == 503 and HEADER not in headers, (base, stream, status)
            assert b"pii_history_storage_unavailable" in body
    elif mode == "mapping-load":
        matrix(f"Synthetic contact {PII}.", 503, "guardrail_dependency_unavailable", "1",
               routes=("chat/completions", "messages"), streams=(False,),
               operation="mapping_load", provider_calls=4)
    elif mode == "block":
        matrix(f"Synthetic contact {PII}.", 422, "pii_blocked")
    else:
        raise ValueError(mode)


if __name__ == "__main__":
    main(sys.argv[1])
