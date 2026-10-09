"""Controlled HTTP time budgets, without model calls or slow real inference."""

import json
import time

from check_nonstream_disconnect import begin, disconnect, mappings, release, state, wait_for


def run():
    measurements = []
    for host in ("localhost:4000", "nginx:80"):
        for path in ("/v1/chat/completions", "/v1/responses"):
            for stream in (False, True):
                case_id, connection, before, _ = begin(
                    host, path, "DISCONNECT_HOLD_ANALYZER", stream=stream,
                )
                try:
                    wait_for(lambda: state(case_id, "analyzer"), "analysis accepted")
                    started = time.monotonic()
                    response = connection.getresponse()
                    payload = json.loads(response.read())
                    elapsed = time.monotonic() - started
                    assert response.status == 503, (response.status, payload)
                    assert not response.getheader("Retry-After"), "A blind retry cannot fix a time budget"
                    assert "pii_preprocessing_timeout" in json.dumps(payload), payload
                    details = payload["error"]["param"]
                    if isinstance(details, str):
                        details = json.loads(details)
                    assert details["preprocessing"]["details"]["retryable"] is False, payload
                    assert not state(case_id), "Provider received a timed-out request"
                    assert mappings() == before
                    wait_for(lambda: state(case_id, "analyzer").get("closed_at"),
                             "analysis HTTP connection closed", 5)
                    assert state(case_id, "analyzer")["attempts"] == 1
                    assert elapsed < 6, elapsed
                    measurements.append({"route": host, "api": path, "stream": stream,
                                         "status": response.status, "wait_seconds": round(elapsed, 3)})
                finally:
                    connection.close()
                    release(case_id, "analyzer")

                # Stock LiteLLM starts disconnect monitoring after pre-call. Until
                # then the budget must still close abandoned Analyzer work.
                case_id, connection, before, _ = begin(
                    host, path, "DISCONNECT_HOLD_ANALYZER", stream=stream,
                )
                try:
                    wait_for(lambda: state(case_id, "analyzer"), "analysis accepted")
                    abandoned_at = disconnect(connection)
                    time.sleep(0.2)
                    assert not state(case_id, "analyzer").get("closed_at")
                    ended = wait_for(lambda: state(case_id, "analyzer").get("closed_at"),
                                     "abandoned analysis closed by budget", 6)
                    assert ended - abandoned_at < 6
                    assert state(case_id, "analyzer")["attempts"] == 1
                    assert not state(case_id), "Provider received an abandoned timed-out request"
                    assert mappings() == before
                    measurements.append({"route": host, "api": path, "stream": stream,
                                         "case": "early_disconnect",
                                         "stop_seconds": round(ended - abandoned_at, 3)})
                finally:
                    connection.close()
                    release(case_id, "analyzer")

    # A short checked request remains usable after repeated controlled refusals.
    case_id, connection, before, _ = begin("nginx:80", "/v1/chat/completions")
    try:
        wait_for(lambda: state(case_id), "short provider call")
        release(case_id)
        response = connection.getresponse()
        assert response.status == 200, response.status
        response.read()
        wait_for(lambda: mappings() == before, "short request cleanup")
    finally:
        connection.close()
        release(case_id)
    print(json.dumps({"checks": len(measurements) + 1, "measurements": measurements}))


if __name__ == "__main__":
    run()
