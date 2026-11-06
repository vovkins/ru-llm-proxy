"""Immutable node configuration, fail-closed publication and network contracts."""

import importlib.util
import json
from pathlib import Path
import shutil
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import node_bundle as bundle
from node_config import NodeConfiguration


@pytest.fixture
def root(tmp_path, monkeypatch):
    for key in ("STACK", "NODE_ROLE", "NODE_CONFIG", "TOPOLOGY", "TOPOLOGY_ENV", "ENV_FILE",
                "PII_GUARDRAIL_MODE", "PII_MODE_ORIGIN", "COMPOSE_PROJECT_NAME"):
        monkeypatch.delenv(key, raising=False)
    shutil.copytree(ROOT / "nginx", tmp_path / "nginx")
    monkeypatch.setattr(bundle, "validate_nginx", lambda *_: None)
    return tmp_path


def config(address="192.0.2.20", source="192.0.2.10/32"):
    return NodeConfiguration((source,), f"http://{address}:5001")


@pytest.mark.parametrize("role", ["primary", "compute"])
def test_generations_are_reproducible_and_mount_the_whole_readonly_directory(root, role):
    first = bundle.prepare(root, "node-test", role, config(), "existing-image")
    second = bundle.prepare(root, "node-test", role, config(), "existing-image")
    assert first == second == bundle.load(root, "node-test", role)
    spec = json.loads(first.overlay.read_text())["services"]["nginx"]
    assert spec["volumes"][1] == {"type": "bind", "source": str(first.directory),
        "target": "/etc/nginx/node-config", "read_only": True}
    if role == "compute":
        assert spec["ports"] == [{"target": 5001, "published": "5001", "host_ip": "192.0.2.20", "protocol": "tcp"}]
        assert "sysctls" not in spec
    else:
        assert "ports" not in spec
        assert spec["sysctls"] == {"net.ipv4.tcp_keepalive_time": "10", "net.ipv4.tcp_keepalive_intvl": "5",
                                   "net.ipv4.tcp_keepalive_probes": "3"}


@pytest.mark.parametrize("role", ["primary", "compute"])
def test_address_update_selects_a_new_immutable_mount_without_overwriting_the_running_one(root, role):
    old = bundle.prepare(root, "node-test", role, config(), "image")
    content = (old.directory / "default.conf").read_bytes()
    new = bundle.prepare(root, "node-test", role, config("192.0.2.21", "192.0.2.11/32"), "image")
    assert new.digest != old.digest
    assert (old.directory / "default.conf").read_bytes() == content
    assert json.loads(new.overlay.read_text())["services"]["nginx"]["labels"] != json.loads(old.overlay.read_text())["services"]["nginx"]["labels"]
    assert bundle.load(root, "node-test", role) == new


def test_failed_validation_preserves_current_pointer_and_all_old_bytes(root, monkeypatch):
    old = bundle.prepare(root, "node-test", "primary", config(), "image")
    parent = old.directory.parent
    pointer = (parent / "current.json").read_bytes()
    files = {p.name: p.read_bytes() for p in old.directory.iterdir()}
    def fail(*_):
        raise bundle.BundleError("synthetic syntax failure")
    monkeypatch.setattr(bundle, "validate_nginx", fail)
    with pytest.raises(bundle.BundleError):
        bundle.prepare(root, "node-test", "primary", config("192.0.2.21"), "image")
    assert (parent / "current.json").read_bytes() == pointer
    assert {p.name: p.read_bytes() for p in old.directory.iterdir()} == files
    assert sorted(p.name for p in parent.iterdir()) == sorted([old.digest, "current.json"])


@pytest.mark.parametrize("address", ["ner-b.internal", "[2001:db8::20]", "127.0.0.1", "0.0.0.0", "224.0.0.1", "255.255.255.255"])
def test_runtime_rejects_non_fixed_unicast_ipv4_before_creating_files(root, address):
    with pytest.raises(bundle.BundleError, match="IPv4"):
        bundle.prepare(root, "node-test", "compute", config(address), "image")
    assert not (root / "deploy/generated").exists()


@pytest.mark.parametrize("filename", ["default.conf", "metadata.json", "compose.json"])
def test_corrupt_generation_is_rejected_without_echoing_contents(root, filename):
    prepared = bundle.prepare(root, "node-test", "primary", config(), "image")
    (prepared.directory / filename).write_text("private-sentinel")
    with pytest.raises(bundle.BundleError) as error:
        bundle.load(root, "node-test", "primary")
    assert "private-sentinel" not in str(error.value)


@pytest.mark.parametrize("project", ["../private-sentinel", "", "/tmp/project", "Uppercase", "a/b"])
def test_project_cannot_escape_the_generated_namespace(root, project):
    with pytest.raises(bundle.BundleError):
        bundle.prepare(root, project, "primary", config(), "image")
    assert not (root / "deploy").exists()


def test_pointer_cannot_escape_the_generation_directory(root):
    prepared = bundle.prepare(root, "node-test", "primary", config(), "image")
    (prepared.directory.parent / "current.json").write_text('{"digest":"../../private-sentinel"}')
    with pytest.raises(bundle.BundleError):
        bundle.load(root, "node-test", "primary")


def test_retry_ownership_is_central_and_does_not_include_semantic_503(root):
    primary = bundle.render(root, "primary", config()).decode()
    central = primary.split("    listen 5001;", 1)[1].split("    listen 127.0.0.1:5002;", 1)[0]
    analyze = central.split("location = /api/v1/analyze", 1)[1]
    assert "proxy_next_upstream error timeout http_502 non_idempotent;" in analyze
    assert "proxy_next_upstream_tries 2;" in analyze
    for status in ("http_503", "http_429", "http_504"):
        assert status not in primary
    assert "proxy_socket_keepalive on;" in central
    assert "proxy_request_buffering on;" in central
    assert "proxy_set_header Connection close;" in central
    nodes = primary.split("upstream node_analyzers", 1)[1].split("\n}", 1)[0]
    assert nodes.count("    server ") == 2
    assert "keepalive " not in nodes
    for text in (primary.split("listen 127.0.0.1:5002;", 1)[1].split("listen 2455;", 1)[0],
                 bundle.render(root, "compute", config()).decode()):
        assert "proxy_next_upstream off;" in text
        assert "proxy_next_upstream_tries" not in text
    assert "proxy_next_upstream off;" in (root / "nginx/conf.d/triton.conf").read_text()


def test_compute_acl_uses_only_canonical_sources_and_loopback(root):
    text = bundle.render(root, "compute", config()).decode()
    assert "allow 127.0.0.1;" in text and "allow 192.0.2.10/32;" in text and "deny all;" in text
    assert "set_real_ip_from" not in text and "real_ip_header" not in text
    assert "/metrics" not in text and "listen 8000" not in text and "2455" not in text


def test_context_can_load_installed_bundle_without_original_node_config(root, monkeypatch):
    spec = importlib.util.spec_from_file_location("bundle_deployment", ROOT / "scripts/deployment.py")
    deployment = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(deployment)
    monkeypatch.setattr(deployment, "ROOT", root)
    shutil.copytree(ROOT / "deploy/topologies", root / "deploy/topologies")
    for key in ("PII_GUARDRAIL_MODE", "NODE_CONFIG", "TOPOLOGY_ENV"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("NODE_ROLE", "compute")
    monkeypatch.setenv("TOPOLOGY", "production")
    monkeypatch.setenv("ENV_FILE", str(root / ".env"))
    installed = bundle.prepare(root, "node-test", "compute", config(), "image")
    context = deployment.Context()
    context._model = {"name": "node-test"}
    context.load_runtime()
    assert context.command[-2:] == ["-f", str(installed.overlay)]


def context_at(root, monkeypatch, role="compute"):
    spec = importlib.util.spec_from_file_location("bundle_deployment_cases", ROOT / "scripts/deployment.py")
    deployment = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(deployment)
    monkeypatch.setattr(deployment, "ROOT", root)
    if not (root / "deploy/topologies").exists():
        shutil.copytree(ROOT / "deploy/topologies", root / "deploy/topologies")
    for key in ("PII_GUARDRAIL_MODE", "TOPOLOGY_ENV"):
        monkeypatch.delenv(key, raising=False)
    node = root / "nodes.json"
    node.write_text(json.dumps({"primary": {"source_cidrs": list(config().source_cidrs)},
                                "compute": {"analyzer_url": config().analyzer_url}}))
    monkeypatch.setenv("NODE_CONFIG", str(node))
    monkeypatch.setenv("NODE_ROLE", role)
    monkeypatch.setenv("TOPOLOGY", "production")
    monkeypatch.setenv("ENV_FILE", str(root / ".env"))
    context = deployment.Context()
    model = {"name": "node-test", "services": {name: {"image": "image"} for name in context.counts}}
    monkeypatch.setattr(context, "compose", lambda *args, **kwargs: json.dumps(model))
    monkeypatch.setattr(context, "containers", lambda: [])
    return deployment, context, node


def test_current_config_cannot_silently_inspect_an_old_installed_generation(root, monkeypatch):
    deployment, context, node = context_at(root, monkeypatch)
    installed = bundle.prepare(root, "node-test", "compute", config("192.0.2.21"), "image")
    with pytest.raises(deployment.DeploymentError, match="изменён"):
        context.load_runtime(validate_current=True)
    assert bundle.load(root, "node-test", "compute") == installed
    assert node.exists()


def test_prepare_context_checks_role_and_syntax_before_switching_generation(root, monkeypatch):
    deployment, context, _ = context_at(root, monkeypatch)
    installed = bundle.prepare(root, "node-test", "compute", config("192.0.2.21"), "image")
    monkeypatch.setattr(context, "guard_role", lambda *_: (_ for _ in ()).throw(deployment.DeploymentError("wrong role")))
    with pytest.raises(deployment.DeploymentError, match="wrong role"):
        context.prepare_runtime()
    assert bundle.load(root, "node-test", "compute") == installed
    monkeypatch.setattr(context, "guard_role", lambda *_: None)
    context.prepare_runtime()
    assert context._bundle.digest != installed.digest
    assert context.command[-2:] == ["-f", str(context._bundle.overlay)]


@pytest.mark.parametrize("role", ["primary", "compute"])
def test_build_does_not_load_node_file_or_create_a_bundle(root, monkeypatch, role):
    _, context, node = context_at(root, monkeypatch, role)
    node.unlink()
    monkeypatch.setattr(context, "guard_role", lambda *_: None)
    context.build()
    assert not (root / "deploy/generated").exists()


def test_bundle_gate_is_in_existing_static_suite_and_ner_workflow():
    assert "tests/test_node_bundle.py" in (ROOT / "Makefile").read_text()
    workflow = (ROOT / ".github/workflows/ner-model-integration.yml").read_text()
    assert workflow.count('"scripts/node_bundle.py"') == 2


def test_short_read_timeout_changes_only_the_temporary_central_post(root, monkeypatch):
    monkeypatch.syspath_prepend(str(ROOT / "tests/e2e"))
    from node_routing_checks import short_read_timeout_copy
    production = ROOT / "nginx/templates/node-primary.conf.template"
    original = production.read_bytes()
    primary = bundle.render(root, "primary", config())
    compute = bundle.render(root, "compute", config())
    normal = short_read_timeout_copy(root)
    assert bundle.render(root, "primary", config()) == primary.replace(
        b"proxy_read_timeout 1500s;\n        proxy_send_timeout 1500s;\n        proxy_next_upstream error timeout http_502 non_idempotent;",
        b"proxy_read_timeout 2s;\n        proxy_send_timeout 1500s;\n        proxy_next_upstream error timeout http_502 non_idempotent;",
    )
    assert bundle.render(root, "compute", config()) == compute
    assert production.read_bytes() == original
    with pytest.raises(AssertionError):
        short_read_timeout_copy(root)
    (root / "nginx/templates/node-primary.conf.template").write_bytes(normal)
    assert bundle.render(root, "primary", config()) == primary
