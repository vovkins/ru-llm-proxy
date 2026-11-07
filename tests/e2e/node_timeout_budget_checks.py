"""Bounded timeouts: stock LiteLLM, real guardrail and disposable Redis."""

import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
import uuid

from ner_test_support import ROOT, image_id, wait_ready
from node_routing_checks import call, check_node_responses, output


def check_budget(network, client, base, control, stats, *, platform_image=None):
    project = "ru-node-budget-" + uuid.uuid4().hex[:8]
    environment = dict(os.environ, PRE_EGRESS_PROXY_PORT="127.0.0.1:0")
    results = []
    # Reuse the already built platform; only our checked-out guardrail is mounted.
    image = image_id(platform_image or "ru-llm-proxy-ner-tests:litellm")
    with tempfile.TemporaryDirectory(prefix="ru-node-budget-") as temp:
        directory = Path(temp)
        config = directory / "config.json"
        config.write_text(json.dumps({
            "model_list": [{"model_name": "mock-node", "litellm_params": {
                "model": "openai/mock-chat", "api_base": "http://mock-upstream:8080/v1",
                "api_key": "os.environ/OPENAI_API_KEY", "max_retries": 0}}],
            "guardrails": [{"guardrail_name": "node-" + mode, "litellm_params": {
                "guardrail": "disconnect_observer.ObservedGuardrail", "mode": mode + "_call", "default_on": True}}
                for mode in ("pre", "post")],
            "general_settings": {"master_key": "os.environ/LITELLM_MASTER_KEY", "cancel_on_disconnect": True},
            "router_settings": {"num_retries": 0},
            "litellm_settings": {"callbacks": ["litellm_guardrails.upstream_stream_adapter"]},
        }))
        overlay = directory / "compose.json"
        overlay.write_text(json.dumps({"services": {
            "litellm": {"image": image, "pull_policy": "never", "environment": {
                "PRESIDIO_ANALYZER_URL": "http://nginx:5001", "PII_GUARDRAIL_ANALYZER_TIMEOUT_SECONDS": "3",
                "PYTHONPATH": "/app:/workspace/tests/e2e"},
                "volumes": [f"{ROOT}:/workspace:ro", f"{config}:/app/config.yaml:ro"]},
            "mock-upstream": {"environment": {"MOCK_ECHO_CHAT_CONTENT": "true", "MOCK_ECHO_RESPONSES_CONTENT": "true"}},
        }, "networks": {"default": {"external": True, "name": network}}}))
        command = ["docker", "compose", "-p", project, "-f",
                   str(ROOT / "tests/e2e/docker-compose.pre-egress-proxy.yml"), "-f", str(overlay)]

        def compose(*args):
            result = subprocess.run(command + list(args), env=environment, capture_output=True, text=True, timeout=180)
            if result.returncode:
                raise RuntimeError("Disposable budget Compose operation failed: " + args[0])
            return result.stdout.strip()

        def redis(*args):
            return compose("exec", "-T", "redis", "redis-cli", "--raw", *args)

        def observations():
            keys = redis("KEYS", "preprocessing_observation:*").split()
            return {key: redis("HGET", key, "client_disconnected") for key in keys}

        def provider():
            return call(worker, "http://mock-upstream:8080/capture")["body"]["provider_requests"]

        def payload(api, stream):
            text = "synthetic controlled node timeout " + uuid.uuid4().hex
            value = {"model": "mock-node", "stream": stream}
            value.update({"messages": [{"role": "user", "content": text}]} if api == "chat/completions" else {"input": text})
            return value

        try:
            compose("up", "-d", "--no-build", "--wait", "--wait-timeout", "120")
            worker = compose("ps", "-q", "litellm")
            front = base.removesuffix(":5001") + ":80"

            def ready():
                try:
                    return call(client, front + "/health/liveliness", timeout=3)["status"] == 200
                except (RuntimeError, subprocess.SubprocessError):
                    return False

            wait_ready(ready, 120)
            headers = {"Authorization": "Bearer sk-test-master"}
            control("ready", "ready")
            response = call(client, front + "/v1/chat/completions", payload("chat/completions", False), headers)
            assert response["status"] == 200, response
            assert provider() == 1
            call(worker, "http://mock-upstream:8080/capture/reset", {})

            for api in ("chat/completions", "responses"):
                for stream in (False, True):
                    for cancel in (False, True):
                        control("delay_first", "delay_first", delay=7)
                        before = observations()
                        began = time.monotonic()
                        try:
                            response = call(client, front + "/v1/" + api, payload(api, stream), headers,
                                            timeout=1 if cancel else 12)
                        except RuntimeError:
                            if not cancel:
                                raise
                        else:
                            assert not cancel, "Expected client disconnect"
                            assert response["status"] == 503, response
                            assert "pii_preprocessing_timeout" in json.dumps(response["body"]), response
                            assert response["retry_after"] is None, response
                        elapsed = time.monotonic() - began
                        assert elapsed < (2.5 if cancel else 6), elapsed

                        def cleaned():
                            found = {key: value for key, value in observations().items() if key not in before}
                            return found if len(found) == 1 and list(found.values()) == ["1" if cancel else "0"] else None

                        wait_ready(cleaned, 1.5 if cancel else 3)
                        assert redis("KEYS", "pii_mapping:*") == ""
                        time.sleep(8)
                        records = [row for snapshot in stats() for row in snapshot["calls"]]
                        assert len(records) == 1, len(records)
                        assert 0 < float(records[0]["budget"]) <= 3, records
                        assert provider() == 0, "Provider received a failed preprocessing request"
                        results.append({"api": api, "stream": stream, "case": "disconnect" if cancel else "budget",
                                        "elapsed_seconds": round(elapsed, 3), "analyzer_posts": 1,
                                        "provider_requests": 0, "mapping_cleanup": True})
            # The first transport/502 attempt consumes ~2s. The remaining
            # budget is not renewed when the central route selects peer two.
            control("delayed_bad_gateway", "delayed_bad_gateway", delay=2)
            began = time.monotonic()
            response = call(client, front + "/v1/chat/completions", payload("chat/completions", False), headers, timeout=10)
            elapsed = time.monotonic() - began
            assert response["status"] == 503 and "pii_preprocessing_timeout" in json.dumps(response["body"]), response
            assert elapsed < 4, elapsed
            time.sleep(5)
            assert [len(row["calls"]) for row in stats()] == [1, 1]
            assert provider() == 0 and redis("KEYS", "pii_mapping:*") == ""
            results.append({"case": "shared_budget_across_two_attempts", "elapsed_seconds": round(elapsed, 3),
                            "analyzer_posts": 2, "provider_requests": 0})
            control("ready", "ready")
            assert call(client, front + "/v1/chat/completions", payload("chat/completions", False), headers)["status"] == 200
            assert provider() == 1
            return {"status": "passed", "test_budget_seconds": 3, "nginx_read_timeout_seconds": 1500,
                    "checks": results, "recovery": "passed", "platform_image": image}
        except BaseException:
            path = Path(tempfile.mkdtemp(prefix="ru-node-budget-diagnostic-")) / "services.log"
            logs = subprocess.run(command + ["logs", "--tail", "100"], env=environment, capture_output=True, timeout=30)
            path.write_bytes(logs.stdout + logs.stderr)
            path.chmod(0o600)
            print("Private budget diagnostics:", path.parent, flush=True)
            raise
        finally:
            compose("down", "-v", "--remove-orphans")
            for kind in ("container", "volume"):
                assert not output(kind, "ls", "-aq" if kind == "container" else "-q", "--filter",
                                  "label=com.docker.compose.project=" + project)


if __name__ == "__main__":
    result = check_node_responses(budget_probe=check_budget)
    print(json.dumps(result, indent=2))
