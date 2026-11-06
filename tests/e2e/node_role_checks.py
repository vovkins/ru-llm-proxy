"""Small two-project acceptance with real local NER pools and a mock provider."""

import contextlib
import io
import ipaddress
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import uuid

from deployment_topology_gate import Gate, assert_request, request, require_http_status
from ner_test_support import IMAGE_TAGS, ROOT, image_id
from node_routing_checks import call, output
from deployment import Context


class TestNodeContext(Context):
    """Only test ingress/network overrides follow the genuine runtime bundle."""

    def __init__(self, test_files, **kwargs):
        self.test_files = test_files
        super().__init__(**kwargs)

    @property
    def command(self):
        command = super().command
        for path in self.test_files:
            command += ["-f", str(path)]
        return command


def check_node_roles(images=None):
    images = {name: image_id(tag) for name, tag in (images or IMAGE_TAGS).items()}
    safe = {key: os.environ[key] for key in ("PATH", "HOME", "TMPDIR", "PYTHONDONTWRITEBYTECODE") if key in os.environ}
    previous = dict(os.environ)
    prefix = "ru-node-roles-" + uuid.uuid4().hex[:8]
    link, client = prefix + "-link", prefix + "-client"
    result = {"check": "two_node_roles", "status": "running", "cases": [],
              "published_host_nat": "not measured by the isolated Docker bridge emulation"}
    compute = None
    generated_projects = [prefix + "-compute"]
    old_source_probe = prefix + "-old-source"
    with tempfile.TemporaryDirectory(prefix="ru-node-roles-") as temp:
        directory = Path(temp)
        try:
            output("network", "create", "--internal", link)
            subnet = ipaddress.ip_network(json.loads(output("network", "inspect", link))[0]["IPAM"]["Config"][0]["Subnet"])
            a, b, stranger = [str(subnet.network_address + n) for n in (20, 21, 22)]
            nodes = directory / "nodes.json"
            nodes.write_text(json.dumps({"primary": {"source_cidrs": [a + "/32"]},
                                         "compute": {"analyzer_url": f"http://{b}:5001"}}))
            resource_file = directory / "small.env"
            resource_file.write_text("LITELLM_REPLICAS=1\nANALYZER_REPLICAS=1\nTRITON_REPLICAS=1\nANALYZER_MEMORY=1g\nTRITON_MEMORY=2g\n")
            compute_env = directory / ".env.compute"
            compute_reset = directory / "compute-ports.yml"
            compute_reset.write_text("services:\n  nginx:\n    ports: !reset []\n")
            compute_test = directory / "compute-test.json"
            compute_test.write_text(json.dumps({"services": {
                "nginx": {"networks": {"ru-llm-proxy": {}, "node-link": {"ipv4_address": b}}},
                **{name: {"image": images[name], "pull_policy": "never", "restart": "no",
                          "healthcheck": {"interval": "3s"}} for name in ("presidio-analyzer", "ner-triton")},
            }, "networks": {"node-link": {"external": True, "name": link}}}))
            compute_base = dict(safe, STACK="litellm-presidio-codex-lb", TOPOLOGY="production", NODE_ROLE="compute",
                NODE_CONFIG=str(nodes), ENV_FILE=str(compute_env), TOPOLOGY_ENV=str(resource_file),
                COMPOSE_PROJECT_NAME=prefix + "-compute", STACK_START_TIMEOUT="300")
            os.environ.clear()
            os.environ.update(compute_base)
            compute = TestNodeContext([compute_reset, compute_test])
            compute.setup()
            compute.start()
            assert len(compute.containers()) == 3
            assert not any(row["HostConfig"]["PortBindings"] for row in compute.containers())
            output("run", "-d", "--name", client, "--network", link, "--ip", stranger,
                   "python:3.12-slim", "sleep", "3600")
            denied = call(client, f"http://{b}:5001/api/v1/health",
                          headers={"X-Forwarded-For": a, "X-Real-IP": a})
            assert denied["status"] == 403

            for stack in ("litellm-presidio", "litellm-presidio-codex-lb"):
                primary_dir = directory / stack
                primary_dir.mkdir()
                os.environ.clear()
                os.environ.update(safe, NODE_ROLE="primary", NODE_CONFIG=str(nodes))
                gate = Gate(primary_dir, images=images)
                generated_projects.append(gate.project)
                probe = gate.project + "-subnet-probe"
                output("network", "create", probe)
                try:
                    gate.ipv4_subnet = json.loads(output("network", "inspect", probe))[0]["IPAM"]["Config"][0]["Subnet"]
                finally:
                    output("network", "rm", probe)
                primary_test = primary_dir / "node-test.json"
                primary_test.write_text(json.dumps({"services": {"nginx": {"networks": {
                    "ru-llm-proxy": {}, "test-ingress": {}, "node-link": {"ipv4_address": a},
                    **({"codex-lb-proxy": {}} if stack.endswith("codex-lb") else {}),
                }}}, "networks": {"node-link": {"external": True, "name": link}}}))

                def primary(mode):
                    os.environ.clear()
                    os.environ.update(safe, NODE_ROLE="primary", NODE_CONFIG=str(nodes))
                    base = gate.context(stack, "production", mode, resource_file)
                    spec = json.loads(gate.overlay.read_text())
                    spec["services"]["mock-upstream"]["labels"]["ru-llm-proxy.node-role"] = "primary"
                    spec["services"]["litellm"]["environment"].update(
                        PII_GUARDRAIL_ANALYZER_TIMEOUT_SECONDS="240", PII_GUARDRAIL_ANALYZER_CONNECT_TIMEOUT_SECONDS="5")
                    gate.overlay.write_text(json.dumps(spec))
                    ctx = TestNodeContext([primary_test], env_file=str(base.env_file))
                    ctx.files = list(base.files)
                    gate.last_context = ctx
                    return ctx

                try:
                    ctx = primary("mask")
                    ctx.compose("up", "-d", "--no-build", "--no-deps", "mock-upstream")
                    ctx.start()
                    gate.verify_state(ctx, first=True)
                    with contextlib.redirect_stdout(io.StringIO()) as stream:
                        ctx.metrics()
                    assert "ru_pii_guardrail_" in stream.getvalue()
                    url, mock = gate.url(ctx, "nginx", 80), gate.url(ctx, "mock-upstream", 8080)
                    status, body = request(url, "/key/generate", {"models": ["mock-chat", "mock-responses"], "key_alias": "node-role-test"})
                    require_http_status(status, body, 200, "/key/generate")
                    token = json.loads(body)["key"]
                    request(mock, "/capture/reset", {})
                    for api in ("chat/completions", "responses"):
                        for streaming in (False, True):
                            assert_request(url, "mask", api, streaming, token)
                    capture = json.loads(request(mock, "/capture")[1])
                    assert capture["provider_requests"] == 4 and not capture["provider_saw_raw_phone"]
                    assert capture["provider_saw_pii_placeholder"]
                    assert call(client, f"http://{a}:5001/api/v1/health", method="HEAD")["status"] == 200
                    for endpoint in ("http://127.0.0.1:5002", f"http://{b}:5001"):
                        ctx.compose("exec", "-T", "nginx", "wget", "--spider", "-q", endpoint + "/api/v1/health", capture=True)
                    # Address each genuine pool explicitly, independent of
                    # cache hits or the balance of health versus POST requests.
                    def inference_counts():
                        counts = []
                        for context in (ctx, compute):
                            item = next(row for row in context.containers()
                                        if row["Config"]["Labels"].get("com.docker.compose.service") == "ner-triton")
                            counts.append(sum(float(line.split()[-1]) for line in gate.metrics(item, 8002).splitlines()
                                              if line.startswith("nv_inference_request_success{") and 'model="tiny2"' in line))
                        return counts

                    counters = []
                    for index, endpoint in enumerate(("http://127.0.0.1:5002", f"http://{b}:5001")):
                        before = inference_counts()
                        text = "Телефон +79031234567."
                        raw = ctx.compose("exec", "-T", "nginx", "wget", "-qO-", "--header=Content-Type: application/json",
                            "--post-data=" + json.dumps({"text": text, "language": "ru"}), endpoint + "/api/v1/analyze", capture=True)
                        assert any(row["entity_type"] == "PHONE_NUMBER" and text[row["start"]:row["end"]] == "+79031234567"
                                   for row in json.loads(raw)["entities"])
                        after = inference_counts()
                        assert after[index] > before[index] and after[1 - index] == before[1 - index], (before, after)
                        counters.append([after[n] - before[n] for n in range(2)])
                    assert gate.redis(ctx, "KEYS", "pii_mapping:*") == ""
                    previous_bundle = ctx._bundle
                    fresh = primary("block")
                    fresh.start()
                    gate.verify_state(fresh)
                    request(mock, "/capture/reset", {})
                    for api in ("chat/completions", "responses"):
                        for streaming in (False, True):
                            assert_request(gate.url(fresh, "nginx", 80), "block", api, streaming, token)
                    assert json.loads(request(mock, "/capture")[1])["provider_requests"] == 0
                    assert fresh._bundle.digest == previous_bundle.digest
                    fresh.start(force=True)
                    gate.verify_state(fresh)
                    assert (previous_bundle.directory / "default.conf").is_file()
                    if stack == "litellm-presidio":
                        old_a, old_b = a, b
                        a, b = str(subnet.network_address + 24), str(subnet.network_address + 23)
                        old_primary_bundle, old_compute_bundle = fresh._bundle, compute._bundle
                        previous_ids = [next(row["Id"] for row in context.containers()
                            if row["Config"]["Labels"].get("com.docker.compose.service") == "nginx")
                            for context in (fresh, compute)]
                        nodes.write_text(json.dumps({"primary": {"source_cidrs": [a + "/32"]},
                                                     "compute": {"analyzer_url": f"http://{b}:5001"}}))
                        spec = json.loads(compute_test.read_text())
                        spec["services"]["nginx"]["networks"]["node-link"]["ipv4_address"] = b
                        compute_test.write_text(json.dumps(spec))
                        spec = json.loads(primary_test.read_text())
                        spec["services"]["nginx"]["networks"]["node-link"]["ipv4_address"] = a
                        primary_test.write_text(json.dumps(spec))
                        os.environ.clear()
                        os.environ.update(compute_base)
                        compute = TestNodeContext([compute_reset, compute_test])
                        compute.start()
                        fresh = primary("block")
                        fresh.start()
                        gate.verify_state(fresh)
                        for index, context in enumerate((fresh, compute)):
                            current = next(row["Id"] for row in context.containers()
                                if row["Config"]["Labels"].get("com.docker.compose.service") == "nginx")
                            assert current != previous_ids[index]
                        assert old_primary_bundle.digest != fresh._bundle.digest
                        assert old_compute_bundle.digest != compute._bundle.digest
                        config = fresh.compose("exec", "-T", "nginx", "nginx", "-T", capture=True)
                        assert f"server {b}:5001 " in config and f"server {old_b}:5001 " not in config
                        raw = fresh.compose("exec", "-T", "nginx", "wget", "-qO-",
                            "--header=Content-Type: application/json", '--post-data={"text":"+79031234567","language":"ru"}',
                            f"http://{b}:5001/api/v1/analyze", capture=True)
                        assert any(row["entity_type"] == "PHONE_NUMBER" for row in json.loads(raw)["entities"])
                        output("run", "-d", "--name", old_source_probe, "--network", link, "--ip", old_a,
                               "python:3.12-slim", "sleep", "120")
                        assert call(old_source_probe, f"http://{b}:5001/api/v1/health",
                                    headers={"X-Forwarded-For": a})["status"] == 403
                        assert call(old_source_probe, f"http://{b}:5001/api/v1/health", method="HEAD",
                                    headers={"X-Forwarded-For": a})["status"] == 403
                        output("rm", "-f", old_source_probe)
                        result["address_and_acl_generation_update"] = "passed"
                    # Local diagnostics and teardown do not need the source JSON.
                    os.environ.pop("NODE_CONFIG")
                    fresh = TestNodeContext([primary_test], env_file=str(gate.env_file))
                    fresh.files = list(ctx.files)
                    fresh.load_runtime()
                    fresh.compose("logs", "--tail", "1", capture=True)
                    gate.last_context = fresh
                    result["cases"].append({"stack": stack, "mask": 4, "block": 4,
                        "local_and_remote_ner": "passed", "inference_deltas_A_B": counters,
                        "restart_state": "preserved", "logs_without_node_config": True, "head_health": "passed"})
                except BaseException as exc:
                    gate.capture_failure(exc)
                    print(json.dumps(gate.result.get("diagnostics", {})), flush=True)
                    raise
                finally:
                    gate.cleanup()
            os.environ.clear()
            os.environ.update(compute_base)
            os.environ.pop("NODE_CONFIG")
            installed = TestNodeContext([compute_reset, compute_test])
            installed.load_runtime()
            installed.compose("logs", "--tail", "1", capture=True)
            installed.compose("down", "-v", "--remove-orphans")
            assert not installed.container_ids()
            result.update(status="passed", unauthorized_source="denied", cleanup_ok=True)
            return result
        finally:
            if compute is not None:
                subprocess.run(compute.command + ["down", "-v", "--remove-orphans"],
                               env=compute.environment, cwd=ROOT, check=True, timeout=90)
            subprocess.run(["docker", "rm", "-f", client, old_source_probe], capture_output=True, timeout=30)
            removed = subprocess.run(["docker", "network", "rm", link], capture_output=True, timeout=30)
            os.environ.clear()
            os.environ.update(previous)
            for project in generated_projects:
                shutil.rmtree(ROOT / "deploy/generated" / project, ignore_errors=True)
            if removed.returncode and subprocess.run(["docker", "network", "inspect", link], capture_output=True).returncode == 0:
                raise RuntimeError("Node role test network cleanup failed")


if __name__ == "__main__":
    print(json.dumps(check_node_roles(), indent=2))
