"""Real HTTP disconnect regression, run inside the isolated LiteLLM container."""

import argparse
import http.client
import json
import socket
import time
import urllib.request
import uuid

import redis


RAW_PHONE = "+79031234567"
REDIS = redis.Redis.from_url("redis://redis:6379", decode_responses=True)
CONTROL_URL = "http://mock-upstream:8080"
MEASUREMENTS = []


def control(path, payload=None):
    data = None if payload is None else json.dumps(payload).encode()
    request = urllib.request.Request(CONTROL_URL + path, data=data,
                                     headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=5) as response:
        return json.load(response)


def wait_for(predicate, label, timeout=10):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.01)
    raise AssertionError(f"Timed out: {label}")


def mappings():
    return set(REDIS.scan_iter("pii_mapping:*"))


def state(case_id, phase="provider"):
    return control("/control/requests").get(f"{phase}:{case_id}", {})


def release(case_id, phase="provider", *, once=False):
    control("/control/release", {"id": case_id, "phase": phase, "once": once})


def begin(host, path, extra="", model="mock-chat", *, stream=False, pii=True, choices=1):
    case_id = uuid.uuid4().hex[:16]
    content = f"DISCONNECT_CASE_{case_id} {extra}" + (f" phone {RAW_PHONE}" if pii else " safe content")
    payload = {"model": model, "stream": stream}
    if choices != 1:
        payload["n"] = choices
    if path.endswith("completions"):
        payload["messages"] = [{"role": "user", "content": content}]
    else:
        payload["input"] = content
    before = mappings()
    connection = http.client.HTTPConnection(host, timeout=15)
    connection.request("POST", path, body=json.dumps(payload), headers={
        "Authorization": "Bearer sk-test-master", "Content-Type": "application/json",
    })
    return case_id, connection, before, content


def accepted(case_id, before):
    value = wait_for(lambda: state(case_id), "masked upstream accepted")
    assert not value["saw_raw_phone"] and value["saw_placeholder"], value
    key = wait_for(lambda: next(iter(mappings() - before), None), "server mapping")
    assert len(mappings() - before) == 1
    assert REDIS.ttl(key) > 3500, "Do not shorten the production TTL"
    return key


def disconnect(connection):
    closed_at = time.monotonic()
    try:
        connection.sock.shutdown(socket.SHUT_RDWR)
    except OSError:
        pass  # The peer may already have closed in the completion race.
    connection.close()
    connection.close()
    return closed_at


def cancelled(case_id, key, closed_at, host, path):
    request_id = key.split(":", 1)[1]
    observation = wait_for(
        lambda: REDIS.hgetall(f"disconnect_observation:{request_id}"),
        "stock 499 failure callback", timeout=5,
    )
    deleted_at = wait_for(lambda: time.monotonic() if not REDIS.exists(key) else None,
                          "specific mapping deleted", timeout=5)
    upstream = wait_for(lambda: state(case_id) if state(case_id).get("closed_at") else None,
                        "upstream HTTP socket closed", timeout=5)
    detected_at = float(observation["detected_at"])
    assert deleted_at - detected_at <= 5
    assert upstream["released_at"] is None, "Not a normal provider completion"
    MEASUREMENTS.append({
        "route": host, "api": path,
        "detect_ms": round((detected_at - closed_at) * 1000, 3),
        "delete_after_detect_ms": round((deleted_at - detected_at) * 1000, 3),
        "upstream_close_ms": round((upstream["closed_at"] - closed_at) * 1000, 3),
    })


def success(connection, content):
    response = connection.getresponse()
    body = json.loads(response.read())
    connection.close()
    assert response.status == 200, (response.status, body)
    if "choices" in body:
        actual = body["choices"][0]["message"]["content"]
    else:
        actual = body["output"][0]["content"][0]["text"]
    assert actual == content, "Exact restoration changed"


def run(baseline):
    checks = 0
    fallback_keys = set()
    dependency_failures = []
    for host in ("litellm:4000", "nginx:80"):
        for path in ("/v1/chat/completions", "/v1/responses"):
            case_id, connection, before, content = begin(host, path)
            key = accepted(case_id, before)
            closed_at = disconnect(connection)
            if baseline:
                until = time.monotonic() + 5.1
                while time.monotonic() < until:
                    assert REDIS.exists(key)
                    assert state(case_id)["closed_at"] is None
                    time.sleep(0.01)
                release(case_id)
                wait_for(lambda: not REDIS.exists(key), "normal baseline completion cleanup")
                checks += 1
                continue
            cancelled(case_id, key, closed_at, host, path)
            checks += 1

            # Fail one attempt, then disconnect during the held retry.
            case_id, connection, before, _ = begin(host, path, "LOAD_UPSTREAM_FAIL_429")
            key = accepted(case_id, before)
            release(case_id, once=True)
            wait_for(lambda: state(case_id).get("attempts", 0) >= 2, "retry accepted")
            cancelled(case_id, key, disconnect(connection), host, path)
            checks += 1

            # An unrelated held response must survive a neighbour's cancellation.
            neighbour, live, before, live_content = begin(host, path)
            live_key = accepted(neighbour, before)
            case_id, connection, before, _ = begin(host, path)
            key = accepted(case_id, before)
            cancelled(case_id, key, disconnect(connection), host, path)
            assert REDIS.exists(live_key)
            release(neighbour)
            success(live, live_content)
            wait_for(lambda: not REDIS.exists(live_key), "neighbour success cleanup")
            checks += 1

            # Disconnect while analysis is held: the stock monitor starts after pre-call.
            observations_before = set(REDIS.scan_iter("disconnect_observation:*"))
            case_id, connection, before, _ = begin(host, path, "DISCONNECT_HOLD_ANALYZER")
            wait_for(lambda: state(case_id, "analyzer"), "analysis accepted")
            assert mappings() == before
            disconnect(connection)
            release(case_id, "analyzer")
            observation_key = wait_for(
                lambda: next(iter(set(REDIS.scan_iter("disconnect_observation:*")) - observations_before), None),
                "early disconnect observed as 499",
            )
            wait_for(lambda: REDIS.hget(observation_key, "cleanup_returned_at"),
                     "early failure cleanup returned")
            upstream = state(case_id)
            assert not upstream or upstream["closed_at"] is not None
            wait_for(lambda: mappings() == before, "early cancellation cleanup")
            checks += 1

            # Repeat completion/close races. Either outcome must clean its own mapping.
            for _ in range(5):
                case_id, connection, before, _ = begin(host, path)
                key = accepted(case_id, before)
                release(case_id)
                disconnect(connection)
                wait_for(lambda: not REDIS.exists(key), "completion/disconnect race cleanup", 5)
                checks += 1

            for failure in ("500", "429", "TIMEOUT"):
                case_id, connection, before, _ = begin(
                    host, path, f"LOAD_UPSTREAM_FAIL_{failure}", model="mock-timeout"
                )
                key = accepted(case_id, before)
                release(case_id)
                response = connection.getresponse()
                response.read()
                connection.close()
                expected_status = {"500": 500, "429": 429, "TIMEOUT": 408}[failure]
                assert response.status == expected_status, response.status
                wait_for(lambda: not REDIS.exists(key), "provider failure cleanup", 5)
                if failure != "TIMEOUT":
                    assert state(case_id)["attempts"] >= 2, "Retry path not exercised"
                checks += 1

            # Prove the gateway remains usable after all cancellation/failure paths.
            case_id, connection, before, content = begin(host, path)
            key = accepted(case_id, before)
            release(case_id)
            success(connection, content)
            assert not REDIS.exists(key)
            checks += 1

            # Real Redis write unavailability: preserve the provider error and TTL.
            case_id, connection, before, _ = begin(host, path, "LOAD_UPSTREAM_FAIL_500")
            key = accepted(case_id, before)
            REDIS.client_pause(20000, mode="WRITE")
            started_at = time.monotonic()
            try:
                release(case_id)
                response = connection.getresponse()
                response.read()
                assert response.status == 500, "Redis failure hid the provider error"
                elapsed = time.monotonic() - started_at
                assert elapsed < 15, "Dependency cleanup was not bounded"
                assert REDIS.exists(key) and REDIS.ttl(key) > 3500
                fallback_keys.add(key)
                dependency_failures.append({"route": host, "api": path,
                                            "error_status": response.status,
                                            "wait_seconds": round(elapsed, 3)})
            finally:
                connection.close()
                REDIS.client_unpause()
            checks += 1
    assert mappings() == fallback_keys, "Unexpected leftover request-scoped mappings"
    print(json.dumps({"baseline": baseline, "checks": checks,
                      "measurements": MEASUREMENTS,
                      "redis_write_unavailable": dependency_failures,
                      "ttl_fallback_records": len(fallback_keys)}, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--baseline", action="store_true")
    run(parser.parse_args().baseline)
