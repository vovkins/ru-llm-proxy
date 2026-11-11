"""Small two-project acceptance with real local NER pools and a mock provider."""

import contextlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import uuid

from check_responses_history import decode
from deployment_topology_gate import PHONE, Gate, assert_request, request, require_http_status
from dkb_corpus_checks import payload_for
from ner_instruction_checks import _restored_text
from ner_protocol_checks import native_json_result
from ner_test_support import IMAGE_TAGS, ROOT, configure_test_ipv4_subnet, image_id, wait_ready
from node_routing_checks import call, output
from deployment import Context, DeploymentError

DICTIONARY_SOURCE = "Словарь Тестовый Альфа"
DICTIONARY_REPLACEMENT = "Словарь Тестовый Бета"


def replicas(context, service):
    return [row for row in context.containers()
            if row["Config"]["Labels"].get("com.docker.compose.service") == service]


def gateway_posts(context):
    gateway, = replicas(context, "nginx")
    rows = (json.loads(line) for line in output("logs", "--tail", "128", gateway["Id"]).splitlines()
            if line.startswith('{"request_id"'))
    return [row for row in rows if row["method"] == "POST"]


def inference_count(gate, context):
    return sum(float(line.split()[-1]) for item in replicas(context, "ner-triton")
               if item["State"]["Running"] for line in gate.metrics(item, 8002).splitlines()
               if line.startswith("nv_inference_request_success{") and 'model="tiny2"' in line)


def route_evidence(primary, compute, endpoint):
    """Correlate one field at the central router and the chosen local ingress."""
    local = "127.0.0.1:5002"
    central, = [row for row in primary if set(row["upstream"].split(", ")) <= {local, endpoint}]
    peers = central["upstream"].split(", ")
    statuses = central["upstream_status"].split(", ")
    assert 1 <= len(peers) == len(statuses) <= 2 and statuses[-1] == "200"
    request_id, field = central["request_id"], central["field"]
    assert request_id not in {"", "-"} and field.isdecimal()
    local_rows = [row for row in primary if row is not central]
    chosen, other = (compute, local_rows) if peers[-1] == endpoint else (local_rows, compute)
    accepted, = [row for row in chosen if row["status"] == 200]
    assert (accepted["request_id"], accepted["field"]) == (request_id, field)
    assert not any(row["status"] == 200 for row in other)
    return {"pool": "B" if peers[-1] == endpoint else "A", "attempts": len(peers)}


def parsed_response(body, stream):
    if not stream:
        return json.loads(body)
    return [json.loads(value) if value != "[DONE]" else value for line in body.splitlines()
            if line.startswith("data:") and (value := line[5:].strip())]


def capture(gate, context, provider, calls):
    state = json.loads(request(provider, "/capture")[1])
    assert state["provider_requests"] == calls, "Unexpected number of provider calls"
    assert not state["provider_saw_raw_phone"] and not state["provider_saw_e9_raw_phone"]
    assert not state["provider_saw_canary"], "Source dictionary value reached the provider"
    if calls:
        assert state["provider_saw_pii_placeholder"]
    assert gate.redis(context, "KEYS", "pii_mapping:*") == "", "Request mapping not cleaned"
    return state


def check_client_route(gate, primary, compute, url, provider, token, endpoint, phase, expected):
    gate.result["operation"] = f"client-route-{phase}"
    contexts = (primary, compute)
    for index in range(6):
        text = f"Проверка {phase} {index}. Телефон клиента {PHONE}."
        payload = payload_for(text, "chat/completions", False, False)
        before_posts = [gateway_posts(context) for context in contexts]
        before_counts = [inference_count(gate, context) for context in contexts]
        request(provider, "/capture/reset", {})
        status, body = request(url, "/v1/chat/completions", payload, token)
        require_http_status(status, body, 200, "/v1/chat/completions")
        assert _restored_text(json.loads(body), "chat/completions", False) == text
        capture(gate, primary, provider, 1)
        posts = [[row for row in gateway_posts(context) if row not in before]
                 for context, before in zip(contexts, before_posts)]
        evidence = route_evidence(*posts, endpoint)
        after_counts = [inference_count(gate, context) for context in contexts]
        deltas = [after - before for before, after in zip(before_counts, after_counts)]
        selected = 0 if evidence["pool"] == "A" else 1
        assert deltas[selected] > 0 and deltas[1 - selected] == 0, "Wrong Triton pool served the analysis"
        if evidence["pool"] == expected:
            return dict(evidence, phase=phase, probes=index + 1, inference_deltas_A_B=deltas,
                        api="chat/completions", stream=False, mode="mask", http_status=200,
                        provider_requests=1, mapping_cleanup=True), payload
        if index < 5:
            # A serial health/POST pair can keep selecting the same two peers.
            status, body = request(gate.url(primary, "nginx", 5001), "/api/v1/health")
            require_http_status(status, body, 200, "/api/v1/health")
    raise AssertionError(f"No fresh client request reached pool {expected} during {phase}")


def check_client_cache(gate, context, compute, url, provider, token, payload):
    gate.result["operation"] = "client-cache-hit"
    litellm, = replicas(context, "litellm")
    before = gate.cache_hits(litellm)
    counts = [inference_count(gate, node) for node in (context, compute)]
    request(provider, "/capture/reset", {})
    status, body = request(url, "/v1/chat/completions", payload, token)
    require_http_status(status, body, 200, "/v1/chat/completions")
    assert _restored_text(json.loads(body), "chat/completions", False) == payload["messages"][0]["content"]
    capture(gate, context, provider, 1)
    assert gate.cache_hits(litellm) > before
    assert [inference_count(gate, node) for node in (context, compute)] == counts


def check_dictionary_tools(gate, context, url, provider, token, *, blocked):
    gate.result["operation"] = "dictionary-tools-block" if blocked else "dictionary-tools-mask"
    values = {"company": DICTIONARY_SOURCE, "phone": PHONE}
    behavior = "JSON_NUMBER_ECHO E9_FRAGMENTED_RESPONSE NATIVE_ARGUMENT_TOOL"
    for api in ("chat/completions", "responses"):
        for stream in (False, True):
            payload = payload_for(behavior, api, stream, True)
            target = (payload["messages"][1]["tool_calls"][0]["function"]
                      if api == "chat/completions" else payload["input"][1])
            target["arguments"] = json.dumps(values, ensure_ascii=False)
            function = {"name": "inspect", "parameters": {"type": "object", "properties": {
                "company": {"type": "string"}, "phone": {"type": "string"},
            }}}
            payload["tools"] = ([{"type": "function", "function": function}]
                                if api == "chat/completions" else [{"type": "function", **function}])
            request(provider, "/capture/reset", {})
            status, body = request(url, "/v1/" + api, payload, token)
            require_http_status(status, body, 422 if blocked else 200, "/v1/" + api, stream)
            state = capture(gate, context, provider, 0 if blocked else 1)
            if blocked:
                assert PHONE not in body and DICTIONARY_SOURCE not in body
                continue
            outgoing, = state["provider_native_arguments"]
            data = json.loads(outgoing)
            assert data["company"] == DICTIONARY_REPLACEMENT
            assert data["phone"].startswith("<PHONE_NUMBER_") and PHONE not in outgoing
            response = parsed_response(body, stream)
            assert json.loads(native_json_result(response, api, stream, True)) == values
            if stream and api == "chat/completions":
                assert "[DONE]" in response
                assert any(choice.get("finish_reason") == "tool_calls" for event in response if isinstance(event, dict)
                           for choice in event.get("choices", []))


def check_client_history(gate, context, url, provider, token, other_token):
    gate.result["operation"] = "client-history"
    second = "+79039876543"
    for stream in (False, True):
        request(provider, "/capture/reset", {})
        payload = {"model": "mock-responses", "input": PHONE, "stream": stream, "store": False,
                   "metadata": {"test_history": "STATEFUL_HISTORY"}}
        status, body = request(url, "/v1/responses", payload, token)
        require_http_status(status, body, 200, "/v1/responses", stream)
        parent, restored = decode(body, stream)
        assert restored == PHONE
        payload.update(input=second, previous_response_id=parent)
        status, body = request(url, "/v1/responses", payload, other_token)
        require_http_status(status, body, 409, "/v1/responses", stream)
        assert PHONE not in body and second not in body
        capture(gate, context, provider, 1)
        status, body = request(url, "/v1/responses", payload, token)
        require_http_status(status, body, 200, "/v1/responses", stream)
        _, restored = decode(body, stream)
        assert restored == PHONE + " / " + second
        capture(gate, context, provider, 2)


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


def check_local_observability(context):
    report = context.health()
    assert report["scope"] == "local" and report["remote_node"] == "not_checked"
    containers = context.containers()
    checks = []
    for service in ("presidio-analyzer", "ner-triton"):
        replicas = [row for row in containers if row["Config"]["Labels"].get("com.docker.compose.service") == service]
        assert len(replicas) == 2
        target = replicas[0]
        output("stop", "-t", "3", target["Id"])
        try:
            # A successful balanced route must not hide a stopped peer.
            wait_ready(lambda: context.gateway_ready(context.containers()), 20)
            report = context.readiness()
            row = report["services"][service]
            assert report["status"] != "full" and row["found"] == row["expected"] == 2
            assert row["ready"] == 1
            assert any(item["name"] == target["Name"].lstrip("/") and item["reason"] == "stopped" for item in row["replicas"])
            try:
                context.health()
            except DeploymentError:
                pass
            else:
                raise AssertionError("Balanced health hid a stopped local replica")
            with contextlib.redirect_stdout(io.StringIO()) as stream:
                try:
                    context.metrics()
                except DeploymentError:
                    pass
                else:
                    raise AssertionError("Incomplete metrics collection was accepted")
            text = stream.getvalue()
            assert "status=stopped" in text and "ru_presidio_" in text and "nv_inference_" in text
            assert "scope=local role=compute" in text
            checks.append({"stopped_service": service, "balanced_health": "passed", "remaining_metrics": "passed"})
        finally:
            output("start", target["Id"])
            context.health(120)
    target = next(row for row in containers if row["Config"]["Labels"].get("com.docker.compose.service") == "ner-triton")
    output("exec", target["Id"], "python3", "-c",
           "import json; from pathlib import Path; p=Path('/models/tiny2/export-manifest.json'); "
           "p.with_suffix('.observability-backup').write_bytes(p.read_bytes()); d=json.loads(p.read_text()); "
           "d['onnx_sha256']='b'*64; p.write_text(json.dumps(d))")
    try:
        report = context.readiness()
        assert report["status"] != "full" and report["services"]["ner-triton"]["ready"] == 1
        with contextlib.redirect_stdout(io.StringIO()):
            try:
                context.health()
            except DeploymentError:
                pass
            else:
                raise AssertionError("An incorrect local model artifact was accepted")
    finally:
        output("exec", target["Id"], "python3", "-c",
               "from pathlib import Path; p=Path('/models/tiny2/export-manifest.json'); "
               "p.write_bytes(p.with_suffix('.observability-backup').read_bytes()); p.with_suffix('.observability-backup').unlink()")
        context.health(120)
    return {"status": "passed", "replicas_per_pool": 2, "partial_failures": checks,
            "wrong_artifact_rejected": True, "recovery": "passed"}


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
            subnet = configure_test_ipv4_subnet(link)
            a, b, stranger = [str(subnet.network_address + n) for n in (20, 21, 22)]
            nodes = directory / "nodes.json"
            nodes.write_text(json.dumps({"primary": {"source_cidrs": [a + "/32"]},
                                         "compute": {"analyzer_url": f"http://{b}:5001"}}))
            resource_file = directory / "small.env"
            resource_file.write_text("LITELLM_REPLICAS=1\nANALYZER_REPLICAS=1\nTRITON_REPLICAS=1\nANALYZER_MEMORY=1g\nTRITON_MEMORY=2g\n")
            compute_resources = directory / "compute-small.env"
            compute_resources.write_text(resource_file.read_text().replace("ANALYZER_REPLICAS=1", "ANALYZER_REPLICAS=2")
                                         .replace("TRITON_REPLICAS=1", "TRITON_REPLICAS=2"))
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
                NODE_CONFIG=str(nodes), ENV_FILE=str(compute_env), TOPOLOGY_ENV=str(compute_resources),
                COMPOSE_PROJECT_NAME=prefix + "-compute", STACK_START_TIMEOUT="300")
            os.environ.clear()
            os.environ.update(compute_base)
            compute = TestNodeContext([compute_reset, compute_test])
            compute.setup()
            compute.start()
            assert len(compute.containers()) == 5
            assert not any(row["HostConfig"]["PortBindings"] for row in compute.containers())
            result["local_observability"] = check_local_observability(compute)
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
                    spec["services"]["mock-upstream"]["environment"]["FINAL_PAYLOAD_LEAK_CHECK_CANARIES"] = DICTIONARY_SOURCE
                    spec["services"]["litellm"]["environment"].update(
                        PII_GUARDRAIL_ANALYZER_TIMEOUT_SECONDS="240", PII_GUARDRAIL_ANALYZER_CONNECT_TIMEOUT_SECONDS="5",
                        DICTIONARY_SUBSTITUTIONS_JSON=json.dumps({"substitutions": [{"id": "node-test",
                            "source": DICTIONARY_SOURCE, "replacement": DICTIONARY_REPLACEMENT, "restore": True}]}))
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
                    status, body = request(url, "/key/generate", {"models": ["mock-chat", "mock-responses"], "key_alias": "node-role-other"})
                    require_http_status(status, body, 200, "/key/generate")
                    other_token = json.loads(body)["key"]
                    request(mock, "/capture/reset", {})
                    for api in ("chat/completions", "responses"):
                        for streaming in (False, True):
                            assert_request(url, "mask", api, streaming, token)
                    capture(gate, ctx, mock, 4)
                    check_dictionary_tools(gate, ctx, url, mock, token, blocked=False)
                    check_client_history(gate, ctx, url, mock, token, other_token)
                    endpoint = f"{b}:5001"
                    healthy, cached = check_client_route(gate, ctx, compute, url, mock, token, endpoint, "remote", "B")
                    check_client_cache(gate, ctx, compute, url, mock, token, cached)
                    compute.compose("stop", "-t", "3")
                    try:
                        local = ctx.health()
                        assert local["status"] == "full" and local["remote_node"] == "not_checked"
                        fallback, _ = check_client_route(gate, ctx, compute, url, mock, token, endpoint, "fallback", "A")
                        ctx.compose("stop", "-t", "3", "presidio-analyzer")
                        try:
                            request(mock, "/capture/reset", {})
                            payload = payload_for(f"Оба пула выключены. Телефон клиента {PHONE}.", "chat/completions", False, False)
                            status, body = request(url, "/v1/chat/completions", payload, token)
                            require_http_status(status, body, 503, "/v1/chat/completions")
                            assert PHONE not in body
                            capture(gate, ctx, mock, 0)
                        finally:
                            ctx.compose("start", "presidio-analyzer")
                    finally:
                        compute.start()
                    ctx.health(120)
                    recovered, _ = check_client_route(gate, ctx, compute, url, mock, token, endpoint, "recovered", "B")
                    result.setdefault("primary_without_compute", []).append({"stack": stack, "local_health": "full",
                                                                              "request": "passed", "compute_recovery": "passed"})
                    assert call(client, f"http://{a}:5001/api/v1/health", method="HEAD")["status"] == 200
                    for endpoint in ("http://127.0.0.1:5002", f"http://{b}:5001"):
                        ctx.compose("exec", "-T", "nginx", "wget", "--spider", "-q", endpoint + "/api/v1/health", capture=True)
                    counters = []
                    for index, endpoint in enumerate(("http://127.0.0.1:5002", f"http://{b}:5001")):
                        before = [inference_count(gate, context) for context in (ctx, compute)]
                        text = "Телефон +79031234567."
                        raw = ctx.compose("exec", "-T", "nginx", "wget", "-qO-", "--header=Content-Type: application/json",
                            "--post-data=" + json.dumps({"text": text, "language": "ru"}), endpoint + "/api/v1/analyze", capture=True)
                        assert any(row["entity_type"] == "PHONE_NUMBER" and text[row["start"]:row["end"]] == "+79031234567"
                                   for row in json.loads(raw)["entities"])
                        after = [inference_count(gate, context) for context in (ctx, compute)]
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
                    capture(gate, fresh, mock, 0)
                    check_dictionary_tools(gate, fresh, gate.url(fresh, "nginx", 80), mock, token, blocked=True)
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
                        "client_routes": [healthy, fallback, recovered], "cache_hit": "passed",
                        "both_pools_down": {"status": 503, "provider_requests": 0, "mapping_cleanup": True},
                        "dictionary_tools": {"mask": 4, "block": 4}, "history_and_key_isolation": "passed",
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
