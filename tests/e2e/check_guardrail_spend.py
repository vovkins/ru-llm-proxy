#!/usr/bin/env python3
"""Assert per-call guardrail cards in both the Logs API and PostgreSQL."""

import argparse
from collections import Counter
import datetime
import http.client
import json
import os
import re
import socket
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid


HEADERS = {"Authorization": "Bearer sk-test-master", "Content-Type": "application/json"}
PERSON = "UI_FIXTURE_PERSON_VALUE"
REFUSAL = "TOKEN=ui_private_value\nTraceback (most recent call last):"


def get(url):
    with urllib.request.urlopen(urllib.request.Request(url, headers=HEADERS), timeout=15) as response:
        return json.load(response)


def invoke(base, api, stream, guardrails, content, *, metadata=None):
    path = "/v1/chat/completions" if api == "chat" else "/v1/responses"
    if isinstance(guardrails, str):
        guardrails = [guardrails]
    data = {"model": "mock-chat" if api == "chat" else "mock-responses",
            "guardrails": guardrails, "stream": stream}
    data.update({"messages": [{"role": "user", "content": content}]} if api == "chat" else {"input": content})
    if metadata is not None:
        data["metadata"] = metadata
    request = urllib.request.Request(base + path, data=json.dumps(data).encode(), headers=HEADERS)
    try:
        with urllib.request.urlopen(request, timeout=40) as response:
            return response.status, response.headers["x-litellm-call-id"], response.read().decode()
    except urllib.error.HTTPError as error:
        return error.code, error.headers["x-litellm-call-id"], error.read().decode()


def pg_row(compose, call_id):
    assert re.fullmatch(r"[a-f0-9-]{36}", call_id)
    sql = (
        'SELECT json_build_object(\'request_id\', request_id, \'status\', status, '
        '\'metadata\', metadata)::text FROM "LiteLLM_SpendLogs" '
        f"WHERE metadata->>'litellm_call_id' = '{call_id}'"
    )
    result = subprocess.check_output([*compose, "exec", "-T", "db", "psql", "-qtAX",
                                      "-U", "postgres", "-c", sql], text=True, timeout=15)
    lines = result.splitlines()
    assert len(lines) <= 1, f"duplicate spend rows for {call_id}"
    return json.loads(lines[0]) if lines else None


def provider_requests(compose):
    response = subprocess.check_output([
        *compose, "exec", "-T", "mock-upstream", "python", "-c",
        "import json,urllib.request;print(json.load(urllib.request.urlopen("
        "'http://localhost:8080/capture'))['provider_requests'])",
    ], text=True, timeout=15)
    return int(response.strip())


def mock_control(compose, path, payload=None):
    script = (
        "import json,sys,urllib.request;"
        "raw=sys.argv[2];data=None if raw=='-' else raw.encode();"
        "request=urllib.request.Request('http://localhost:8080'+sys.argv[1],data=data,"
        "headers={'Content-Type':'application/json'});"
        "print(json.dumps(json.load(urllib.request.urlopen(request,timeout=5))))"
    )
    result = subprocess.check_output([
        *compose, "exec", "-T", "mock-upstream", "python", "-c", script,
        path, "-" if payload is None else json.dumps(payload),
    ], text=True, timeout=15)
    return json.loads(result)


def wait_mock_state(compose, key, *, closed=False, timeout=10):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = mock_control(compose, "/control/requests").get(key, {})
        if value and (not closed or value.get("closed_at") is not None):
            return value
        time.sleep(.05)
    raise AssertionError(f"missing mock state {key}, closed={closed}")


def redis_mappings(compose):
    result = subprocess.check_output([
        *compose, "exec", "-T", "redis", "redis-cli", "--scan", "--pattern", "pii_mapping:*",
    ], text=True, timeout=15)
    return set(result.splitlines())


def card_summary(cards):
    if not isinstance(cards, list):
        return repr(cards)
    return [
        {key: card.get(key) for key in (
            "guardrail_name", "guardrail_mode", "guardrail_action", "guardrail_status",
        )} if isinstance(card, dict) else repr(card)
        for card in cards
    ]


def wait_for_cards(base, compose, query, call_id, expected_count, label, *, ready=None):
    db = None
    api_rows = []
    db_cards = None
    api_cards = None
    deadline = time.monotonic() + 45
    while True:
        db = pg_row(compose, call_id)
        api_rows = [row for row in get(base + "/spend/logs/ui?" + query)["data"]
                    if row.get("metadata", {}).get("litellm_call_id") == call_id]
        if db:
            db_cards = db["metadata"].get("guardrail_information") or []
        if len(api_rows) == 1:
            api_cards = api_rows[0]["metadata"].get("guardrail_information") or []
        if (db and len(api_rows) == 1 and isinstance(db_cards, list) and isinstance(api_cards, list)
                and len(db_cards) == expected_count and db_cards == api_cards
                and (ready is None or ready(db_cards))):
            return db, api_rows[0], db_cards
        if time.monotonic() > deadline:
            raise AssertionError(
                f"missing or duplicate cards: {label} call={call_id} "
                f"database_cards={card_summary(db_cards)} logs_count={len(api_rows)} "
                f"logs_cards={card_summary(api_cards)}"
            )
        time.sleep(.5)


def assert_safe_log(db, api_row, cards, call_id, expected_status=None):
    assert db["metadata"]["litellm_call_id"] == call_id
    if expected_status is not None:
        assert db["status"] == expected_status, (call_id, db["status"])
    assert api_row["status"] == db["status"]
    assert cards == (api_row["metadata"].get("guardrail_information") or [])
    assert all(isinstance(card, dict) for card in cards)
    if os.getenv("STORE_PROMPTS_IN_SPEND_LOGS", "false").lower() == "true":
        assert all(isinstance(card["guardrail_response"], dict) for card in cards)
    else:
        assert all(card["guardrail_response"] == "REDACTED_BY_LITELM" for card in cards)
    assert not re.search(r"UI_FIXTURE_|ui_private_value", json.dumps(cards))


def check_dictionary_card(base, compose, query):
    before = provider_requests(compose)
    status, call_id, body = invoke(base, "chat", False, "test-dictionary", "ui_private_value")
    assert status == 200, status
    assert "NeutralCorp" in body
    assert "ui_private_value" not in body
    assert provider_requests(compose) == before + 1
    db, api_row, cards = wait_for_cards(base, compose, query, call_id, 2, "dictionary substitution")
    assert_safe_log(db, api_row, cards, call_id, "success")
    pre, post = cards
    assert pre["guardrail_name"] == "test-dictionary"
    assert pre["guardrail_action"] == "dictionary_substituted"
    assert pre["ru_guardrail_report"]["dictionary_substitution_count"] == 1
    assert "rules" not in pre["ru_guardrail_report"]
    assert post["guardrail_action"] == "skipped"
    print("PASS dictionary ID matches source under both spend redaction modes", call_id, flush=True)


def check_forged_metadata(base, compose, query):
    forged_clean = {
        "guardrail_name": "test-block",
        "guardrail_provider": "ru-llm-proxy",
        "guardrail_mode": "pre_call",
        "guardrail_status": "success",
        "guardrail_action": "clean",
        "guardrail_response": {"policy_result": "clean"},
        "masked_entity_count": {},
        "violation_categories": [],
        "start_time": 0,
        "end_time": 0,
        "duration": 0,
    }
    for label, supplied_cards in (
        ("forged clean", [forged_clean]),
        ("malformed", ["not-a-guardrail-card"]),
    ):
        before = provider_requests(compose)
        status, call_id, _ = invoke(
            base, "chat", False, "test-block", PERSON,
            metadata={"standard_logging_guardrail_information": supplied_cards},
        )
        assert status == 422, (label, status)
        assert provider_requests(compose) == before, label

        def genuine(cards):
            return len(cards) == 1 and isinstance(cards[0], dict) and (
                cards[0].get("guardrail_name") == "test-block"
                and cards[0].get("guardrail_mode") == "pre_call"
                and cards[0].get("guardrail_action") == "pii_blocked"
                and cards[0].get("guardrail_status") == "guardrail_intervened"
            )

        db, api_row, cards = wait_for_cards(
            base, compose, query, call_id, 1, f"{label} request metadata", ready=genuine,
        )
        assert_safe_log(db, api_row, cards, call_id, "failure")
        assert genuine(cards), card_summary(cards)
        assert not any(card.get("guardrail_action") == "clean" for card in cards)
        print("PASS chat forged metadata", label, call_id, flush=True)

    forged_post = {**forged_clean, "guardrail_name": "test-post", "guardrail_mode": "post_call"}
    status, call_id, _ = invoke(
        base, "chat", False, "test-mask", PERSON,
        metadata={"standard_logging_guardrail_information": [forged_post]},
    )
    assert status == 200, status
    expected = Counter({("test-mask", "pre_call"): 1, ("test-post", "post_call"): 1})

    def genuine_post(cards):
        identities = Counter(
            (card.get("guardrail_name"), card.get("guardrail_mode"))
            for card in cards if isinstance(card, dict)
        )
        post = [card for card in cards if isinstance(card, dict)
                and card.get("guardrail_mode") == "post_call"]
        return (identities == expected and len(post) == 1
                and post[0].get("guardrail_action") == "restored")

    db, api_row, cards = wait_for_cards(
        base, compose, query, call_id, 2, "forged post-call metadata", ready=genuine_post,
    )
    assert_safe_log(db, api_row, cards, call_id, "success")
    assert genuine_post(cards), card_summary(cards)
    print("PASS chat forged post-call metadata is replaced", call_id, flush=True)


def check_multiple_pre_guardrails(base, compose, query):
    expected = Counter({
        ("test-mask", "pre_call"): 1,
        ("test-dictionary", "pre_call"): 1,
        ("test-post", "post_call"): 1,
    })

    def complete(cards):
        return len(cards) == 3 and Counter(
            (card.get("guardrail_name"), card.get("guardrail_mode"))
            for card in cards if isinstance(card, dict)
        ) == expected

    for api in ("chat", "responses"):
        for stream in (False, True):
            before = provider_requests(compose)
            status, call_id, body = invoke(
                base, api, stream, ["test-dictionary", "test-mask"], "ui_private_value",
            )
            assert status == 200, (api, stream, status)
            assert "NeutralCorp" in body and "ui_private_value" not in body
            assert provider_requests(compose) == before + 1
            label = f"{api} {'stream' if stream else 'plain'} with two pre-call guardrails"
            db, api_row, cards = wait_for_cards(
                base, compose, query, call_id, 3, label, ready=complete,
            )
            assert_safe_log(db, api_row, cards, call_id, "success")
            assert complete(cards), card_summary(cards)
            by_name = {card["guardrail_name"]: card for card in cards}
            assert by_name["test-mask"]["guardrail_action"] == "clean"
            dictionary = by_name["test-dictionary"]
            assert dictionary["guardrail_action"] == "dictionary_substituted"
            assert dictionary["ru_guardrail_report"]["dictionary_substitution_count"] == 1
            assert by_name["test-post"]["guardrail_status"] == "success"
            print("PASS", label, call_id, flush=True)


def check_multiple_post_guardrails(base, compose, query):
    before = provider_requests(compose)
    status, call_id, body = invoke(
        base, "responses", True, ["test-mask", "test-post-secondary"], PERSON,
    )
    assert status == 200, status
    assert PERSON in body, "Responses SSE was not restored"
    assert "response.completed" in body, "Responses SSE did not complete"
    assert provider_requests(compose) == before + 1
    expected = Counter({
        ("test-mask", "pre_call"): 1,
        ("test-post", "post_call"): 1,
        ("test-post-secondary", "post_call"): 1,
    })

    def complete(cards):
        return len(cards) == 3 and Counter(
            (card.get("guardrail_name"), card.get("guardrail_mode"))
            for card in cards if isinstance(card, dict)
        ) == expected

    db, api_row, cards = wait_for_cards(
        base, compose, query, call_id, 3, "Responses SSE with two post-call guardrails", ready=complete,
    )
    assert_safe_log(db, api_row, cards, call_id, "success")
    assert complete(cards), card_summary(cards)
    pre = [card for card in cards if card["guardrail_mode"] == "pre_call"]
    post = [card for card in cards if card["guardrail_mode"] == "post_call"]
    assert len(pre) == 1 and len(post) == 2
    assert pre[0]["guardrail_action"] == "masked"
    assert pre[0]["guardrail_status"] == "guardrail_intervened"
    assert all(card["guardrail_status"] == "success" for card in post)
    assert any(card["guardrail_action"] == "restored" for card in post)
    print("PASS Responses SSE pre_call plus two distinct post_call cards", call_id, flush=True)


def check_interrupted_stream(base, compose, query):
    before = provider_requests(compose)
    status, call_id, body = invoke(
        base, "responses", True, "test-mask", PERSON + " SSE_CONTROLLED SSE_ERROR",
    )
    assert status == 200, status
    assert "response.completed" not in body, "Failed upstream became a terminal success"
    assert provider_requests(compose) == before + 1
    expected = Counter({("test-mask", "pre_call"): 1, ("test-post", "post_call"): 1})

    def diagnosed(cards):
        if len(cards) != 2 or not all(isinstance(card, dict) for card in cards):
            return False
        identities = Counter((card.get("guardrail_name"), card.get("guardrail_mode")) for card in cards)
        post = [card for card in cards if card.get("guardrail_mode") == "post_call"]
        return (identities == expected and len(post) == 1
                and post[0].get("guardrail_status") == "guardrail_failed_to_respond")

    db, api_row, cards = wait_for_cards(
        base, compose, query, call_id, 2, "interrupted Responses upstream", ready=diagnosed,
    )
    # Native spend status differs by LiteLLM transport; the guardrail must diagnose the failure either way.
    assert_safe_log(db, api_row, cards, call_id)
    assert diagnosed(cards), card_summary(cards)
    pre = next(card for card in cards if card["guardrail_mode"] == "pre_call")
    post = next(card for card in cards if card["guardrail_mode"] == "post_call")
    assert pre["guardrail_action"] == "masked"
    assert post["guardrail_action"] in {"incomplete", "upstream_failure"}
    print("PASS Responses upstream interruption has one diagnostic row", call_id, flush=True)


def check_client_cancel(base, compose, query):
    case_id = uuid.uuid4().hex[:16]
    parsed = urllib.parse.urlsplit(base)
    connection = http.client.HTTPConnection(parsed.hostname, parsed.port, timeout=15)
    payload = {
        "model": "mock-chat", "guardrails": ["test-mask"], "stream": True,
        "messages": [{"role": "user", "content": f"DISCONNECT_CASE_{case_id} SSE_CONTROLLED {PERSON}"}],
    }
    response = None
    try:
        connection.request("POST", "/v1/chat/completions", body=json.dumps(payload), headers=HEADERS)
        wait_mock_state(compose, f"provider:{case_id}")
        mock_control(compose, "/control/release", {"id": case_id, "phase": "provider"})
        mock_control(compose, "/control/release", {"id": case_id, "phase": "stream_first"})
        response = connection.getresponse()
        assert response.status == 200, response.status
        call_id = response.getheader("x-litellm-call-id")
        assert call_id
        while True:
            line = response.readline()
            assert line, "stream ended before the first text delta"
            if line.startswith(b"data: ") and b"chat.completion.chunk" in line and b"content" in line:
                break
        try:
            connection.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        response.close()
        connection.close()
        wait_mock_state(compose, f"stream_text:{case_id}", closed=True)

        expected = Counter({("test-mask", "pre_call"): 1, ("test-post", "post_call"): 1})

        def diagnosed(cards):
            identities = Counter(
                (card.get("guardrail_name"), card.get("guardrail_mode"))
                for card in cards if isinstance(card, dict)
            )
            post = [card for card in cards if isinstance(card, dict)
                    and card.get("guardrail_mode") == "post_call"]
            return (identities == expected and len(post) == 1
                    and post[0].get("guardrail_status") == "guardrail_failed_to_respond")

        db, api_row, cards = wait_for_cards(
            base, compose, query, call_id, 2, "client-cancelled Chat SSE", ready=diagnosed,
        )
        assert_safe_log(db, api_row, cards, call_id)
        post = next(card for card in cards if card["guardrail_mode"] == "post_call")
        assert post["guardrail_action"] in {"cancelled", "upstream_failure"}
        deadline = time.monotonic() + 10
        while redis_mappings(compose) and time.monotonic() < deadline:
            time.sleep(.1)
        assert not redis_mappings(compose), "client cancellation left a Redis mapping"
        print("PASS client cancellation updates native row and cleans Redis", call_id, flush=True)
    finally:
        if response is not None:
            response.close()
        connection.close()
        for phase in ("provider", "stream_first", "stream_text", "stream_placeholder",
                      "stream_partial", "stream_terminal"):
            try:
                mock_control(compose, "/control/release", {"id": case_id, "phase": phase})
            except (subprocess.SubprocessError, OSError):
                pass


def check_mapping_load_failure(base, compose, query):
    case_id = uuid.uuid4().hex[:16]
    parsed = urllib.parse.urlsplit(base)
    connection = http.client.HTTPConnection(parsed.hostname, parsed.port, timeout=20)
    payload = {
        "model": "mock-responses", "guardrails": ["test-mask"], "stream": False,
        "input": f"DISCONNECT_CASE_{case_id} {PERSON}",
    }
    paused = False
    before = provider_requests(compose)
    try:
        connection.request("POST", "/v1/responses", body=json.dumps(payload), headers=HEADERS)
        wait_mock_state(compose, f"provider:{case_id}")
        subprocess.check_output([*compose, "pause", "redis"], text=True, timeout=15)
        paused = True
        mock_control(compose, "/control/release", {"id": case_id, "phase": "provider"})
        response = connection.getresponse()
        call_id = response.getheader("x-litellm-call-id")
        body = response.read().decode()
        assert response.status == 503, (response.status, body)
        assert call_id and PERSON not in body
        assert provider_requests(compose) == before + 1
        subprocess.check_output([*compose, "unpause", "redis"], text=True, timeout=15)
        paused = False

        expected = Counter({("test-mask", "pre_call"): 1, ("test-post", "post_call"): 1})

        def diagnosed(cards):
            identities = Counter(
                (card.get("guardrail_name"), card.get("guardrail_mode"))
                for card in cards if isinstance(card, dict)
            )
            post = [card for card in cards if isinstance(card, dict)
                    and card.get("guardrail_mode") == "post_call"]
            return (identities == expected and len(post) == 1
                    and post[0].get("guardrail_action") == "fail_closed"
                    and post[0].get("ru_guardrail_report", {}).get("failure_operation") == "mapping_load")

        db, api_row, cards = wait_for_cards(
            base, compose, query, call_id, 2, "post-call mapping load failure", ready=diagnosed,
        )
        assert_safe_log(db, api_row, cards, call_id, "failure")
        deadline = time.monotonic() + 10
        while redis_mappings(compose) and time.monotonic() < deadline:
            time.sleep(.1)
        assert not redis_mappings(compose), "mapping-load failure left a Redis mapping"
        print("PASS mapping load failure preserves diagnosis and cleans Redis", call_id, flush=True)
    finally:
        if paused:
            subprocess.check_output([*compose, "unpause", "redis"], text=True, timeout=15)
        connection.close()
        try:
            mock_control(compose, "/control/release", {"id": case_id, "phase": "provider"})
        except (subprocess.SubprocessError, OSError):
            pass


def check(base, compose):
    start = (datetime.datetime.now(datetime.timezone.utc) -
             datetime.timedelta(days=1)).strftime("%Y-%m-%d %H:%M:%S")
    end = (datetime.datetime.now(datetime.timezone.utc) +
           datetime.timedelta(days=1)).strftime("%Y-%m-%d %H:%M:%S")
    query = urllib.parse.urlencode({"start_date": start, "end_date": end})
    for api in ("chat", "responses"):
        for stream in (False, True):
            for guardrail, text, expected_status, pre_action in (
                ("test-mask", PERSON, 200, "masked"),
                ("test-block", PERSON, 422, "pii_blocked"),
                ("test-block", REFUSAL, 422, "pre_egress_policy_blocked"),
            ):
                before = provider_requests(compose)
                status, call_id, body = invoke(base, api, stream, guardrail, text)
                assert status == expected_status, (api, stream, status)
                if status == 200:
                    assert PERSON in body, (api, stream, "response was not restored")
                    assert provider_requests(compose) == before + 1
                else:
                    assert provider_requests(compose) == before
                assert "ui_private_value" not in body
                label = f"{api} {'stream' if stream else 'plain'} {pre_action}"
                db, api_row, db_cards = wait_for_cards(
                    base, compose, query, call_id, 2 if status == 200 else 1, label,
                )
                assert_safe_log(db, api_row, db_cards, call_id, "success" if status == 200 else "failure")
                assert [card["guardrail_mode"] for card in db_cards] == (
                    ["pre_call", "post_call"] if status == 200 else ["pre_call"]
                )
                assert db_cards[0]["guardrail_name"] == guardrail
                assert db_cards[0]["guardrail_action"] == pre_action
                assert db_cards[0]["guardrail_status"] == (
                    "guardrail_intervened" if pre_action != "clean" else "success"
                )
                if status == 200:
                    assert db_cards[1]["guardrail_name"] == "test-post"
                    assert db_cards[1]["guardrail_action"] == "restored"
                    assert db_cards[1]["guardrail_status"] == "success"
                print("PASS", api, "stream" if stream else "plain", pre_action, call_id, flush=True)

    check_dictionary_card(base, compose, query)
    check_forged_metadata(base, compose, query)
    check_multiple_pre_guardrails(base, compose, query)
    check_multiple_post_guardrails(base, compose, query)
    check_interrupted_stream(base, compose, query)
    check_client_cancel(base, compose, query)
    check_mapping_load_failure(base, compose, query)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("base")
    parser.add_argument("compose", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    check(args.base, args.compose)
