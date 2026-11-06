"""Exercise test orchestration without Docker, model downloads or API keys."""
import json
import ipaddress
import os
import signal
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests/e2e"))
import model_smoke
import ner_integration
import ner_test_support as support
import mock_capacity_analyzer as capacity_fixture
from deployment_topology_gate import Gate
from presidio.tests.test_onnx_artifact import fixture_metadata
from presidio.model_artifact import ModelArtifactError


@pytest.fixture(autouse=True)
def isolated_environment(monkeypatch):
    monkeypatch.setattr(os, "environ", dict(os.environ))


@pytest.mark.parametrize("mode,status,code,retry", [
    ("overloaded", 503, "analyzer_overloaded", 7),
    ("unavailable", 503, "required_ner_unavailable", 5),
    ("ready", 200, None, None),
])
def test_capacity_fixture_does_not_echo_input_or_simulate_detection(monkeypatch, mode, status, code, retry):
    from io import BytesIO
    monkeypatch.setattr(capacity_fixture, "STATE", {"mode": mode, "calls": 0, "health": {}})
    body = b'{"text":"private-synthetic-input"}'
    output = {}
    handler = SimpleNamespace(path="/api/v1/analyze", headers={"Content-Length": str(len(body))},
        rfile=BytesIO(body), _json=lambda status, body, retry_after=None:
            output.update(status=status, body=body, retry_after=retry_after))
    capacity_fixture.Handler.do_POST(handler)
    assert output["status"] == status and output["retry_after"] == retry
    assert capacity_fixture.STATE["calls"] == 1
    assert "private-synthetic-input" not in json.dumps(output)
    if mode == "ready":
        assert output["body"] == {"entities": []}
    else:
        assert output["body"]["detail"]["code"] == code


def test_capacity_fixture_keeps_overload_ready_but_model_failure_unready(monkeypatch):
    health = {"status": "ok", "ner_state": "ready", "analysis_signature": "a" * 64}
    monkeypatch.setattr(capacity_fixture, "STATE", {"mode": "overloaded", "calls": 0, "health": health})
    output = {}
    handler = SimpleNamespace(path="/api/v1/health", _json=lambda status, body:
                              output.update(status=status, body=body))
    capacity_fixture.Handler.do_GET(handler)
    assert output == {"status": 200, "body": health}
    capacity_fixture.STATE["mode"] = "unavailable"
    capacity_fixture.Handler.do_GET(handler)
    assert output["status"] == 503 and output["body"]["ner_state"] == "failed"
    assert health["ner_state"] == "ready"


def test_ner_ci_tracks_analyzer_routing_fixtures():
    workflow = yaml.safe_load((ROOT / ".github/workflows/ner-model-integration.yml").read_text())
    triggers = workflow.get("on", workflow.get(True))
    for trigger in ("pull_request", "push"):
        assert "tests/e2e/analyzer_routing_checks.py" in triggers[trigger]["paths"]
        assert "tests/e2e/mock_capacity_analyzer.py" in triggers[trigger]["paths"]


def test_wait_ready_has_a_total_deadline():
    now = [0]
    probes = []
    def sleep(value):
        now[0] += value
    with pytest.raises(TimeoutError):
        support.wait_ready(lambda: probes.append(True) or False, 2.5, sleep=sleep, clock=lambda: now[0])
    assert now[0] == 2.5
    assert len(probes) == 4


def test_wait_ready_stops_after_first_success():
    probes = iter([False, True, False])
    support.wait_ready(lambda: next(probes), 5, sleep=lambda _: None)
    assert next(probes) is False


@pytest.mark.parametrize("output", ["not-an-image", "sha256:short", ""])
def test_invalid_image_identity_is_not_accepted(monkeypatch, output):
    monkeypatch.setattr(support, "docker", lambda *a, **k: SimpleNamespace(stdout=output))
    with pytest.raises(RuntimeError, match="identity"):
        support.image_id("tag")


@pytest.mark.parametrize("exception", [subprocess.TimeoutExpired("probe", 1), KeyboardInterrupt()])
def test_offline_probe_removes_its_container_on_failure(monkeypatch, exception):
    calls = []
    def fail(*args, **kwargs):
        calls.append(args)
        raise exception
    monkeypatch.setattr(support, "docker", fail)
    monkeypatch.setattr(support.subprocess, "run", lambda *a, **k: calls.append(a) or SimpleNamespace(returncode=0))
    with pytest.raises(type(exception)):
        support.offline_probe("image", "python", "check.py")
    name = calls[0][calls[0].index("--name") + 1]
    assert calls[-1][0] == ["docker", "rm", "-f", name]
    assert "--network" in calls[0] and "none" in calls[0]


@pytest.mark.parametrize("mismatch", [False, True])
def test_both_images_must_match_the_checkout_and_each_other(monkeypatch, mismatch):
    _, artifact = fixture_metadata()
    expected = {"model_id": artifact["source_model_id"], "revision": artifact["source_revision"],
                "onnx_sha256": artifact["onnx_sha256"]}
    peer = dict(expected, onnx_sha256="f" * 64) if mismatch else expected
    replies = iter([artifact, peer])
    monkeypatch.setattr(support, "offline_probe", lambda *a, **k: SimpleNamespace(stdout=json.dumps(next(replies))))
    if mismatch:
        with pytest.raises(RuntimeError, match="same trusted"):
            support.identities({"presidio-analyzer": "a", "ner-triton": "t"})
    else:
        assert support.identities({"presidio-analyzer": "a", "ner-triton": "t"}) == expected


def test_source_mismatch_cannot_be_learned_from_the_server(monkeypatch):
    _, artifact = fixture_metadata()
    artifact["source_revision"] = "main"
    monkeypatch.setattr(support, "offline_probe", lambda *a, **k: SimpleNamespace(stdout=json.dumps(artifact)))
    with pytest.raises(ModelArtifactError):
        support.identities({"presidio-analyzer": "a", "ner-triton": "t"})


@pytest.mark.parametrize("internal", [True, False])
def test_network_isolation_is_checked_not_assumed(monkeypatch, internal):
    monkeypatch.setattr(support, "docker", lambda *a, **k: SimpleNamespace(stdout=json.dumps([{"Internal": internal}])))
    if internal:
        support.check_internal_network("test")
    else:
        with pytest.raises(RuntimeError):
            support.check_internal_network("test")


@pytest.fixture
def empty_node_link():
    return {"Name": "ru-node-routing-0123abcd-link", "Driver": "bridge", "Internal": True,
            "Containers": {}, "IPAM": {"Config": [{"Subnet": "172.28.0.0/16"}]}}


@pytest.mark.parametrize("role", ["routing", "roles"])
@pytest.mark.parametrize("cidr", ["10.80.0.0/24", "172.28.0.0/16", "172.28.0.0/27"])
def test_node_link_reuses_docker_cidr_with_explicit_internal_subnet(monkeypatch, empty_node_link, role, cidr):
    name = "ru-node-" + role + "-0123abcd-link"
    empty_node_link["Name"] = name
    empty_node_link["IPAM"]["Config"][0]["Subnet"] = cidr
    calls = []
    def docker(*args, **kwargs):
        calls.append(args)
        return SimpleNamespace(stdout=json.dumps([empty_node_link]) if args[:2] == ("network", "inspect") else "")
    monkeypatch.setattr(support, "docker", docker)
    subnet = support.configure_test_ipv4_subnet(name)
    assert subnet == ipaddress.IPv4Network(cidr)
    assert all(subnet.network_address + n in subnet.hosts() for n in (20, 21, 22, 23, 24))
    assert calls == [("network", "inspect", name), ("network", "rm", name),
                     ("network", "create", "--internal", "--subnet", cidr, name)]


@pytest.mark.parametrize("name", [None, 1, "ru-llm-proxy_ru-llm-proxy", "ru-node-routing-other-link",
                                "ru-node-roles-0123abcd-local-a"])
def test_node_link_rejects_unowned_names_without_docker(monkeypatch, name):
    calls = []
    monkeypatch.setattr(support, "docker", lambda *a, **k: calls.append(a))
    with pytest.raises(RuntimeError, match="owned"):
        support.configure_test_ipv4_subnet(name)
    assert calls == []


@pytest.mark.parametrize("invalid", ["wrong-name", "external", "overlay", "attached", "missing-containers",
                                   "ipv6", "small", "host-bits", "invalid-cidr", "no-config", "two-subnets",
                                   "not-json", "not-a-list", "empty-list", "two-networks"])
def test_node_link_rejects_unsafe_or_invalid_inspect_before_mutation(monkeypatch, empty_node_link, invalid):
    name = empty_node_link["Name"]
    if invalid == "wrong-name":
        empty_node_link["Name"] = "private-other-network"
    elif invalid == "external":
        empty_node_link["Internal"] = False
    elif invalid == "overlay":
        empty_node_link["Driver"] = "overlay"
    elif invalid == "attached":
        empty_node_link["Containers"] = {"private-container": {}}
    elif invalid == "missing-containers":
        empty_node_link.pop("Containers")
    elif invalid in {"ipv6", "small", "host-bits", "invalid-cidr"}:
        empty_node_link["IPAM"]["Config"][0]["Subnet"] = {
            "ipv6": "fd00::/64", "small": "172.28.0.0/28", "host-bits": "172.28.0.1/16",
            "invalid-cidr": "private-cidr"}[invalid]
    elif invalid == "no-config":
        empty_node_link["IPAM"]["Config"] = []
    elif invalid == "two-subnets":
        empty_node_link["IPAM"]["Config"].append({"Subnet": "fd00::/64"})
    raw = json.dumps([empty_node_link])
    raw = {"not-json": "private-invalid-json", "not-a-list": "{}", "empty-list": "[]",
           "two-networks": json.dumps([empty_node_link, empty_node_link])}.get(invalid, raw)
    calls = []
    monkeypatch.setattr(support, "docker", lambda *a, **k: calls.append(a) or SimpleNamespace(stdout=raw))
    with pytest.raises(RuntimeError, match="empty internal bridge") as captured:
        support.configure_test_ipv4_subnet(name)
    assert calls == [("network", "inspect", name)]
    assert "private" not in str(captured.value)


@pytest.mark.parametrize("operation", ["inspect", "rm", "create"])
@pytest.mark.parametrize("interrupted", [False, True])
def test_node_link_command_failure_is_not_retried_or_replaced(monkeypatch, empty_node_link, operation, interrupted):
    name = empty_node_link["Name"]
    exc = KeyboardInterrupt() if interrupted else subprocess.CalledProcessError(125, ["docker", "network", operation])
    calls = []
    def docker(*args, **kwargs):
        calls.append(args)
        if args[1] == operation:
            raise exc
        return SimpleNamespace(stdout=json.dumps([empty_node_link]))
    monkeypatch.setattr(support, "docker", docker)
    with pytest.raises(type(exc)) as captured:
        support.configure_test_ipv4_subnet(name)
    assert captured.value is exc
    assert [command[1] for command in calls] == ["inspect", "rm", "create"][:["inspect", "rm", "create"].index(operation) + 1]


@pytest.mark.parametrize("module", ["node_routing_checks", "node_role_checks"])
@pytest.mark.parametrize("interrupted", [False, True])
def test_node_scenarios_clean_up_if_link_configuration_fails(monkeypatch, module, interrupted):
    import importlib
    node = importlib.import_module(module)
    commands, cleanup, configured = [], [], []
    previous = dict(os.environ)
    exc = KeyboardInterrupt() if interrupted else subprocess.CalledProcessError(125, ["docker", "network", "create"])
    def configure(name):
        configured.append(name)
        raise exc
    monkeypatch.setattr(node, "configure_test_ipv4_subnet", configure)
    monkeypatch.setattr(node, "output", lambda *a: commands.append(a) or "")
    monkeypatch.setattr(node.subprocess, "run", lambda a, **k:
                        cleanup.append(a) or SimpleNamespace(stdout=b"", stderr=b"", returncode=0))
    if module == "node_role_checks":
        monkeypatch.setattr(node, "image_id", lambda image: image)
    with pytest.raises(type(exc)) as captured:
        (node.check_node_responses if module == "node_routing_checks" else node.check_node_roles)()
    assert captured.value is exc and len(configured) == 1
    assert all(command[:2] == ("network", "create") for command in commands)
    assert ["docker", "network", "rm", configured[0]] in cleanup
    assert dict(os.environ) == previous


def test_diagnostic_report_does_not_copy_container_secrets(monkeypatch):
    data = [{"Name": "/test", "Image": "sha256:test", "RestartCount": 0,
             "Config": {"Env": ["PASSWORD=not-for-report"]}, "State": {
                 "Status": "running", "ExitCode": 0, "OOMKilled": False,
                 "Health": {"Status": "healthy", "Log": [{"Output": "private payload"}]}}}]
    monkeypatch.setattr(support, "docker", lambda *a, **k: SimpleNamespace(stdout=json.dumps(data)))
    result = support.container_states(["one"])
    assert result[0]["health"] == "healthy"
    assert "not-for-report" not in json.dumps(result)
    assert "private payload" not in json.dumps(result)


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("tool", [False, True])
@pytest.mark.parametrize("password_only", [False, True])
def test_password_mock_echo_selection_does_not_change_the_request(stream, tool, password_only):
    import mock_openai_upstream as mock

    payload = {"messages": [{"role": "user", "content": "<PASSWORD_9>"},
                             {"role": "user", "content": "STOCK_BRIDGE_ECHO Пар<PERSON_1>: <PASSWORD_1>"}],
               "stream": stream}
    if password_only:
        payload["k07_password_echo"] = True
    if tool:
        payload["tools"] = [{"type": "function", "function": {"name": "save"}}]
    original = json.dumps(payload, sort_keys=True)
    output = {}
    handler = SimpleNamespace(
        _write_json=lambda status, body: output.update(status=status, body=body),
        _write_sse=lambda events: output.update(events=events),
    )
    mock.Handler._write_chat_bridge_echo(handler, payload)
    expected = ["<PASSWORD_1>"] if password_only else ["<PASSWORD_9>", "<PERSON_1>", "<PASSWORD_1>"]
    if stream:
        chunks = [event[1] for event in output["events"] if isinstance(event[1], dict)]
        if tool:
            actual = "".join(call.get("function", {}).get("arguments", "")
                             for event in chunks for call in event["choices"][0]["delta"].get("tool_calls", []))
        else:
            actual = "".join(event["choices"][0]["delta"].get("content", "") for event in chunks)
    else:
        assert output["status"] == 200
        message = output["body"]["choices"][0]["message"]
        actual = message["tool_calls"][0]["function"]["arguments"] if tool else message["content"]
    assert (json.loads(actual)["values"] if tool else actual) == (expected if tool else " / ".join(expected))
    assert json.dumps(payload, sort_keys=True) == original


@pytest.mark.parametrize("exception", [RuntimeError("readiness"), KeyboardInterrupt()])
def test_model_smoke_cleans_up_and_reports_failure(monkeypatch, tmp_path, exception):
    monkeypatch.setattr(model_smoke, "Context", lambda **k: SimpleNamespace(
        model={"services": {"nginx": {"image": "nginx:test"}}}, start_timeout=1))
    monkeypatch.setattr(model_smoke, "image_id", lambda x: x)
    monkeypatch.setattr(model_smoke, "identities", lambda x: {"revision": "test"})
    monkeypatch.setattr(model_smoke, "offline_probe", lambda *a: None)
    monkeypatch.setattr(model_smoke, "check_internal_network", lambda *a: None)
    monkeypatch.setattr(model_smoke, "docker", lambda *a, **k: SimpleNamespace(returncode=0))
    monkeypatch.setattr(model_smoke, "wait_ready", lambda *a, **k: (_ for _ in ()).throw(exception))
    calls = []
    monkeypatch.setattr(model_smoke.subprocess, "run", lambda args, **k:
                        calls.append(args) or SimpleNamespace(stdout="", returncode=0))
    report = tmp_path / "model.json"
    with pytest.raises(type(exception)):
        model_smoke.run("analyzer", "triton", report)
    assert any(call[1:3] == ["rm", "-f"] for call in calls)
    assert any(call[1:3] == ["network", "rm"] for call in calls)
    result = json.loads(report.read_text())
    assert result["status"] == "failed" and result["cleanup_ok"]
    assert result["phase"] == "readiness"


@pytest.mark.parametrize("suite", ["model", "proxy", "all"])
def test_missing_images_fail_before_a_provider_gate(monkeypatch, tmp_path, suite):
    monkeypatch.setattr(ner_integration, "image_id", lambda _: (_ for _ in ()).throw(RuntimeError("missing image")))
    calls = []
    monkeypatch.setattr(ner_integration, "run_child", lambda *a, **k: calls.append(a))
    with pytest.raises(RuntimeError):
        ner_integration.run(suite, report_dir=tmp_path)
    assert calls == []
    result = json.loads((tmp_path / f"integration-{suite}.json").read_text())
    assert result["status"] == "failed" and result["phase"] == "images"


def test_build_uses_synthetic_env_and_dedicated_tags(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(ner_integration, "docker", lambda *a, **k: calls.append((a, k)))
    ner_integration.build_images(tmp_path, ["presidio-analyzer", "ner-triton"], support.IMAGE_TAGS["presidio-analyzer"])
    args, kwargs = calls[0]
    assert args[args.index("--env-file") + 1] == str(ROOT / ".env.example")
    assert kwargs["env"]["CODEX_LB_API_KEY"] == "build-only"
    assert args[-3:] == ("build", "presidio-analyzer", "ner-triton")
    override = json.loads((tmp_path / "build.json").read_text())
    assert all(v["image"].startswith("ru-llm-proxy-ner-tests:") for v in override["services"].values())


@pytest.mark.parametrize("interrupted", [subprocess.TimeoutExpired("child", 1), KeyboardInterrupt()])
def test_child_interruption_gives_the_shell_a_chance_to_clean_up(monkeypatch, interrupted):
    class Child:
        pid = 12345
        calls = 0
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def poll(self):
            return None
        def wait(self, timeout):
            self.calls += 1
            if self.calls == 1:
                raise interrupted
            return 0
    child = Child()
    kills = []
    monkeypatch.setattr(support.subprocess, "Popen", lambda *a, **k: child)
    monkeypatch.setattr(support.os, "killpg", lambda *a: kills.append(a))
    with pytest.raises(type(interrupted)):
        support.run_child(["test"])
    assert kills == [(child.pid, signal.SIGTERM)]
    assert child.calls == 2


def test_topology_gate_cannot_pass_without_any_model_images(tmp_path, monkeypatch):
    import deployment_topology_gate as module
    gate = Gate(tmp_path, images=dict(support.IMAGE_TAGS))
    monkeypatch.setattr(gate, "sample_memory", lambda: None)
    monkeypatch.setattr(module, "image_id", lambda _: (_ for _ in ()).throw(RuntimeError("missing image")))
    with pytest.raises(RuntimeError):
        gate.run()
    assert gate.result["status"] == "failed"
    assert gate.result["phase"] == "identity"
    assert gate.result["cases"] == []


def test_cleanup_failure_cannot_leave_a_passed_report(tmp_path, monkeypatch):
    import deployment_topology_gate as module
    gate = Gate(tmp_path, images=dict(support.IMAGE_TAGS))
    gate.result["status"] = "passed"
    gate.last_context = SimpleNamespace(command=["docker", "compose"], environment={}, container_ids=lambda: [])
    monkeypatch.setattr(module, "container_states", lambda _: [])
    monkeypatch.setattr(module.subprocess, "run", lambda *a, **k: (_ for _ in ()).throw(
        subprocess.CalledProcessError(1, "docker")))
    with pytest.raises(subprocess.CalledProcessError):
        gate.cleanup()
    assert gate.result["status"] == "failed" and gate.result["cleanup_ok"] is False


@pytest.mark.parametrize("stack", ["litellm-presidio", "litellm-presidio-codex-lb"])
@pytest.mark.parametrize("topology", ["functional", "production"])
@pytest.mark.parametrize("count", [1, 2])
def test_topology_context_is_offline_isolated_and_uses_small_test_resources(tmp_path, stack, topology, count):
    gate = Gate(tmp_path, images=dict(support.IMAGE_TAGS))
    gate.ipv4_subnet = "172.28.0.0/16"
    before = gate.secrets()
    working_files = {path: path.read_bytes() for path in ROOT.glob("docker-compose*.yml")}
    ctx = gate.context(stack, topology, "mask", triton_replicas=count)
    overlay = json.loads(gate.overlay.read_text())
    assert overlay["networks"]["ru-llm-proxy"] == {
        "internal": True, "ipam": {"config": [{"subnet": "172.28.0.0/18"}]}}
    assert overlay["networks"]["test-ingress"] == {"ipam": {"config": [{"subnet": "172.28.64.0/18"}]}}
    used_networks = {network for service in ctx.model["services"].values() for network in service.get("networks", [])}
    assert used_networks == set(overlay["networks"])
    assert all(network.get("ipam", {}).get("config") for network in overlay["networks"].values())
    assert overlay["services"]["nginx"]["networks"][:2] == ["ru-llm-proxy", "test-ingress"]
    assert "container_name" not in overlay["services"]["ner-triton"]
    assert ctx.counts["ner-triton"] == (count if topology == "production" else 1)
    assert ctx.counts["presidio-analyzer"] == (2 if topology == "production" else 1)
    if topology == "production":
        assert ctx.values["LITELLM_MEMORY"] == "2g"
    assert gate.secrets() == before
    assert str(ROOT / ".env") != str(ctx.env_file)
    assert all(path.read_bytes() == body for path, body in working_files.items())


def test_topology_context_requires_subnet_without_contacting_docker(tmp_path, monkeypatch):
    import deployment_topology_gate as topology
    monkeypatch.setattr(topology, "docker", lambda *a: pytest.fail("Unexpected Docker call"))
    gate = Gate(tmp_path, images=dict(support.IMAGE_TAGS))
    with pytest.raises(RuntimeError, match="IPv4 subnet"):
        gate.context("litellm-presidio", "functional", "mask")
    assert not gate.overlay.exists() and gate.last_context is None


@pytest.mark.parametrize("subnet", ["172.28.0.0/16", "10.231.0.0/24", "10.231.0.0/25"])
@pytest.mark.parametrize("extended", [False, True])
def test_all_topology_networks_have_disjoint_explicit_subnets(subnet, extended):
    from deployment_topology_gate import topology_networks
    networks = topology_networks(subnet, extended)
    assert set(networks) == ({"ru-llm-proxy", "test-ingress", "codex-lb-proxy", "codex-lb-database"}
                             if extended else {"ru-llm-proxy", "test-ingress"})
    children = [ipaddress.IPv4Network(network["ipam"]["config"][0]["subnet"]) for network in networks.values()]
    assert all(child.subnet_of(ipaddress.IPv4Network(subnet)) and child.num_addresses >= 32 for child in children)
    assert not any(a.overlaps(b) for i, a in enumerate(children) for b in children[i + 1:])
    assert {name for name, network in networks.items() if network.get("internal")} == (
        {"ru-llm-proxy", "codex-lb-database"} if extended else {"ru-llm-proxy"})
    assert not any(network.get("external") or network.get("name") for network in networks.values())


@pytest.mark.parametrize("subnet", [None, "private-invalid", "fd00::/64", "172.28.0.1/16",
                                     "172.28.0.0/26", "172.28.0.0/32"])
def test_invalid_or_small_topology_subnet_fails_before_a_stand_is_created(subnet):
    from deployment_topology_gate import topology_networks
    with pytest.raises(RuntimeError, match="at least 30 hosts") as captured:
        topology_networks(subnet, True)
    assert "private" not in str(captured.value)


@pytest.mark.parametrize("configs", [
    [{"Subnet": "172.28.0.0/16", "Gateway": "172.28.0.1"}],
    [{"Subnet": "fd00::/64"}, {"Subnet": "172.28.0.0/16"}],
])
def test_test_network_subnet_is_read_from_docker_not_hardcoded(monkeypatch, configs):
    import triton_routing_checks as routing
    calls = []
    monkeypatch.setattr(routing, "output", lambda *args: calls.append(args) or json.dumps(
        [{"Internal": True, "IPAM": {"Config": configs}}]))
    assert routing.network_ipv4_subnet("test-only-network") == "172.28.0.0/16"
    assert calls == [("network", "inspect", "test-only-network")]


@pytest.mark.parametrize("data", [
    "not-json", "[]", "null", "{}", json.dumps([None]),
    json.dumps([{"Internal": True}]),
    *[json.dumps([{"Internal": True, "IPAM": {"Config": config}}]) for config in (
        None, {}, [], [{}], [{"Subnet": "private-invalid-value"}],
        [{"Subnet": "172.28.0.1/16"}], [{"Subnet": "172.28.0.0/31"}],
        [{"Subnet": "fd00::/64"}],
        [{"Subnet": "172.28.0.0/16"}, {"Subnet": "172.29.0.0/16"}],
    )],
    json.dumps([{"Internal": False, "IPAM": {"Config": [{"Subnet": "172.28.0.0/16"}]}}]),
])
def test_missing_or_invalid_test_network_subnet_fails_closed(monkeypatch, data):
    import triton_routing_checks as routing
    monkeypatch.setattr(routing, "output", lambda *args: data)
    with pytest.raises(RuntimeError, match="one usable IPv4 subnet") as captured:
        routing.network_ipv4_subnet("test-only-network")
    assert "private" not in str(captured.value)


@pytest.mark.parametrize("failure", [None, "metadata", "post", "cleanup", "interrupt"])
def test_no_replay_subnet_is_returned_only_after_successful_check_and_cleanup(monkeypatch, failure):
    import triton_routing_checks as routing
    calls = []

    def output(*args):
        calls.append(args)
        if args[:2] == ("network", "inspect"):
            if failure == "interrupt":
                raise KeyboardInterrupt
            return "[]" if failure == "metadata" else json.dumps([
                {"Internal": True, "IPAM": {"Config": [{"Subnet": "172.28.0.0/16"}]}}])
        if args[0] == "inspect":
            prefix = args[1].removesuffix("-nginx")
            return json.dumps([{"NetworkSettings": {"Networks": {prefix: {"IPAddress": "172.28.0.4"}}}}])
        if args[0] == "exec" and "urllib.error" in args[-1]:
            return "200" if failure == "post" else "503"
        if args[0] == "exec" and "/count" in args[-1]:
            return "0" if args[1].endswith("-a") else "1"
        return "test-output"

    cleanup = []
    def run(args, **kwargs):
        cleanup.append(args)
        return SimpleNamespace(returncode=1 if failure == "cleanup" and args[1:3] == ["network", "rm"] else 0)

    monkeypatch.setattr(routing, "output", output)
    monkeypatch.setattr(routing, "wait_ready", lambda *a: None)
    monkeypatch.setattr(routing.subprocess, "run", run)
    if failure:
        error = {"metadata": RuntimeError, "post": AssertionError, "cleanup": RuntimeError,
                 "interrupt": KeyboardInterrupt}[failure]
        with pytest.raises(error):
            routing.check_no_replay()
    else:
        assert routing.check_no_replay() == {"check": "single_post_no_replay", "status": "passed",
                                            "peer_post_counts": [0, 1], "ipv4_subnet": "172.28.0.0/16"}
    prefix = calls[0][-1]
    assert cleanup[0] == ["docker", "rm", "-f", prefix + "-nginx", prefix + "-a", prefix + "-b"]
    assert cleanup[1] == ["docker", "network", "rm", prefix]
    if failure == "cleanup":
        assert cleanup[2] == ["docker", "network", "inspect", prefix]


@pytest.mark.parametrize("cleanup_failed", [False, True])
def test_topology_run_uses_subnet_only_after_no_replay_cleanup(monkeypatch, tmp_path, cleanup_failed):
    import deployment_topology_gate as topology
    import triton_routing_checks as routing
    import analyzer_routing_checks as analyzer_routing
    import node_routing_checks as node_routing
    import node_role_checks as node_roles
    gate = Gate(tmp_path, images=dict(support.IMAGE_TAGS))
    calls = []
    result = {"check": "single_post_no_replay", "status": "passed", "ipv4_subnet": "172.28.0.0/16"}
    def no_replay():
        if cleanup_failed:
            raise RuntimeError("No-replay test network cleanup failed")
        return result
    def context(*args, **kwargs):
        calls.append(gate.ipv4_subnet)
        raise RuntimeError("Stop before starting test containers")
    monkeypatch.setattr(gate, "sample_memory", lambda: None)
    monkeypatch.setattr(gate, "context", context)
    monkeypatch.setattr(topology, "image_id", lambda value: value)
    monkeypatch.setattr(topology, "identities", lambda images: {})
    monkeypatch.setattr(routing, "check_no_replay", no_replay)
    monkeypatch.setattr(analyzer_routing, "check_analyzer_responses", lambda: {"status": "passed"})
    monkeypatch.setattr(node_routing, "check_node_responses", lambda **_: {"status": "passed"})
    monkeypatch.setattr(node_roles, "check_node_roles", lambda *_: {"status": "passed"})
    with pytest.raises(RuntimeError):
        gate.run()
    assert calls == ([] if cleanup_failed else ["172.28.0.0/16"])
    assert gate.ipv4_subnet == (None if cleanup_failed else "172.28.0.0/16")
    assert gate.result["checks"] == ([] if cleanup_failed else [result, *[{"status": "passed"}] * 3])


def test_all_new_analyzer_tests_run_in_baseline_ci():
    workflow = yaml.safe_load((ROOT / ".github/workflows/baseline.yml").read_text())
    steps = workflow["jobs"]["recognizer-api"]["steps"]
    assert any(step.get("run") == "make test-recognizers" for step in steps)
    for module in ("test_triton_resilience.py", "test_onnx_artifact.py", "test_numpy_runtime.py",
                   "test_analyzer_budget.py", "test_ner_value_repair.py", "test_triton_service.py"):
        assert (ROOT / "presidio/tests" / module).is_file()


def test_ci_paths_include_every_shared_runtime_test_helper():
    import fnmatch
    workflow = yaml.safe_load((ROOT / ".github/workflows/ner-model-integration.yml").read_text())
    triggers = workflow.get("on", workflow.get(True))
    for event in ("push", "pull_request"):
        patterns = triggers[event]["paths"]
        for name in ("model_smoke.py", "ner_integration.py", "ner_test_support.py",
                     "triton_routing_checks.py", "deployment_topology_gate.py",
                     "node_routing_checks.py", "node_timeout_budget_checks.py", "node_role_checks.py",
                     "mock_node_analyzer.py", "mock_syn_blackhole.py", "syn_capability_check.py",
                     "topology-resources.env", "nginx.ner-proxy.conf", "check_responses_history.py"):
            assert any(fnmatch.fnmatch("tests/e2e/" + name, pattern) for pattern in patterns), (event, name)
    assert workflow["permissions"] == {"contents": "read"}
    assert "pull_request_target" not in triggers


def test_independent_groups_continue_but_any_failure_is_fatal():
    calls, results = [], []
    def fail():
        calls.append("failed")
        raise AssertionError(("known_case", "do not include this request body"))
    with pytest.raises(RuntimeError, match="independent"):
        support.run_checks([("first", fail), ("next", lambda: calls.append("passed"))],
                           results, case_ids=["known_case"])
    assert calls == ["failed", "passed"]
    assert results == [
        {"check": "first", "status": "failed", "error_type": "AssertionError", "case_id": "known_case"},
        {"check": "next", "status": "passed"},
    ]
    assert "request body" not in json.dumps(results)


def test_successful_independent_groups_do_not_report_a_failure():
    results = []
    support.run_checks([("one", lambda: None), ("two", lambda: None)], results)
    assert [row["status"] for row in results] == ["passed", "passed"]


@pytest.mark.parametrize("exc", [KeyboardInterrupt(), SystemExit(143)])
def test_interruption_does_not_start_more_independent_groups(exc):
    results, calls = [], []
    def stop():
        raise exc
    with pytest.raises(type(exc)):
        support.run_checks([("stop", stop), ("never", lambda: calls.append(True))], results)
    assert not calls
    assert results == [{"check": "stop", "status": "interrupted", "error_type": type(exc).__name__}]


def test_arbitrary_exception_text_is_not_a_reported_case_id():
    results = []
    with pytest.raises(RuntimeError):
        support.run_checks([("private", lambda: (_ for _ in ()).throw(
            ValueError(("not-a-corpus-id", "private data"))))], results, case_ids=["known_case"])
    assert "not-a-corpus-id" not in json.dumps(results)
    assert "private data" not in json.dumps(results)


def test_known_case_prefix_and_http_code_are_value_free_diagnostics():
    from urllib.error import HTTPError
    results = []
    checks = [("assertion", lambda: (_ for _ in ()).throw(AssertionError("known_case: private request body"))),
              ("http", lambda: (_ for _ in ()).throw(HTTPError("http://private", 503, "private error", {}, None)))]
    with pytest.raises(RuntimeError):
        support.run_checks(checks, results, case_ids=["known_case"])
    assert results[0]["case_id"] == "known_case"
    assert results[1]["http_status"] == 503
    assert "private" not in json.dumps(results)


@pytest.mark.parametrize("stderr", [
    "docker: private-token: invalid endpoint settings: user specified IP address is supported only when connecting to networks with user configured subnets private-body",
    b"docker: private-token: invalid endpoint settings: user specified IP address is supported only when connecting to networks with user configured subnets private-body\xff",
])
def test_recovery_diagnostic_reports_only_canonical_error_signatures(stderr):
    import triton_routing_checks as routing
    exc = subprocess.CalledProcessError(125, ["docker", "run", "--env", "private-token"],
                                       output="private-stdout", stderr=stderr)
    result = routing._command_diagnostic(exc)
    assert result["operation"] == "docker run" and result["returncode"] == 125
    assert result["stderr_classes"] == ["explicit_ip_requires_configured_subnet", "invalid_endpoint_settings"]
    assert "user configured subnet" in result["stderr_excerpt"]
    assert "private" not in json.dumps(result)


@pytest.mark.parametrize("stderr", [None, "", "Bearer private-token\nrequest=private-body", b"\xffprivate-token"])
def test_unrecognized_recovery_stderr_is_not_disclosed(stderr):
    import triton_routing_checks as routing
    result = routing._command_diagnostic(subprocess.CalledProcessError(1, ["docker", "exec", "private"], stderr=stderr))
    assert result["stderr_classes"] == []
    assert result["stderr_excerpt"] == "stderr omitted: no allowlisted diagnostic"
    assert "private" not in json.dumps(result)


@pytest.mark.parametrize("phase", ["network-setup", "fixture-startup", "healthy-head", "connect-timeout"])
def test_node_routing_phase_uses_fixed_labels(phase, capsys):
    import node_routing_checks as routing
    progress = {}
    routing.routing_phase(progress, phase)
    assert progress == {"phase": phase, "status": "running"}
    assert json.loads(capsys.readouterr().out.removeprefix("NER NODE ROUTING ")) == progress
    with pytest.raises(ValueError, match="Unknown"):
        routing.routing_phase(progress, "private-token")
    assert "private" not in json.dumps(progress) + capsys.readouterr().out


@pytest.mark.parametrize("exception", [
    AssertionError(("private request", {"Authorization": "Bearer private-token"})),
    subprocess.TimeoutExpired(["docker", "exec", "private-token"], 1,
                              output=b"private stdout", stderr=b"private stderr"),
    subprocess.CalledProcessError(125, ["docker", "run", "private-token"],
                                 output="private stdout", stderr="address already in use private-token"),
    KeyboardInterrupt(), SystemExit(143),
])
def test_node_routing_failure_does_not_disclose_exception_values(exception):
    import node_routing_checks as routing
    result = routing.routing_failure("fixture-startup", exception)
    assert result["phase"] == "fixture-startup"
    assert result["status"] == ("failed" if isinstance(exception, Exception) else "interrupted")
    assert result["error_type"] == type(exception).__name__
    if isinstance(exception, subprocess.CalledProcessError):
        assert result["operation"] == "docker run" and result["returncode"] == 125
        assert result["stderr_classes"] == ["address_in_use"]
    else:
        assert result["operation"] == "check"
    assert "private" not in json.dumps(result)
    with pytest.raises(ValueError, match="Unknown"):
        routing.routing_failure("private-token", exception)


@pytest.mark.parametrize("raw", ["private stdout " * 5000, b"private stdout\xff" * 5000])
@pytest.mark.parametrize("timeout", [False, True])
def test_node_command_output_is_private_and_bounded(tmp_path, raw, timeout):
    import node_routing_checks as routing
    exc = (subprocess.TimeoutExpired(["private command"], 1, output=raw, stderr=raw) if timeout else
           subprocess.CalledProcessError(125, ["private command"], output=raw, stderr=raw))
    routing.private_command_output(tmp_path, exc)
    for field in ("stdout", "stderr"):
        path = tmp_path / ("command." + field)
        assert path.stat().st_mode & 0o777 == 0o600
        assert len(path.read_bytes()) == 16384
        assert b"private stdout" in path.read_bytes()
    assert sorted(path.name for path in tmp_path.iterdir()) == ["command.stderr", "command.stdout"]


@pytest.mark.parametrize("failure", [None, "bind", "filter", "filter-errno", "listen", "marker", "waiting"])
def test_syn_fixture_reports_stages_without_changing_socket_or_wait(monkeypatch, capsys, failure):
    import mock_syn_blackhole as syn
    operations, filters, closed = [], [], []
    exc = KeyboardInterrupt() if failure == "waiting" else RuntimeError("private-body")
    def operation(name, value):
        operations.append((name, value))
        if failure == name:
            raise exc
    class Listener:
        def __enter__(self):
            return self
        def __exit__(self, *args):
            closed.append(True)
        def bind(self, address):
            operation("bind", address)
        def fileno(self):
            return 17
        def listen(self, backlog):
            operation("listen", backlog)
    def setsockopt(fd, level, option, pointer, size):
        program = pointer._obj
        instruction = program.instructions[0]
        filters.append((fd, level, option, size, program.length, instruction.code, instruction.k))
        operation("filter", option)
        return 1 if failure == "filter-errno" else 0
    def path(value):
        assert value == "/tmp/blackhole-ready"
        return SimpleNamespace(touch=lambda: operation("marker", value))
    monkeypatch.setattr(syn.socket, "socket", lambda: Listener())
    monkeypatch.setattr(syn.ctypes, "CDLL", lambda *a, **k: SimpleNamespace(setsockopt=setsockopt))
    monkeypatch.setattr(syn.ctypes, "get_errno", lambda: 13)
    monkeypatch.setattr(syn, "Path", path)
    monkeypatch.setattr(syn.threading, "Event", lambda: SimpleNamespace(wait=lambda seconds: operation("waiting", seconds)))
    if failure:
        with pytest.raises(PermissionError if failure == "filter-errno" else type(exc)) as caught:
            syn.serve()
        if failure != "filter-errno":
            assert caught.value is exc
    else:
        syn.serve()
        assert operations == [("bind", ("0.0.0.0", 5001)), ("filter", 26),
                              ("listen", 8), ("marker", "/tmp/blackhole-ready"), ("waiting", 600)]
    assert closed == [True]
    if failure != "bind":
        assert filters == [(17, syn.socket.SOL_SOCKET, 26, syn.ctypes.sizeof(syn.Program), 1, 0x06, 0)]
    public = capsys.readouterr().out
    assert "private-body" not in public
    events = [json.loads(line.removeprefix(syn.EVENT_PREFIX)) for line in public.splitlines()]
    if failure:
        last = events[-1]
        assert last["phase"] == ("filter" if failure == "filter-errno" else failure)
        assert last["status"] == ("interrupted" if failure == "waiting" else "failed")
        assert last["errno"] == (13 if failure == "filter-errno" else None)
    else:
        assert events[-2:] == [{"phase": "marker", "status": "passed"}, {"phase": "waiting", "status": "started"}]


def test_syn_fixture_event_rejects_untrusted_labels_and_error_values(capsys):
    import mock_syn_blackhole as syn
    class PrivateError(RuntimeError):
        errno = "private-errno"
    syn.emit("marker", "failed", PrivateError("private-body"))
    with pytest.raises(ValueError):
        syn.emit("private-phase", "failed")
    public = capsys.readouterr().out
    assert "private" not in public and "PrivateError" not in public
    assert json.loads(public.removeprefix(syn.EVENT_PREFIX))["error_type"] == "OtherError"


@pytest.mark.parametrize("code,stderr,category", [
    (0, b"", "ready"), (1, b"", "marker_not_confirmed"),
    (127, b'exec: "test": executable file not found; private-token', "probe_executable_missing"),
    (1, b"Container private-container is not running", "container_not_running"),
    (126, b"private-body", "command_failed"),
])
def test_syn_readiness_retains_original_command_and_reports_only_safe_values(monkeypatch, code, stderr, category):
    import node_routing_checks as routing
    calls, progress = [], {}
    def run(args, **kwargs):
        calls.append((args, kwargs))
        return SimpleNamespace(returncode=code, stdout=b"private-stdout", stderr=stderr)
    monkeypatch.setattr(routing.subprocess, "run", run)
    assert routing.syn_listener_ready("fixture", progress) is (code == 0)
    assert progress == {"attempts": 1, "last_probe": {"returncode": code, "category": category}}
    assert calls == [(["docker", "exec", "fixture", "test", "-f", "/tmp/blackhole-ready"],
                      {"capture_output": True, "timeout": 10})]
    assert "private" not in json.dumps(progress)


def test_syn_readiness_timeout_is_not_hidden_or_retried(monkeypatch):
    import node_routing_checks as routing
    exc = subprocess.TimeoutExpired(["private-command"], 10, stderr=b"private-token")
    calls, progress = [], {}
    def run(*args, **kwargs):
        calls.append(args)
        raise exc
    monkeypatch.setattr(routing.subprocess, "run", run)
    with pytest.raises(subprocess.TimeoutExpired) as caught:
        routing.syn_listener_ready("fixture", progress)
    assert caught.value is exc and len(calls) == 1
    assert progress["last_probe"] == {"returncode": None, "category": "command_timeout"}
    assert "private" not in json.dumps(progress)


def test_syn_probe_classifies_stdout_without_disclosing_it():
    import node_routing_checks as routing
    result = routing.syn_probe_result(SimpleNamespace(returncode=127, stderr=b"", stdout=
        b'OCI runtime exec failed: exec: "test": executable file not found in $PATH private-token'))
    assert result == {"returncode": 127, "category": "probe_executable_missing"}
    assert "private" not in json.dumps(result)


@pytest.mark.parametrize("admin", [False, True])
def test_syn_launch_only_grants_explicit_net_admin(admin):
    import node_routing_checks as routing
    args = routing.syn_fixture_args("fixture", "none", net_admin=admin)
    assert args[:6] == ["run", "-d", "--name", "fixture", "--network", "none"]
    assert args[args.index("--cap-drop") + 1] == "ALL"
    assert ("--cap-add" in args) is admin
    if admin:
        assert args[args.index("--cap-add") + 1] == "NET_ADMIN" and args.count("--cap-add") == 1
    assert args[args.index("--security-opt") + 1] == "no-new-privileges"
    assert args[args.index("-v") + 1] == f"{ROOT}/tests/e2e/mock_syn_blackhole.py:/fixture.py:ro"
    assert not any(value in args for value in ("--privileged", "--pid", "--env", "-p", "--device"))
    assert routing.syn_fixture_args("fixture", "none") == routing.syn_fixture_args("fixture", "none", net_admin=False)


@pytest.mark.parametrize("invalid", [None, "extra-cap", "privileged", "host-network", "ports", "mounts", "security"])
def test_syn_security_inspects_actual_flags_without_disclosing_config(monkeypatch, invalid):
    import node_routing_checks as routing
    data = {"add": ["CAP_NET_ADMIN"], "drop": ["ALL"], "privileged": False,
            "security": ["no-new-privileges"], "network": "none", "ports": {},
            "binds": [f"{ROOT}/tests/e2e/mock_syn_blackhole.py:/fixture.py:ro"], "Env": ["private-token"]}
    if invalid == "extra-cap":
        data["add"].append("CAP_SYS_ADMIN")
    elif invalid == "privileged":
        data["privileged"] = True
    elif invalid == "host-network":
        data["network"] = "host"
    elif invalid == "ports":
        data["ports"] = {"5001/tcp": [{"HostPort": "5001"}]}
    elif invalid == "mounts":
        data["binds"].append("/var/run/docker.sock:/var/run/docker.sock")
    elif invalid == "security":
        data["security"] = ["seccomp=unconfined"]
    monkeypatch.setattr(routing.subprocess, "run", lambda *a, **k: SimpleNamespace(stdout=json.dumps(data)))
    if invalid:
        with pytest.raises(AssertionError) as captured:
            routing.syn_fixture_security("fixture", "none", net_admin=True)
        assert "private" not in str(captured.value)
    else:
        result = routing.syn_fixture_security("fixture", "none", net_admin=True)
        assert result["net_admin"] is True and result["other_capabilities"] is False
        assert "private" not in json.dumps(result)


@pytest.mark.parametrize("control", ["eperm", "ready", "unrelated"])
@pytest.mark.parametrize("positive_ready", [False, True])
def test_syn_capability_ab_is_sequential_bounded_and_never_accepts_unrelated_failure(monkeypatch, control, positive_ready):
    import syn_capability_check as check
    image = "sha256:" + "a" * 64
    launches, removed = [], []
    current = {"admin": False}
    def output(*args):
        if args[0] == "run":
            launches.append(args)
            current["admin"] = "--cap-add" in args
            assert args[args.index("--network") + 1] == "none"
            return "container"
        return json.dumps(dict.fromkeys(("CapEff", "CapPrm", "CapBnd"), 4096 if current["admin"] else 0))
    def wait(probe, timeout):
        assert timeout == 10
        if not probe():
            raise TimeoutError
    def diagnostic(*args):
        return {"container": {"image_id": image, "exit_code": 1, "oom_killed": control == "unrelated"},
                "events": [{"phase": "filter", "status": "failed", "errno": 1}]}
    def run(args, **kwargs):
        if args[:3] == ["docker", "rm", "-f"]:
            removed.append(args[-1])
        return SimpleNamespace(returncode=1 if args[:2] == ["docker", "inspect"] else 0)
    monkeypatch.setattr(check, "output", output)
    monkeypatch.setattr(check, "wait_ready", wait)
    monkeypatch.setattr(check, "syn_listener_ready", lambda *a: positive_ready if current["admin"] else control == "ready")
    monkeypatch.setattr(check, "syn_fixture_diagnostic", diagnostic)
    monkeypatch.setattr(check, "syn_fixture_security", lambda *a, **k: {"net_admin": k["net_admin"]})
    monkeypatch.setattr(check.subprocess, "run", run)
    report = {}
    if control == "unrelated" or not positive_ready:
        with pytest.raises(AssertionError):
            check.check_capabilities(report)
        assert report["status"] == "failed"
    else:
        check.check_capabilities(report)
        assert report["status"] == "passed"
        assert report["cases"][1]["connect_timeout"] == "passed"
    assert len(launches) == len(removed) == (1 if control == "unrelated" else 2)
    assert all(row["cleanup_ok"] for row in report["cases"])
    if len(launches) == 2:
        assert launches[1][-3] == image
    assert "--cap-add" not in launches[0]


def test_syn_ab_precedes_model_build_and_saves_only_bounded_report():
    workflow = yaml.safe_load((ROOT / ".github/workflows/ner-model-integration.yml").read_text())
    steps = workflow["jobs"]["real-model-integration"]["steps"]
    names = [step["name"] for step in steps]
    assert names.index("Check isolated SYN fixture capability controls") < names.index("Build pinned Analyzer image")
    step = steps[names.index("Check isolated SYN fixture capability controls")]
    assert not step.get("continue-on-error")
    saved = steps[names.index("Save bounded SYN capability report")]
    assert saved["if"] == "always()" and saved["with"]["path"] == "tmp/syn-capability-ab.json"
    assert saved["with"]["retention-days"] == 7


def test_transport_grants_net_admin_only_at_the_explicit_syn_fixture_call():
    import ast
    tree = ast.parse((ROOT / "tests/e2e/node_routing_checks.py").read_text())
    scenario = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "check_node_responses")
    launches = [node for node in ast.walk(scenario) if isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name) and node.func.id == "syn_fixture_args"]
    assert len(launches) == 1 and ast.unparse(launches[0].args[0]) == "blackhole"
    assert next(item.value.value for item in launches[0].keywords if item.arg == "net_admin") is True
    checks = [node for node in ast.walk(scenario) if isinstance(node, ast.Call)
              and isinstance(node.func, ast.Name) and node.func.id == "syn_fixture_security"]
    assert len(checks) == 1 and ast.unparse(checks[0].args[0]) == "blackhole"
    assert sum(isinstance(node, ast.Constant) and node.value == "--cap-add" for node in ast.walk(tree)) == 1
    assert not any(isinstance(node, ast.Constant) and node.value in {"--privileged", "--pid=host"}
                   for node in ast.walk(tree) if isinstance(node, ast.Constant) and isinstance(node.value, str))


@pytest.mark.parametrize("architecture", ["amd64", "arm64", "private-architecture"])
def test_syn_snapshot_is_bounded_and_does_not_publish_logs_or_env(monkeypatch, architecture):
    import node_routing_checks as routing
    identity = "sha256:" + "a" * 64
    events = [{"phase": "filter", "status": "failed", "error_type": "PermissionError", "errno": 13,
               "Authorization": "private-token"}] * 30
    raw = b"private-log\nSYN_FIXTURE broken private-body\n" + b"\n".join(
        (routing.EVENT_PREFIX + json.dumps(event)).encode() for event in events)
    calls = []
    def run(args, **kwargs):
        calls.append(args)
        if args[1] == "inspect":
            value = json.dumps({"running": False, "exit_code": 1, "oom_killed": False,
                                "image_id": identity, "Env": ["PASSWORD=private-token"]}).encode()
        elif args[1] == "image":
            value = json.dumps(architecture).encode()
        elif args[1] == "exec":
            value = b"1\n"
        else:
            value = raw
        return SimpleNamespace(returncode=0, stdout=value, stderr=b"private-stderr")
    monkeypatch.setattr(routing.subprocess, "run", run)
    report = routing.syn_fixture_diagnostic("fixture", {
        "attempts": 10, "private-key": "private-token",
        "last_probe": {"returncode": 127, "category": "probe_executable_missing", "stderr": "private-stderr"}})
    assert report["container"] == {"running": False, "exit_code": 1, "oom_killed": False, "image_id": identity}
    assert report["architecture"] == (architecture if architecture in {"amd64", "arm64"} else "unreported")
    assert report["marker_present"] is True
    assert report["readiness"]["last_probe"]["returncode"] == 127
    assert len(report["events"]) == 12 and report["events"][0] == {
        "phase": "filter", "status": "failed", "error_type": "PermissionError", "errno": 13}
    assert report["errors"] == [] and len(calls) == 4
    assert "private" not in json.dumps(report) and len(json.dumps(report)) < 4096


def test_syn_snapshot_partial_collection_does_not_raise_or_disclose_error(monkeypatch):
    import node_routing_checks as routing
    def run(*args, **kwargs):
        raise subprocess.TimeoutExpired("private-command", 10, stderr=b"private-token")
    monkeypatch.setattr(routing.subprocess, "run", run)
    result = routing.syn_fixture_diagnostic("fixture", {"attempts": "private-value", "last_probe": "private-token"})
    assert len(result["errors"]) == 3 and result["events"] == []
    assert "private" not in json.dumps(result)


@pytest.mark.parametrize("collection_fails", [False, True])
def test_syn_diagnostics_never_turn_readiness_timeout_into_success(monkeypatch, collection_fails, capsys):
    import node_routing_checks as routing
    exc, progress = TimeoutError("private-timeout"), {}
    def phase(report, _):
        report.update(phase="connect-timeout", status="running")
        raise exc
    def snapshot(*args):
        if collection_fails:
            raise RuntimeError("private-collection-error")
        return {"marker_present": True}
    monkeypatch.setattr(routing, "routing_phase", phase)
    monkeypatch.setattr(routing, "syn_fixture_diagnostic", snapshot)
    monkeypatch.setattr(routing.subprocess, "run", lambda *a, **k:
                        SimpleNamespace(stdout=b"", stderr=b"", returncode=0))
    with pytest.raises(TimeoutError) as caught:
        routing.check_node_responses(diagnostic=progress)
    assert caught.value is exc and progress["status"] == "failed"
    assert progress["syn_fixture"] == ({"diagnostic_status": "collection_failed"} if collection_fails else {"marker_present": True})
    public = json.dumps(progress) + capsys.readouterr().out
    assert "private-timeout" not in public and "private-collection-error" not in public


def test_node_routing_failure_identifies_command_line_without_changing_failure_or_cleanup(monkeypatch, tmp_path, capsys):
    import node_routing_checks as routing
    progress, commands, cleanup = {}, [], []
    exc = subprocess.CalledProcessError(125, ["docker", "run", "private-token"],
        output=b"private stdout", stderr=b"private-token: invalid endpoint settings: user specified IP address is supported only when connecting to networks with user configured subnets")
    def output(*args):
        commands.append(args)
        if args[0] == "run" and "--ip" in args:
            raise exc
        return ""
    def configure(name):
        commands.append(("configure-test-network", name))
        return ipaddress.IPv4Network("172.28.0.0/16")
    def run(args, **kwargs):
        cleanup.append(args)
        return SimpleNamespace(stdout=b"private log", stderr=b"", returncode=0)
    mkdtemp = routing.tempfile.mkdtemp
    def private_directory(*args, **kwargs):
        if kwargs.get("prefix") == "ru-node-routing-diagnostic-":
            kwargs["dir"] = tmp_path
        return mkdtemp(*args, **kwargs)
    monkeypatch.setattr(routing, "output", output)
    monkeypatch.setattr(routing, "configure_test_ipv4_subnet", configure)
    monkeypatch.setattr(routing.subprocess, "run", run)
    monkeypatch.setattr(routing.tempfile, "mkdtemp", private_directory)
    with pytest.raises(subprocess.CalledProcessError) as captured:
        routing.check_node_responses(diagnostic=progress)
    assert captured.value is exc
    assert progress["phase"] == "fixture-startup" and progress["returncode"] == 125
    assert progress["operation"] == "docker run"
    assert progress["stderr_classes"] == ["explicit_ip_requires_configured_subnet", "invalid_endpoint_settings"]
    origin = progress["origin"]
    assert origin["file"] == "node_routing_checks.py" and origin["function"] == "check_node_responses"
    line = (ROOT / "tests/e2e/node_routing_checks.py").read_text().splitlines()[origin["line"] - 1]
    assert 'output("run", "-d", "--name", client' in line
    assert [command[:2] for command in commands] == [
        ("network", "create"), ("network", "create"), ("network", "create"),
        ("configure-test-network", commands[3][1]), ("run", "-d"), ("run", "-d"), ("run", "-d")]
    assert sum(command[:3] == ["docker", "rm", "-f"] for command in cleanup) == 1
    assert sum(command[:3] == ["docker", "network", "rm"] for command in cleanup) == 3
    public = json.dumps(progress) + capsys.readouterr().out
    for secret in ("private-token", "private stdout", "private log"):
        assert secret not in public
    directory, = tmp_path.iterdir()
    assert directory.stat().st_mode & 0o777 == 0o700
    assert (directory / "command.stderr").read_bytes() == exc.stderr


def test_node_routing_failure_details_survive_in_existing_topology_report(monkeypatch, tmp_path):
    import deployment_topology_gate as topology
    import triton_routing_checks as routing
    import analyzer_routing_checks as analyzer_routing
    import node_routing_checks as node_routing
    gate = Gate(tmp_path, images=dict(support.IMAGE_TAGS))
    exc = subprocess.CalledProcessError(125, ["docker", "run", "private-token"], stderr="private-body")
    diagnostic = node_routing.routing_failure("fixture-startup", exc)
    def fail(*, budget_probe, diagnostic):
        diagnostic.update(node_routing.routing_failure("fixture-startup", exc))
        raise exc
    monkeypatch.setattr(gate, "sample_memory", lambda: None)
    monkeypatch.setattr(gate, "capture_failure", lambda _: None)
    monkeypatch.setattr(topology, "image_id", lambda value: value)
    monkeypatch.setattr(topology, "identities", lambda images: {})
    monkeypatch.setattr(routing, "check_no_replay", lambda: {"ipv4_subnet": "172.28.0.0/16"})
    monkeypatch.setattr(analyzer_routing, "check_analyzer_responses", lambda: {"status": "passed"})
    monkeypatch.setattr(node_routing, "check_node_responses", fail)
    with pytest.raises(subprocess.CalledProcessError) as captured:
        gate.run()
    assert captured.value is exc and gate.result["node_routing"] == diagnostic
    assert gate.result["status"] == "failed" and gate.result["phase"] == "node-routing"
    report = tmp_path / "topologies.json"
    support.write_report(report, gate.result)
    assert json.loads(report.read_text())["node_routing"] == diagnostic
    assert "private" not in report.read_text()


@pytest.mark.parametrize("command", ["private-token", ["private-token"], ["docker", {"private": "token"}]])
def test_unrecognized_recovery_argv_and_invalid_returncode_are_not_serialized(command):
    import triton_routing_checks as routing
    result = routing._command_diagnostic(subprocess.CalledProcessError("private-returncode", command))
    assert result["operation"] == "unreported-command" and result["returncode"] is None
    assert "private" not in json.dumps(result)


def test_recovery_diagnostic_is_bounded_and_does_not_search_unbounded_stderr():
    import triton_routing_checks as routing
    stderr = "private-token" * 10000 + "address already in use"
    result = routing._command_diagnostic(subprocess.CalledProcessError(1, ["docker", "run"], stderr=stderr))
    assert result["stderr_classes"] == []
    stderr = "no such container; address already in use; permission denied; private-body" * 10000
    result = routing._command_diagnostic(subprocess.CalledProcessError(1, ["docker", "run"], stderr=stderr))
    assert result["stderr_classes"] == ["address_in_use", "container_missing", "permission_denied"]
    assert len(result["stderr_excerpt"]) <= 512
    assert len(json.dumps(result)) < 2048
    assert "private" not in json.dumps(result)


@pytest.mark.parametrize("exception", [AssertionError("private assertion"),
                                     subprocess.CalledProcessError(125, ["docker", "run", "private-token"], stderr="address already in use; private-body"),
                                     KeyboardInterrupt(), SystemExit(143)])
def test_recovery_substep_preserves_failure_or_interruption(exception, capsys):
    import triton_routing_checks as routing
    gate = SimpleNamespace(result={})
    with pytest.raises(type(exception)) as captured:
        with routing._recovery_step(gate, "reserve-old-address"):
            raise exception
    assert captured.value is exception
    row = gate.result["recovery_steps"][0]
    assert row["status"] == ("failed" if isinstance(exception, Exception) else "interrupted")
    assert row["error_type"] == type(exception).__name__
    assert "private" not in json.dumps(gate.result) + capsys.readouterr().out


def test_recovery_substep_reports_success_without_accepting_arbitrary_labels(capsys):
    import triton_routing_checks as routing
    gate = SimpleNamespace(result={})
    with routing._recovery_step(gate, "select-replicas"):
        pass
    assert gate.result["recovery_steps"] == [{"step": "select-replicas", "status": "passed"}]
    with pytest.raises(ValueError, match="Unknown"):
        with routing._recovery_step(gate, "private-token"):
            pytest.fail("Unexpected operation")
    assert "private" not in json.dumps(gate.result) + capsys.readouterr().out


@pytest.mark.parametrize("failure", [None, "reserve-old-address", "reject-mismatched-manifest"])
def test_recovery_substeps_do_not_change_commands_assertions_or_cleanup(monkeypatch, failure, capsys):
    import deployment_topology_gate as topology
    import triton_routing_checks as routing
    from deployment import DeploymentError

    calls = []
    original = [{"Id": str(i), "Config": {"Labels": {"com.docker.compose.service": "ner-triton"}},
                 "NetworkSettings": {"Networks": {"test-network": {"IPAddress": f"172.28.1.{i}"}}}}
                for i in (2, 3)]
    replacement = {"Id": "4", "Config": original[0]["Config"],
                   "NetworkSettings": {"Networks": {"test-network": {"IPAddress": "172.28.1.4"}}}}
    state = {"health": 0, "started": False}
    gate = SimpleNamespace(result={"checks": []}, project="test-recovery", url=lambda *a: "http://test")

    def health(*args):
        calls.append(("health", *args))
        state["health"] += 1
        if state["health"] in (1, 3) and failure != "reject-mismatched-manifest":
            raise DeploymentError("expected rejection")

    def start():
        calls.append(("start",))
        state["started"] = True

    ctx = SimpleNamespace(health=health, start=start,
                          containers=lambda: [original[1], replacement] if state["started"] else original)
    command_error = subprocess.CalledProcessError(125, ["docker", "run", "private-token"],
                                                 stderr="invalid endpoint settings: address already in use; private-body")

    def output(*args):
        calls.append(args)
        if failure == "reserve-old-address" and args[0] == "run":
            raise command_error
        return "test-output"

    monkeypatch.setattr(routing, "output", output)
    monkeypatch.setattr(routing.subprocess, "run", lambda args, **kw: calls.append(tuple(args)) or SimpleNamespace(returncode=0))
    monkeypatch.setattr(topology, "request", lambda *a, **k: (200, "+79031234569"))
    monkeypatch.setattr(topology, "assert_request", lambda *a, **k: calls.append(("assert-restored", *a[1:])))
    if failure is not None:
        expected = subprocess.CalledProcessError if failure == "reserve-old-address" else AssertionError
        with pytest.raises(expected) as captured:
            routing.check_replica_recovery(gate, ctx, "private-virtual-key")
        if failure == "reserve-old-address":
            assert captured.value is command_error
        assert not state["started"] and gate.result["checks"] == []
        assert any(row["step"] == failure and row["status"] == "failed" for row in gate.result["recovery_steps"])
    else:
        routing.check_replica_recovery(gate, ctx, "private-virtual-key")
        assert all(row["status"] == "passed" for row in gate.result["recovery_steps"])
        assert gate.result["checks"][0] == {"check": "triton_replica_recovery", "status": "passed",
                                          "wrong_artifact_rejected": True, "stopped_replica_rejected": True,
                                          "client_attempt_statuses": [200], "replacement_new_ip": True}
    if failure == "reject-mismatched-manifest":
        assert len([call for call in calls if call[0] == "exec"]) == 2
        assert not any(call[0] == "stop" for call in calls)
    else:
        assert ("stop", "2") in calls and ("rm", "2") in calls
        assert ("run", "-d", "--name", "test-recovery-address-holder", "--network", "test-network", "--ip", "172.28.1.2",
                "python:3.12-slim", "sleep", "240") in calls
        assert calls[-1] == ("docker", "rm", "-f", "test-recovery-address-holder")
    assert "private" not in json.dumps(gate.result) + capsys.readouterr().out


def test_proxy_failure_does_not_skip_topologies(monkeypatch, tmp_path):
    import deployment_topology_gate as topology
    calls = []
    class FakeGate:
        def __init__(self, *a, **k):
            self.result = {"status": "running"}
        def run(self):
            calls.append("topologies")
            self.result["status"] = "passed"
    monkeypatch.setattr(topology, "Gate", FakeGate)
    monkeypatch.setattr(ner_integration, "image_id", lambda value: value)
    monkeypatch.setattr(ner_integration, "identities", lambda images: {"revision": "test"})
    monkeypatch.setattr(ner_integration, "build_images", lambda *a, **k: None)
    monkeypatch.setattr(ner_integration, "run_child", lambda *a, **k:
                        (_ for _ in ()).throw(RuntimeError("proxy failure")))
    with pytest.raises(RuntimeError):
        ner_integration.run("proxy", report_dir=tmp_path)
    assert calls == ["topologies"]
    report = json.loads((tmp_path / "integration-proxy.json").read_text())
    assert report["status"] == "failed"
    assert [item["status"] for item in report["checks"]] == ["failed", "passed"]
    assert json.loads((tmp_path / "topologies.json").read_text())["status"] == "passed"


def test_model_only_success_cannot_overwrite_a_failed_full_run(monkeypatch, tmp_path):
    aggregate = tmp_path / "integration-all.json"
    aggregate.write_text('{"status":"failed","suite":"all"}')
    monkeypatch.setattr(ner_integration, "image_id", lambda value: value)
    monkeypatch.setattr(ner_integration, "identities", lambda images: {"revision": "test"})
    monkeypatch.setattr(model_smoke, "run", lambda *a, **k: None)
    ner_integration.run("model", report_dir=tmp_path)
    assert json.loads(aggregate.read_text())["status"] == "failed"
    assert json.loads((tmp_path / "integration-model.json").read_text())["status"] == "passed"


def test_instruction_failure_does_not_skip_other_groups(monkeypatch, tmp_path):
    from tests.e2e import ner_instruction_checks as module
    calls = []
    monkeypatch.setattr(sys, "argv", ["instruction-checks"] + [
        item for name in ("analyzer", "proxy", "block-proxy", "capture")
        for item in ("--" + name + "-url", "http://test")])
    monkeypatch.setenv("NER_TEST_REPORT_DIR", str(tmp_path))
    monkeypatch.setenv("PRESIDIO_ANALYZER_BASE64_POLICY", "content")
    def fail(*args):
        raise AssertionError(("dialogue_role_which", "unchanged check"))
    monkeypatch.setattr(module, "check_dialogue_arguments", fail)
    monkeypatch.setattr(module, "check_instructions", lambda *a: calls.append("instructions"))
    monkeypatch.setattr(module, "check_names", lambda *a, **k: calls.append("names"))
    monkeypatch.setattr(module, "check_accepted_coverage", lambda *a: calls.append("accepted-coverage"))
    with pytest.raises(RuntimeError):
        module.main()
    assert calls == ["instructions", "names", "names", "names", "accepted-coverage"]
    report = json.loads((tmp_path / "instructions-content.json").read_text())
    assert report["status"] == "failed"
    assert report["checks"][0]["case_id"] == "dialogue_role_which"
    assert all(item["status"] == "passed" for item in report["checks"][1:])


def test_dkb_failure_does_not_skip_base64_boundary_checks(monkeypatch, tmp_path):
    import dkb_corpus_checks as module
    calls = []
    monkeypatch.setattr(sys, "argv", ["dkb-checks"] + [
        item for name in ("analyzer", "proxy", "block-proxy", "capture")
        for item in ("--" + name + "-url", "http://test")])
    monkeypatch.setenv("NER_TEST_REPORT_DIR", str(tmp_path))
    monkeypatch.setenv("PRESIDIO_ANALYZER_BASE64_POLICY", "logistic")
    monkeypatch.setattr(module, "check", lambda *a: (_ for _ in ()).throw(AssertionError("synthetic failure")))
    monkeypatch.setattr(module, "check_chunk_boundaries", lambda *a: calls.append("boundaries"))
    monkeypatch.setattr(module, "check_base64_policy", lambda *a: calls.append("base64"))
    with pytest.raises(RuntimeError):
        module.main()
    assert calls == ["boundaries", "base64"]
    report = json.loads((tmp_path / "dkb-logistic.json").read_text())
    assert [item["status"] for item in report["checks"]] == ["failed", "passed", "passed"]


def test_logistic_ci_step_is_not_hidden_by_a_proxy_test_failure():
    workflow = yaml.safe_load((ROOT / ".github/workflows/ner-model-integration.yml").read_text())
    steps = workflow["jobs"]["real-model-integration"]["steps"]
    assert any(step.get("id") == "proxy" for step in steps)
    logistic = next(step for step in steps if step.get("env", {}).get("PRESIDIO_ANALYZER_BASE64_POLICY") == "logistic")
    assert "!cancelled()" in logistic["if"]
    assert "steps.proxy.outcome != 'skipped'" in logistic["if"]
    assert not logistic.get("continue-on-error")


@pytest.mark.parametrize("target,suite,build", [
    ("test-hf-model", "model", True), ("test-hf-model-run", "model", False),
    ("test-ner-proxy", "proxy", False), ("test-ner-integration", "all", True),
])
def test_existing_make_commands_dispatch_without_docker(target, suite, build):
    result = subprocess.run(["make", "-n", target, "PYTHON_LOCAL=python3"], cwd=ROOT,
                            env={k: v for k, v in os.environ.items() if k not in {
                                "MAKEFLAGS", "MFLAGS", "MAKELEVEL", "MAKEOVERRIDES"}},
                            text=True, capture_output=True, check=True)
    command = next(line for line in result.stdout.splitlines() if "ner_integration.py " in line)
    assert f"ner_integration.py {suite} " in command
    assert ("--build-model" in command) == build
    assert "docker compose" not in result.stdout
