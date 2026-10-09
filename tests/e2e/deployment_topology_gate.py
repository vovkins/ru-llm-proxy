#!/usr/bin/env python3
"""Isolated deployment gate: real NER, disposable DBs and mock provider."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
from deployment import Context, memory_bytes  # noqa: E402


MASTER = "sk-topology-test-only"
PHONE = "+79031234567"
OTHER_PHONE = "+79031234568"


def docker(*args):
    return subprocess.check_output(["docker", *args], text=True, stderr=subprocess.DEVNULL).strip()


def request(url, path, payload=None, token=MASTER, headers=False):
    req = urllib.request.Request(url + path, data=None if payload is None else json.dumps(payload).encode(), headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=90) as response:
            result = response.status, response.read().decode()
            return (*result, response.headers) if headers else result
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode()


def assert_request(url, mode, api, stream, token, phone=PHONE):
    text = f"Телефон клиента {phone}."
    payload = {"model": "mock-chat" if api == "chat/completions" else "mock-responses", "stream": stream}
    payload.update({"messages": [{"role": "user", "content": text}]} if api == "chat/completions" else {"input": text})
    status, body = request(url, "/v1/" + api, payload, token)
    if mode == "block":
        assert status == 422, (status, body[:300])
        assert phone not in body, "Blocked error disclosed PII"
    else:
        assert status == 200, (status, body[:300])
        assert phone in body, "Original value was not restored"
        assert (OTHER_PHONE if phone == PHONE else PHONE) not in body, "Cross-user mapping leaked"
        assert "<PII_" not in body, "Placeholder escaped to client"
        if stream:
            assert "data:" in body, "Expected SSE response"


class Gate:
    def __init__(self, directory):
        self.directory = Path(directory)
        self.project = "topology-gate-" + uuid.uuid4().hex[:8]
        self.env_file = self.directory / ".env"
        self.env_file.write_text("\n".join([
            f"LITELLM_MASTER_KEY={MASTER}", "LITELLM_SALT_KEY=topology-salt-only",
            "UI_USERNAME=admin", "UI_PASSWORD=topology-ui-only", "POSTGRES_PASSWORD=topology-db-only",
            "LITELLM_DB_URL=postgresql://litellm:topology-db-only@db:5432/litellm",
            "ZAI_API_KEY=test-only", "ZAI_API_KEY_2=test-only", "CODEX_LB_POSTGRES_PASSWORD=topology-codex-db-only",
            "CODEX_LB_API_KEY=sk-test-only", "PII_GUARDRAIL_MODE=mask", "NGINX_HTTP_PORT=0",
            "LITELLM_PORT=0", "CODEX_LB_PORT=0", "CODEX_LB_METRICS_PORT=0", "",
        ]))
        self.env_file.chmod(0o600)
        self.original = self.secrets()
        self.overlay = self.directory / "test.json"
        self.ports_reset = self.directory / "ports-reset.yml"
        self.stop = threading.Event()
        self.peak_memory = {}
        self.result = {"cases": [], "checks": [], "peak_container_memory_bytes": self.peak_memory, "peak_total_memory_bytes": 0}
        self.last_context = None

    def secrets(self):
        return [line for line in self.env_file.read_text().splitlines() if not line.startswith("PII_GUARDRAIL_MODE=")]

    def context(self, stack, topology, mode, resources_file=None):
        os.environ.update(COMPOSE_PROJECT_NAME=self.project, STACK=stack, TOPOLOGY=topology, PII_GUARDRAIL_MODE=mode, PII_MODE_ORIGIN="command line", STACK_START_TIMEOUT="360")
        for key in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "TOPOLOGY_ENV", "ANALYZER_PROFILE"):
            os.environ.pop(key, None)
        if resources_file:
            os.environ["TOPOLOGY_ENV"] = str(resources_file)
        services = {service: {"container_name": f"{self.project}-{service}"} for service in ("nginx", "db", "redis", "litellm", "presidio-analyzer", "codex-lb", "codex-lb-db")}
        if topology == "production":
            for service in ("litellm", "presidio-analyzer"):
                services[service].pop("container_name")
        if stack == "litellm-presidio":
            services.pop("codex-lb")
            services.pop("codex-lb-db")
        services["litellm"].update(image="ru-llm-proxy-litellm:latest", pull_policy="never", volumes=[f"{ROOT}/tests/e2e/litellm-config.topologies.yaml:/app/config.yaml:ro"], environment={"OPENAI_API_KEY": "test-only", "PII_GUARDRAIL_ANALYZER_TIMEOUT_SECONDS": "5", "PII_GUARDRAIL_ANALYZER_CONNECT_TIMEOUT_SECONDS": "1"})
        services["presidio-analyzer"].update(image="ru-llm-proxy-presidio-analyzer:latest", pull_policy="never")
        for service in services:
            services[service]["healthcheck"] = {"interval": "3s"}
        services["nginx"]["ports"] = ["127.0.0.1:0:80"]
        if topology == "production":
            services["nginx"]["ports"] += ["127.0.0.1:0:4000", "127.0.0.1:0:5001"]
        if stack.endswith("codex-lb"):
            services["nginx"]["ports"].append("127.0.0.1:0:2455")
        services["mock-upstream"] = {"image": "python:3.12-slim", "command": ["python", "/mock.py"], "volumes": [f"{ROOT}/tests/e2e/mock_openai_upstream.py:/mock.py:ro"], "environment": {"MOCK_ECHO_CHAT_CONTENT": "true", "MOCK_ECHO_RESPONSES_CONTENT": "true"}, "networks": ["ru-llm-proxy"], "ports": ["127.0.0.1:0:8080"], "labels": {"ru-llm-proxy.topology": topology}}
        if stack.endswith("codex-lb"):
            services["codex-lb"].update(image="ru-llm-proxy-codex-lb:latest", pull_policy="never")
        self.overlay.write_text(json.dumps({"services": services}))
        self.ports_reset.write_text("services:\n" + "".join(f"  {service}:\n    ports: !reset []\n" for service in services if service != "mock-upstream"))
        ctx = Context(env_file=str(self.env_file))
        ctx.files.extend([str(self.ports_reset), str(self.overlay)])
        self.last_context = ctx
        return ctx

    def url(self, ctx, service, port):
        address = ctx.compose("port", service, str(port), capture=True).strip().splitlines()[0]
        return "http://" + address

    def sample_memory(self):
        while not self.stop.is_set():
            ids = docker("ps", "-q", "--filter", f"label=com.docker.compose.project={self.project}").split()
            if ids:
                try:
                    rows = docker("stats", "--no-stream", "--format", "{{json .}}", *ids)
                    total = 0
                    for line in rows.splitlines():
                        row = json.loads(line)
                        used = memory_bytes(row["MemUsage"].split("/")[0].replace(" ", ""))
                        name = row["Name"]
                        self.peak_memory[name] = max(used, self.peak_memory.get(name, 0))
                        total += used
                    self.result["peak_total_memory_bytes"] = max(total, self.result["peak_total_memory_bytes"])
                except (subprocess.CalledProcessError, ValueError, RuntimeError):
                    pass
            self.stop.wait(2)

    def redis(self, ctx, *args):
        return ctx.compose("exec", "-T", "redis", "redis-cli", "--raw", *args, capture=True).strip()

    def verify_state(self, ctx, first=False):
        if first:
            self.redis(ctx, "SET", "topology-gate:sentinel", "preserved")
            ctx.compose("exec", "-T", "db", "psql", "-U", "litellm", "-c", "CREATE TABLE topology_gate (value text); INSERT INTO topology_gate VALUES ('preserved');", capture=True)
        assert self.redis(ctx, "GET", "topology-gate:sentinel") == "preserved", "Redis state lost during switch"
        assert "preserved" in ctx.compose("exec", "-T", "db", "psql", "-U", "litellm", "-Atc", "SELECT value FROM topology_gate;", capture=True), "PostgreSQL state lost during switch"
        if ctx.extended:
            if first:
                ctx.compose("exec", "-T", "codex-lb-db", "psql", "-U", "codex_lb", "-c", "CREATE TABLE topology_gate (value text); INSERT INTO topology_gate VALUES ('preserved');", capture=True)
                ctx.compose("exec", "-T", "codex-lb", "python", "-c", "from pathlib import Path; Path('/var/lib/codex-lb/topology-gate-sentinel').write_text('preserved')", capture=True)
            assert "preserved" in ctx.compose("exec", "-T", "codex-lb-db", "psql", "-U", "codex_lb", "-Atc", "SELECT value FROM topology_gate;", capture=True), "codex-lb PostgreSQL state lost during switch"
            assert ctx.compose("exec", "-T", "codex-lb", "cat", "/var/lib/codex-lb/topology-gate-sentinel", capture=True) == "preserved", "codex-lb data volume lost during switch"
        assert self.secrets() == self.original, "Secrets changed during switch"
        info = ctx.compose("exec", "-T", "redis", "cat", "/proc/meminfo", capture=True)
        self.result.setdefault("vm_memory_samples", []).append({line.split(":")[0]: line.split(":")[1].strip() for line in info.splitlines() if line.startswith(("MemTotal:", "MemAvailable:", "SwapTotal:", "SwapFree:"))})
        for item in ctx.containers():
            assert not item["State"].get("OOMKilled"), "Container was OOM-killed"
            assert item["RestartCount"] == 0, "Unexpected restart"
            service = item["Config"]["Labels"].get("com.docker.compose.service")
            if ctx.topology == "production" and service in ctx.counts:
                configured = ctx.model["services"][service]
                assert item["HostConfig"]["Memory"] == int(configured["mem_limit"]), f"{service}: memory {item['HostConfig']['Memory']} != {configured['mem_limit']}"
                assert item["HostConfig"]["NanoCpus"] == int(float(configured["cpus"]) * 1e9), f"{service}: CPU {item['HostConfig']['NanoCpus']} != {configured['cpus']}"

    def replica_request(self, ctx, item, payload, token, api="chat/completions"):
        mock = next(entry for entry in ctx.containers() if entry["Config"]["Labels"].get("com.docker.compose.service") == "mock-upstream")
        shared = set(item["NetworkSettings"]["Networks"]) & set(mock["NetworkSettings"]["Networks"])
        address = item["NetworkSettings"]["Networks"][next(iter(shared))]["IPAddress"]
        code = "import json,sys,urllib.request,urllib.error; data=json.load(sys.stdin); req=urllib.request.Request(data['url'], data=json.dumps(data['payload']).encode(), headers={'Authorization':'Bearer '+data['token'],'Content-Type':'application/json'});\ntry:\n r=urllib.request.urlopen(req,timeout=30); print(json.dumps({'status':r.status,'body':r.read().decode()}))\nexcept urllib.error.HTTPError as e:\n print(json.dumps({'status':e.code,'body':e.read().decode()}))"
        result = subprocess.run(["docker", "exec", "-i", mock["Id"], "python", "-c", code], input=json.dumps({"url": f"http://{address}:4000/v1/{api}", "payload": payload, "token": token}), text=True, capture_output=True)
        if result.returncode:
            raise RuntimeError(f"Per-replica test connection failed: {result.stderr[-500:]}")
        return json.loads(result.stdout)

    def metrics(self, item, port):
        return docker("exec", item["Id"], "python", "-c", f"import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:{port}/metrics', timeout=10).read().decode())")

    def cache_hits(self, item):
        prefix = 'ru_pii_guardrail_analysis_cache_requests_total{result="hit"} '
        return sum(float(line[len(prefix):]) for line in self.metrics(item, 4000).splitlines() if line.startswith(prefix))

    def responses_history(self, ctx, token):
        from check_responses_history import decode

        replicas = [item for item in ctx.containers() if item["Config"]["Labels"].get("com.docker.compose.service") == "litellm"]
        assert len(replicas) >= 2
        for stream in (False, True):
            previous = None
            for index, (text, expected) in enumerate([
                (PHONE, [PHONE]), (OTHER_PHONE, [PHONE, OTHER_PHONE]),
                ("Repeat both phones", [PHONE, OTHER_PHONE]),
            ]):
                payload = {"model": "mock-responses", "input": text, "stream": stream,
                           "metadata": {"test_history": "STATEFUL_HISTORY"}}
                if previous:
                    payload["previous_response_id"] = previous
                result = self.replica_request(ctx, replicas[index % 2], payload, token, "responses")
                assert result["status"] == 200, result
                previous, restored = decode(result["body"], stream)
                assert all(value in restored for value in expected), restored
                assert "<PHONE_NUMBER_" not in restored, restored
        self.result["checks"].append(
            f"{ctx.stack}: Responses history restored across two LiteLLM processes, both stream modes"
        )

    def exercise(self, ctx, token, other_token):
        url = self.url(ctx, "nginx", 80)
        mock = self.url(ctx, "mock-upstream", 8080)
        request(mock, "/capture/reset", {})
        for api in ("chat/completions", "responses"):
            for stream in (False, True):
                assert_request(url, ctx.mode, api, stream, token)
        capture = json.loads(request(mock, "/capture")[1])
        assert capture["provider_requests"] == (0 if ctx.mode == "block" else 4), capture
        assert capture["provider_saw_raw_phone"] is False
        if ctx.mode == "mask":
            assert capture["provider_saw_pii_placeholder"] is True
            with ThreadPoolExecutor(max_workers=2) as pool:
                futures = [pool.submit(assert_request, url, "mask", "responses", True, key, phone) for key, phone in ((token, PHONE), (other_token, OTHER_PHONE))]
                for future in futures:
                    future.result()
            if ctx.topology == "production":
                self.responses_history(ctx, token)
        payload = {"model": "mock-chat", "messages": [{"role": "user", "content": f"Телефон клиента {PHONE}."}]}
        assert self.redis(ctx, "KEYS", "pii_analysis_cache:v1:*")
        for item in ctx.containers():
            if item["Config"]["Labels"].get("com.docker.compose.service") == "litellm":
                hits = self.cache_hits(item)
                result = self.replica_request(ctx, item, payload, token)
                assert result["status"] == (422 if ctx.mode == "block" else 200)
                assert (PHONE in result["body"]) == (ctx.mode == "mask")
                assert self.cache_hits(item) > hits, "Replica did not reuse shared analysis cache"
        assert self.redis(ctx, "KEYS", "pii_mapping:*") == "", "Mapping not cleaned after response"
        for item in ctx.containers():
            service = item["Config"]["Labels"].get("com.docker.compose.service")
            if service in {"litellm", "presidio-analyzer"}:
                port = 4000 if service == "litellm" else 5001
                metrics = self.metrics(item, port)
                assert "ru_pii_guardrail_" in metrics if service == "litellm" else "ru_presidio_analyzer_" in metrics
        self.result["cases"].append({"stack": ctx.stack, "topology": ctx.topology, "mode": ctx.mode, "replicas": ctx.counts, "status": "passed"})
        print("CASE PASSED", ctx.stack, ctx.topology, ctx.mode, flush=True)

    def resilience(self, ctx, token):
        url = self.url(ctx, "nginx", 80)
        lite = [item for item in ctx.containers() if item["Config"]["Labels"].get("com.docker.compose.service") == "litellm"]
        docker("stop", lite[0]["Id"])
        time.sleep(2)
        assert_request(url, "mask", "responses", True, token)
        ctx.start()
        url = self.url(ctx, "nginx", 80)
        analyzers = [item for item in ctx.containers() if item["Config"]["Labels"].get("com.docker.compose.service") == "presidio-analyzer"]
        docker("stop", *(item["Id"] for item in analyzers))
        mock = self.url(ctx, "mock-upstream", 8080)
        request(mock, "/capture/reset", {})
        status, body = request(url, "/v1/chat/completions", {"model": "mock-chat", "messages": [{"role": "user", "content": "Телефон другого клиента +79031234569."}]}, token)
        assert status == 503 and "+79031234569" not in body
        assert json.loads(request(mock, "/capture")[1])["provider_requests"] == 0
        before_restart = {item["Id"] for item in ctx.containers() if item["Config"]["Labels"].get("com.docker.compose.service") in ctx.counts}
        ctx.start(force=True)
        assert not before_restart & {item["Id"] for item in ctx.containers()}, "Forced restart did not recreate all managed containers"
        self.verify_state(ctx)
        self.result["checks"].append("New SSE request succeeds with one LiteLLM replica stopped; all Analyzer unavailable: 503 without provider call; full sequential restart preserves shared state")
        custom = self.directory / "resources.env"
        custom.write_text("LITELLM_REPLICAS=3\nANALYZER_REPLICAS=2\n")
        scaled = self.context(ctx.stack, "production", "mask", custom)
        scaled.start()
        self.verify_state(scaled)
        assert_request(self.url(scaled, "nginx", 80), "mask", "responses", True, token)
        ctx = self.context(ctx.stack, "production", "mask")
        ctx.start()
        self.verify_state(ctx)
        self.result["checks"].append("Scale 2/4 -> 3/2 -> 2/4: state and virtual key preserved, changed Docker DNS pools serve requests")
        return ctx

    def run(self):
        monitor = threading.Thread(target=self.sample_memory, daemon=True)
        monitor.start()
        try:
            for stack in ("litellm-presidio", "litellm-presidio-codex-lb"):
                token = None
                affinity = None
                for topology, mode in (("functional", "mask"), ("production", "mask"), ("production", "block"), ("functional", "block")):
                    ctx = self.context(stack, topology, mode)
                    ctx.compose("up", "-d", "--no-build", "--no-deps", "mock-upstream")
                    ctx.start()
                    self.verify_state(ctx, first=token is None)
                    url = self.url(ctx, "nginx", 80)
                    if token is None:
                        status, body = request(url, "/key/generate", {"models": ["mock-chat", "mock-responses"], "key_alias": "topology-gate"})
                        assert status == 200, (status, body[:200])
                        token = json.loads(body)["key"]
                        status, body = request(url, "/key/generate", {"models": ["mock-chat", "mock-responses"], "key_alias": "topology-gate-other"})
                        assert status == 200
                        other_token = json.loads(body)["key"]
                    status, body, headers = request(url, "/v1/chat/completions", {"model": "mock-chat", "messages": [{"role": "user", "content": "ok"}]}, token, headers=True)
                    assert status == 200
                    current = headers.get("x-litellm-model-id")
                    assert current and (affinity is None or affinity == current), "Affinity changed after topology/policy switch"
                    affinity = current
                    assert self.redis(ctx, "KEYS", "deployment_affinity:v1:*")
                    if ctx.extended and topology == "production" and mode == "mask":
                        ctx = self.resilience(ctx, token)
                    self.exercise(ctx, token, other_token)
                    if ctx.extended:
                        assert request(self.url(ctx, "nginx", 2455), "/health/ready")[0] == 200
                ctx.compose("down", "-v", "--remove-orphans")
            self.result["checks"].append("Both topologies, both policies, both stacks, both APIs/stream modes; state/secrets, shared analysis cache hits, limits, per-replica readiness/metrics and mapping cleanup")
        finally:
            self.stop.set()
            monitor.join(timeout=15)
            if self.last_context:
                self.result["final_containers"] = [{"name": item["Name"].lstrip("/"), "oom_killed": item["State"].get("OOMKilled"), "restarts": item["RestartCount"], "health": item["State"].get("Health", {}).get("Status")} for item in self.last_context.containers()]
                self.last_context.compose("down", "-v", "--remove-orphans")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="ru-proxy-topologies-") as directory:
        gate = Gate(directory)
        try:
            gate.run()
            gate.result["status"] = "passed"
            print(f"Topology gate passed: {len(gate.result['cases'])} cases; sampled peak container memory {gate.result['peak_total_memory_bytes'] / 1024**3:.2f} GiB", flush=True)
        except BaseException as exc:
            gate.result.update(status="failed", error=str(exc))
            raise
        finally:
            if args.report:
                args.report.write_text(json.dumps(gate.result, ensure_ascii=False, indent=2) + "\n")


if __name__ == "__main__":
    main()
