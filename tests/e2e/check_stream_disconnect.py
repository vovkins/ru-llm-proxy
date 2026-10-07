"""Actual TCP/SSE disconnect matrix, run in the isolated LiteLLM container."""

import json
import time

from check_nonstream_disconnect import (
    REDIS, RAW_PHONE, accepted, begin, cancelled, disconnect, mappings, release, state, wait_for,
)
from controlled_sse import OPAQUE, PHASES_TEXT, PHASES_TOOL


MEASUREMENTS = []
FAILURES = []
CHECKS = 0


def started(host, path, tool=False, extra="", *, wait_loaded=True):
    marker = "SSE_CONTROLLED " + ("SSE_TOOL " if tool else "") + extra
    case_id, connection, before, _ = begin(host, path, marker, stream=True)
    key = accepted(case_id, before)
    release(case_id)
    wait_for(lambda: state(case_id, "stream_first"), "provider SSE headers sent")
    if wait_loaded:
        wait_for(lambda: REDIS.hget("stream_observation:" + key.split(":", 1)[1], "loaded_at"),
                 "guardrail waiting for first SSE event")
    return case_id, connection, key


def advance(case_id, phases, target):
    for phase in phases:
        wait_for(lambda: state(case_id, phase), "provider held at " + phase)
        if phase == target:
            return
        release(case_id, phase)
    raise AssertionError("Unknown phase " + target)


def events(response):
    result = []
    try:
        while True:
            line = response.readline()
            if not line:
                break
            if line.startswith(b"data: "):
                data = line[6:].strip()
                result.append("[DONE]" if data == b"[DONE]" else json.loads(data))
    except Exception as exc:
        raise AssertionError(f"Invalid client SSE: {type(exc).__name__}") from exc
    return result


def terminal_success(values):
    return any(isinstance(value, dict) and (
        value.get("type") == "response.completed" or any(
            choice.get("finish_reason") is not None for choice in value.get("choices", [])
        )
    ) for value in values)


def restored(values, path, tool):
    if path.endswith("responses"):
        kind = "response.function_call_arguments.delta" if tool else "response.output_text.delta"
        return "".join(value.get("delta", "") for value in values
                       if isinstance(value, dict) and value.get("type") == kind)
    fragments = []
    for value in values:
        if not isinstance(value, dict):
            continue
        for choice in value.get("choices", []):
            delta = choice.get("delta", {})
            if tool:
                fragments.extend(call.get("function", {}).get("arguments", "")
                                 for call in delta.get("tool_calls", []))
            else:
                fragments.append(delta.get("content") or "")
    return "".join(fragments)


def check_complete(connection, path, tool):
    response = connection.getresponse()
    assert response.status == 200, response.status
    assert response.getheader("content-type").startswith("text/event-stream")
    values = events(response)
    connection.close()
    assert terminal_success(values), "Missing real terminal success"
    actual = restored(values, path, tool)
    if tool:
        assert json.loads(actual) == {"phone": RAW_PHONE, "note": 'quote" slash/'}, actual
    else:
        assert actual == "safe " + RAW_PHONE + " tail", actual
    if path.endswith("responses"):
        complete = next(value for value in values if value.get("type") == "response.completed")
        snapshot = complete["response"]
        assert snapshot["output"][1]["encrypted_content"] == OPAQUE
        assert snapshot["usage"]["total_tokens"] == 2
        sequence = [value["sequence_number"] for value in values if "sequence_number" in value]
        assert sequence == sorted(set(sequence)), sequence
        item = snapshot["output"][0]
        if tool:
            assert json.loads(item["arguments"]) == json.loads(actual)
        else:
            assert item["content"][0]["text"] == actual
    assert "<PHONE_" not in actual


def finish(case_id, phases):
    for phase in phases:
        wait_for(lambda: state(case_id, phase), "completion phase " + phase)
        release(case_id, phase)


def check_cancel(host, path, tool, phase):
    phases = PHASES_TOOL if tool else PHASES_TEXT
    case_id, connection, key = started(host, path, tool)
    try:
        advance(case_id, phases, phase)
        if phase != "stream_first":
            response = connection.getresponse()
            assert response.status == 200
            assert response.getheader("content-type").startswith("text/event-stream")
            # The provider's hidden partial placeholder need not reach the client.
            first = response.readline()
            assert first, "No first SSE record delivered"
        closed_at = disconnect(connection)
        upstream = wait_for(lambda: state(case_id, phase).get("closed_at"), "upstream EOF", 5)
        wait_for(lambda: not REDIS.exists(key), "specific stream mapping deleted", 5)
        observation = REDIS.hgetall("stream_observation:" + key.split(":", 1)[1])
        assert observation.get("deleted_at"), observation
        assert state(case_id, phase)["released_at"] is None
        MEASUREMENTS.append({"route": host, "api": path, "tool": tool, "phase": phase,
                             "upstream_close_ms": round((upstream - closed_at) * 1000, 3),
                             "cleanup_entry_ms": round((float(observation["delete_entered_at"]) - closed_at) * 1000, 3),
                             "cleanup_duration_ms": round((float(observation["deleted_at"]) - float(observation["delete_entered_at"])) * 1000, 3),
                             "delete_ms": round((float(observation["deleted_at"]) - closed_at) * 1000, 3)})
    finally:
        connection.close()
        # Release only after assertions, so a failed scenario cannot stall later ones.
        for boundary in phases:
            release(case_id, boundary)


def check_provider(host, path, tool, phase=None, error=False):
    phases = PHASES_TOOL if tool else PHASES_TEXT
    extra = "SSE_ERROR" if error else ("SSE_EOF_" + phase.upper() if phase else "")
    case_id, connection, key = started(host, path, tool, extra)
    try:
        for boundary in phases:
            wait_for(lambda: state(case_id, boundary), "provider phase " + boundary)
            release(case_id, boundary)
            if boundary == phase:
                break
        if not phase and not error:
            check_complete(connection, path, tool)
        else:
            response = connection.getresponse()
            values = events(response)
            wait_for(lambda: not REDIS.exists(key), "provider-end mapping cleanup", 5)
            assert not terminal_success(values), "Premature EOF/error became terminal success"
            actual = restored(values, path, tool)
            assert "<PHONE_" not in actual, "Pending placeholder was flushed after abort"
            if error:
                assert any(isinstance(value, dict) and (
                    "error" in value or value.get("type") in {"error", "response.failed"}
                ) for value in values), "No visible SSE error"
        wait_for(lambda: not REDIS.exists(key), "provider-end mapping cleanup", 5)
    finally:
        connection.close()
        for boundary in phases:
            release(case_id, boundary)


def check_neighbour(host, path):
    neighbour, live, live_key = started(host, path)
    try:
        check_cancel(host, path, False, "stream_placeholder")
        assert REDIS.exists(live_key), "Neighbour mapping was removed"
        finish(neighbour, PHASES_TEXT)
        check_complete(live, path, False)
        wait_for(lambda: not REDIS.exists(live_key), "neighbour normal cleanup", 5)
    finally:
        live.close()
        for phase in PHASES_TEXT:
            release(neighbour, phase)


def check_before_headers(host, path):
    case_id, connection, before, _ = begin(host, path, "SSE_CONTROLLED", stream=True)
    key = accepted(case_id, before)
    try:
        cancelled(case_id, key, disconnect(connection), host, path)
    finally:
        connection.close()
        release(case_id)


def check_early_race(host, path, tool):
    case_id, connection, key = started(host, path, tool, wait_loaded=False)
    try:
        disconnect(connection)
        wait_for(lambda: state(case_id, "stream_first").get("closed_at"),
                 "early-race upstream EOF", 5)
        wait_for(lambda: not REDIS.exists(key), "early-race mapping deletion", 5)
        assert state(case_id, "stream_first")["released_at"] is None
    except AssertionError as exc:
        request_id = key.split(":", 1)[1]
        observation = REDIS.hgetall("stream_observation:" + request_id)
        evidence = {
            "mapping_exists": bool(REDIS.exists(key)),
            "loaded": bool(observation.get("loaded_at")),
            "delete_entered": bool(observation.get("delete_entered_at")),
            "deleted": bool(observation.get("deleted_at")),
            "failure_499": bool(REDIS.exists("disconnect_observation:" + request_id)),
            "upstream_closed": bool(state(case_id, "stream_first").get("closed_at")),
        }
        raise AssertionError(f"{exc}; evidence={json.dumps(evidence)}") from exc
    finally:
        connection.close()
        release(case_id, "stream_first")



def run_case(label, operation):
    global CHECKS
    CHECKS += 1
    try:
        operation()
        print("PASS " + label, flush=True)
    except Exception as exc:
        failure = {"case": label, "type": type(exc).__name__, "message": str(exc)}
        FAILURES.append(failure)
        print("FAIL " + json.dumps(failure), flush=True)


def run():
    # The preceding Redis-write failure controls intentionally leave TTL records.
    initial_mappings = mappings()
    for host in ("litellm:4000", "nginx:80"):
        for path in ("/v1/chat/completions", "/v1/responses"):
            label = host + " " + path
            run_case(label + " cancel before provider headers", lambda: check_before_headers(host, path))
            for tool in (False, True):
                phases = PHASES_TOOL if tool else PHASES_TEXT
                for phase in phases:
                    run_case(f"{label} cancel tool={tool} {phase}",
                             lambda: check_cancel(host, path, tool, phase))
                run_case(f"{label} success tool={tool}", lambda: check_provider(host, path, tool))
                for phase in (phases[2], "stream_partial"):
                    run_case(f"{label} EOF tool={tool} {phase}",
                             lambda: check_provider(host, path, tool, phase))
                run_case(f"{label} error tool={tool}", lambda: check_provider(host, path, tool, error=True))
                for repeat in range(5):
                    run_case(f"{label} early race tool={tool} repeat={repeat}",
                             lambda: check_early_race(host, path, tool))
            run_case(label + " neighbour", lambda: check_neighbour(host, path))
            for repeat in range(5):
                def race():
                    case_id, connection, key = started(host, path)
                    advance(case_id, PHASES_TEXT, "stream_terminal")
                    release(case_id, "stream_terminal")
                    disconnect(connection)
                    wait_for(lambda: not REDIS.exists(key), "terminal/cancel race cleanup", 5)
                run_case(label + f" race {repeat}", race)
            run_case(label + " next success", lambda: check_provider(host, path, False))
    print(json.dumps({"checks": CHECKS, "failures": FAILURES, "measurements": MEASUREMENTS,
                      "leftover_mappings": len(mappings() - initial_mappings),
                      "preserved_initial_mappings": len(mappings() & initial_mappings)}, indent=2), flush=True)
    assert not FAILURES and mappings() == initial_mappings, "SSE matrix failed; see per-case evidence"


if __name__ == "__main__":
    run()
