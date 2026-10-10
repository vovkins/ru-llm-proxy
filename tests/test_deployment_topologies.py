"""Deployment context, policy persistence and replica readiness contracts."""

import importlib.util
import json
import os
import stat
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
spec = importlib.util.spec_from_file_location("deployment", ROOT / "scripts/deployment.py")
deployment = importlib.util.module_from_spec(spec)
spec.loader.exec_module(deployment)


@pytest.fixture(autouse=True)
def clean_context(monkeypatch):
    for key in ["STACK", "TOPOLOGY", "TOPOLOGY_ENV", "PII_GUARDRAIL_MODE", "PII_MODE_ORIGIN", "ANALYZER_PROFILE", "ENV_FILE", "LITELLM_PORT", "STACK_START_TIMEOUT", "COMPOSE_COMPATIBILITY"]:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("ENV_FILE", "/nonexistent/issue96-test-env")


@pytest.mark.parametrize("mode", ["MASK", "mask", "BLOCK", "block"])
def test_mode_is_normalized_and_explicit_choice_persists(tmp_path, monkeypatch, mode):
    path = tmp_path / ".env"
    original = "# Keep secrets intact\nLITELLM_MASTER_KEY=sk-private-value\nOTHER='literal # secret'\n"
    path.write_text(original)
    path.chmod(0o600)
    monkeypatch.setenv("PII_GUARDRAIL_MODE", mode)
    monkeypatch.setenv("PII_MODE_ORIGIN", "command line")
    context = deployment.Context(env_file=str(path))
    context.persist_mode()
    assert path.read_text() == original + f"PII_GUARDRAIL_MODE={mode.lower()}\n"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    monkeypatch.delenv("PII_GUARDRAIL_MODE")
    assert deployment.Context(env_file=str(path)).mode == mode.lower()


def test_mode_update_preserves_existing_permissions(tmp_path, monkeypatch):
    path = tmp_path / ".env"
    path.write_text("SECRET=value\n")
    path.chmod(0o640)
    monkeypatch.setenv("PII_GUARDRAIL_MODE", "BLOCK")
    monkeypatch.setenv("PII_MODE_ORIGIN", "command line")
    deployment.Context(env_file=str(path)).persist_mode()
    assert stat.S_IMODE(path.stat().st_mode) == 0o640


def test_atomic_mode_update_preserves_crlf_and_other_bytes(tmp_path, monkeypatch):
    path = tmp_path / ".env"
    original = b"SECRET='keep # value'\r\nPII_GUARDRAIL_MODE=mask\r\nOTHER=value\r\n"
    path.write_bytes(original)
    monkeypatch.setenv("PII_GUARDRAIL_MODE", "BLOCK")
    monkeypatch.setenv("PII_MODE_ORIGIN", "command line")
    deployment.Context(env_file=str(path)).persist_mode()
    assert path.read_bytes() == original.replace(b"PII_GUARDRAIL_MODE=mask", b"PII_GUARDRAIL_MODE=block")


@pytest.mark.parametrize("value", ["0", "-1", "bad"])
def test_invalid_start_timeout_rejected_before_mutation(monkeypatch, value):
    monkeypatch.setenv("STACK_START_TIMEOUT", value)
    with pytest.raises(deployment.DeploymentError, match="STACK_START_TIMEOUT"):
        deployment.Context()


def test_environment_overrides_file_but_is_not_persisted(tmp_path, monkeypatch):
    path = tmp_path / ".env"
    path.write_text("PII_GUARDRAIL_MODE=block\n")
    monkeypatch.setenv("PII_GUARDRAIL_MODE", "MASK")
    context = deployment.Context(env_file=str(path))
    assert context.mode == "mask"
    context.persist_mode()
    assert path.read_text() == "PII_GUARDRAIL_MODE=block\n"


def test_explicit_value_can_replace_invalid_file_setting(tmp_path, monkeypatch):
    path = tmp_path / ".env"
    path.write_text("export PII_GUARDRAIL_MODE = broken\nOTHER=value\n")
    monkeypatch.setenv("PII_GUARDRAIL_MODE", "BLOCK")
    monkeypatch.setenv("PII_MODE_ORIGIN", "command line")
    deployment.Context(env_file=str(path)).persist_mode()
    assert path.read_text() == "PII_GUARDRAIL_MODE=block\nOTHER=value\n"


def test_explicit_mode_repair_collapses_duplicate_assignments(tmp_path, monkeypatch):
    path = tmp_path / ".env"
    path.write_text("PII_GUARDRAIL_MODE=mask\nOTHER=value\nPII_GUARDRAIL_MODE=broken\n")
    monkeypatch.setenv("PII_GUARDRAIL_MODE", "BLOCK")
    monkeypatch.setenv("PII_MODE_ORIGIN", "command line")
    deployment.Context(env_file=str(path)).persist_mode()
    assert path.read_text() == "PII_GUARDRAIL_MODE=block\nOTHER=value\n"


@pytest.mark.parametrize("value", ["", "OFF", "fail_open", "mas", "mask#typo"])
def test_invalid_mode_rejected_without_touching_file(tmp_path, monkeypatch, value):
    path = tmp_path / ".env"
    path.write_text("PII_GUARDRAIL_MODE=block\nSECRET=keep\n")
    before = path.read_bytes()
    monkeypatch.setenv("PII_GUARDRAIL_MODE", value)
    with pytest.raises(deployment.DeploymentError):
        deployment.Context(env_file=str(path))
    assert path.read_bytes() == before


@pytest.mark.parametrize("line", ["PII_GUARDRAIL_MODE=BLOCK", "PII_GUARDRAIL_MODE='block'", 'PII_GUARDRAIL_MODE="BLOCK" # comment', "export PII_GUARDRAIL_MODE = block # comment"])
def test_scalar_env_formats(tmp_path, line):
    path = tmp_path / ".env"
    path.write_text(line + "\n")
    assert deployment.Context(env_file=str(path)).mode == "block"


def test_functional_default_keeps_existing_compose():
    context = deployment.Context(env_file="/nonexistent/issue96-env")
    assert context.mode == "mask"
    assert context.files == ["docker-compose.yml"]
    assert context.counts["litellm"] == context.counts["presidio-analyzer"] == 1


@pytest.mark.parametrize("extended", [False, True])
def test_production_files_and_counts(monkeypatch, extended):
    monkeypatch.setenv("TOPOLOGY", "production")
    context = deployment.Context(stack="litellm-presidio-codex-lb" if extended else "litellm-presidio")
    assert context.counts["litellm"] == 2
    assert context.counts["presidio-analyzer"] == 4
    assert len(context.counts) == (7 if extended else 5)
    assert "docker-compose.production.yml" in context.files
    assert ("docker-compose.production.codex-lb.yml" in context.files) == extended
    assert context.environment["PII_GUARDRAIL_MODE"] == context.mode


@pytest.mark.parametrize("key,value", [("ANALYZER_REPLICAS", "0"), ("LITELLM_REPLICAS", "1.5"), ("DB_CPUS", "nan"), ("REDIS_CPUS", "0"), ("ANALYZER_MEMORY", "0g"), ("LITELLM_MEMORY", "-1g"), ("PII_GUARDRAIL_MODE", "mask"), ("CODEX_LB_API_KEY", "secret")])
def test_topology_file_rejects_invalid_resources_and_secret_keys(tmp_path, monkeypatch, key, value):
    path = tmp_path / "production.env"
    path.write_text(f"{key}={value}\n")
    monkeypatch.setenv("TOPOLOGY", "production")
    monkeypatch.setenv("TOPOLOGY_ENV", str(path))
    with pytest.raises(deployment.DeploymentError):
        deployment.Context()


def test_custom_file_overrides_only_given_resources(tmp_path, monkeypatch):
    path = tmp_path / "resources with spaces.env"
    path.write_text("ANALYZER_REPLICAS=2\nANALYZER_MEMORY=3g\n")
    monkeypatch.setenv("TOPOLOGY", "production")
    monkeypatch.setenv("TOPOLOGY_ENV", str(path))
    context = deployment.Context()
    assert context.counts["presidio-analyzer"] == 2
    assert context.values["ANALYZER_MEMORY"] == "3g"
    assert context.values["LITELLM_MEMORY"] == "4g"


def test_functional_gpu_is_retained_but_production_gpu_is_out_of_scope(monkeypatch):
    monkeypatch.setenv("ANALYZER_PROFILE", "gpu")
    assert "docker-compose.gpu.yml" in deployment.Context().files
    monkeypatch.setenv("TOPOLOGY", "production")
    with pytest.raises(deployment.DeploymentError, match="CPU"):
        deployment.Context()


@pytest.mark.parametrize("target", ["setup", "up", "restart", "down", "logs", "health", "metrics", "guardrails-smoke", "client-auth-smoke", "update-litellm"])
def test_make_invalid_mode_fails_before_any_mutation(tmp_path, target):
    path = tmp_path / ".env"
    path.write_text("SECRET=unchanged\nPII_GUARDRAIL_MODE=block\n")
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
    result = subprocess.run(["make", target, "STACK=litellm-presidio", f"ENV_FILE={path}", "PII_GUARDRAIL_MODE=TYPO"], cwd=ROOT, env=env, capture_output=True, text=True)
    assert result.returncode != 0
    assert "MASK или BLOCK" in result.stderr
    assert path.read_text() == "SECRET=unchanged\nPII_GUARDRAIL_MODE=block\n"


def container(service, name, mode="mask", healthy=True):
    return {"Id": name, "Image": "current-image", "Name": "/" + name, "State": {"Running": True, "Health": {"Status": "healthy" if healthy else "unhealthy"}}, "Config": {"Labels": {"com.docker.compose.service": service}, "Env": [f"PII_GUARDRAIL_MODE={mode}"]}}


def test_health_checks_each_replica_not_only_load_balancer(monkeypatch):
    monkeypatch.setenv("TOPOLOGY", "production")
    context = deployment.Context()
    items = [container(service, f"{service}-{index}") for service, count in context.counts.items() for index in range(count)]
    monkeypatch.setattr(context, "containers", lambda: items)
    monkeypatch.setattr(context, "ner_ready", lambda _: True)
    monkeypatch.setattr(context, "gateway_ready", lambda _: True)
    context.health()
    items[-1]["State"]["Health"]["Status"] = "unhealthy"
    with pytest.raises(deployment.DeploymentError, match="не готов"):
        context.health()


def test_health_rejects_broken_frontend_even_when_instances_are_ready(monkeypatch):
    context = deployment.Context()
    items = [container(service, service) for service in context.counts]
    monkeypatch.setattr(context, "containers", lambda: items)
    monkeypatch.setattr(context, "ner_ready", lambda _: True)
    monkeypatch.setattr(context, "gateway_ready", lambda _: False)
    with pytest.raises(deployment.DeploymentError, match="маршрут"):
        context.health()


@pytest.mark.parametrize("extended", [False, True])
def test_gateway_readiness_checks_real_routes(monkeypatch, extended):
    context = deployment.Context(stack="litellm-presidio-codex-lb" if extended else "litellm-presidio")
    calls = []

    def run(command, **kwargs):
        calls.append(command)
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(deployment.subprocess, "run", run)
    assert context.gateway_ready([container("nginx", "ingress")])
    assert [call[-1] for call in calls] == ["http://127.0.0.1/health/liveliness"] + (["http://127.0.0.1:2455/health/ready"] if extended else [])
    monkeypatch.setattr(deployment.subprocess, "run", lambda command, **kwargs: subprocess.CompletedProcess(command, 1))
    assert not context.gateway_ready([container("nginx", "ingress")])


def test_health_rejects_missing_replica_and_wrong_mode(monkeypatch):
    context = deployment.Context()
    items = [container(service, service) for service in context.counts]
    monkeypatch.setattr(context, "containers", lambda: items)
    monkeypatch.setattr(context, "ner_ready", lambda _: True)
    items[3]["Config"]["Env"] = ["PII_GUARDRAIL_MODE=block"]
    with pytest.raises(deployment.DeploymentError, match="работает BLOCK"):
        context.health()
    items.pop()
    with pytest.raises(deployment.DeploymentError, match="найдено 0"):
        context.health()


def test_cold_start_is_incremental_but_up_does_not_shrink_live_pool(monkeypatch):
    monkeypatch.setenv("TOPOLOGY", "production")
    context = deployment.Context()
    assert list(context.start_counts([], "presidio-analyzer")) == [1, 2, 3, 4]
    previous = [container("presidio-analyzer", str(index)) for index in range(4)]
    assert list(context.start_counts(previous, "presidio-analyzer")) == [4]


def test_production_proxy_does_not_retry_sent_model_posts():
    config = (ROOT / "nginx/conf.d/production.conf").read_text()
    model_route = config.split("listen 80;", 1)[1].split("listen 5001;", 1)[0]
    assert "non_idempotent" not in model_route
    assert "proxy_buffering off;" in model_route
    assert "server litellm:4000 resolve;" in config
    assert "server presidio-analyzer:5001 resolve;" in config


@pytest.mark.parametrize("name,routes", [("default.conf", 1), ("production.conf", 2)])
def test_nginx_waits_for_large_preprocessing_without_changing_codex_route(name, routes):
    config = (ROOT / "nginx/conf.d" / name).read_text()
    preprocessing_routes, codex_route = config.split("listen 2455;", 1)
    assert preprocessing_routes.count("proxy_read_timeout 1500s;") == routes
    assert preprocessing_routes.count("proxy_send_timeout 1500s;") == routes
    assert "proxy_read_timeout 600s;" in codex_route
    assert "proxy_send_timeout 600s;" in codex_route
    assert "proxy_buffering off;" in preprocessing_routes


def test_production_metrics_are_scraped_per_container():
    source = (ROOT / "scripts/deployment.py").read_text()
    assert '["docker", "exec", item["Id"], "python", "-c", code]' in source
    assert "context.metrics()" in source


@pytest.mark.parametrize("only_litellm", [False, True])
def test_force_restart_closes_ingress_and_starts_models_sequentially(monkeypatch, only_litellm):
    monkeypatch.setenv("TOPOLOGY", "production")
    context = deployment.Context()
    context._model = {"name": "test"}
    calls = []
    previous = [container(service, f"{service}-{i}") for service, count in context.counts.items() for i in range(count)]
    monkeypatch.setattr(context, "containers", lambda: previous)
    monkeypatch.setattr(context, "summary", lambda: None)
    monkeypatch.setattr(context, "guard", lambda operation: calls.append(("guard", operation)))
    monkeypatch.setattr(context, "persist_mode", lambda: calls.append(("persist",)))
    monkeypatch.setattr(context, "compose", lambda *args: calls.append(args))
    monkeypatch.setattr(context, "health", lambda *args, **kwargs: None)
    context.start(force=True, only_litellm=only_litellm)
    assert calls.index(("stop", "nginx")) < calls.index(("persist",))
    assert calls.index(("stop", "litellm")) < calls.index(("persist",))
    up = [args for args in calls if args[0] == "up"]
    analyzer = [args for args in up if args[-1] == "presidio-analyzer"]
    assert len(analyzer) == (0 if only_litellm else 4)
    if analyzer:
        assert [args[args.index("--scale") + 1] for args in analyzer] == [f"presidio-analyzer={i}" for i in range(1, 5)]
    lite = [args for args in up if args[-1] == "litellm"]
    assert [args[args.index("--scale") + 1] for args in lite] == ["litellm=1", "litellm=2"]
    assert "--force-recreate" in lite[0] and "--force-recreate" not in lite[1]
    assert up[-1][-1] == "nginx"
    assert "--force-recreate" in up[-1]
    if only_litellm:
        assert all(args[-1] in {"nginx", "litellm"} for args in up)


@pytest.mark.parametrize("topology,mode,stop_lite", [("functional", "mask", False), ("functional", "block", True), ("production", "mask", True)])
def test_up_closes_direct_functional_ingress_before_policy_or_topology_switch(monkeypatch, topology, mode, stop_lite):
    monkeypatch.setenv("TOPOLOGY", topology)
    monkeypatch.setenv("PII_GUARDRAIL_MODE", mode)
    context = deployment.Context()
    context._model = {"name": "test", "services": {service: {"image": service} for service in context.counts}}
    items = [container(service, service) for service in context.counts]
    for item in items:
        item["Config"]["Labels"]["com.docker.compose.config-hash"] = "unchanged"
    calls = []

    def compose(*args, **kwargs):
        calls.append(args)
        return "service unchanged" if args[0] == "config" else None

    monkeypatch.setattr(context, "containers", lambda: items)
    monkeypatch.setattr(context, "summary", lambda: None)
    monkeypatch.setattr(context, "guard", lambda _: None)
    monkeypatch.setattr(context, "compose", compose)
    monkeypatch.setattr(deployment.subprocess, "check_output", lambda *args, **kwargs: "current-image")
    monkeypatch.setattr(context, "health", lambda *args, **kwargs: None)
    monkeypatch.setattr(context, "persist_mode", lambda: calls.append(("persist",)))
    context.start()
    assert calls.index(("stop", "nginx")) < calls.index(("persist",))
    assert (("stop", "litellm") in calls) == stop_lite
    if stop_lite:
        assert calls.index(("stop", "litellm")) < calls.index(("persist",))


@pytest.mark.parametrize("updated_service", ["presidio-analyzer", "litellm"])
@pytest.mark.parametrize("explicit_image", [False, True])
@pytest.mark.parametrize("compatibility", ["off", "environment", "file"])
def test_same_tag_image_update_starts_changed_pool_sequentially(tmp_path, monkeypatch, updated_service, explicit_image, compatibility):
    monkeypatch.setenv("TOPOLOGY", "production")
    if compatibility == "environment":
        monkeypatch.setenv("COMPOSE_COMPATIBILITY", "true")
    elif compatibility == "file":
        path = tmp_path / ".env"
        path.write_text("COMPOSE_COMPATIBILITY=true\n")
        monkeypatch.setenv("ENV_FILE", str(path))
    context = deployment.Context()
    context._model = {"name": "test", "services": {service: {"image": service} if explicit_image else {} for service in context.counts}}
    items = [container(service, f"{service}-{i}") for service, count in context.counts.items() for i in range(count)]
    for item in items:
        item["Config"]["Labels"]["com.docker.compose.config-hash"] = "unchanged"
        item["Config"]["Labels"]["ru-llm-proxy.topology"] = "production"
    calls = []

    def compose(*args, **kwargs):
        calls.append(args)
        if args[:2] == ("config", "--hash"):
            return "service unchanged"
        if args[:2] == ("config", "--images"):
            return "dependency-image\nrequested-image"

    monkeypatch.setattr(context, "containers", lambda: items)
    monkeypatch.setattr(context, "summary", lambda: None)
    monkeypatch.setattr(context, "guard", lambda _: None)
    monkeypatch.setattr(context, "compose", compose)
    monkeypatch.setattr(context, "health", lambda *args, **kwargs: None)
    images = []
    separator = "_" if compatibility != "off" else "-"

    def inspect_image(command, **kwargs):
        images.append(command[-1])
        return "new-image" if command[-1] == (updated_service if explicit_image else f"test{separator}{updated_service}") else "current-image"

    monkeypatch.setattr(deployment.subprocess, "check_output", inspect_image)
    context.start()
    up = [args for args in calls if args[0] == "up"]
    analyzer = [args for args in up if args[-1] == "presidio-analyzer"]
    assert [args[args.index("--scale") + 1] for args in analyzer] == ([f"presidio-analyzer={i}" for i in range(1, 5)] if updated_service == "presidio-analyzer" else ["presidio-analyzer=4"])
    lite = [args for args in up if args[-1] == "litellm"]
    assert [args[args.index("--scale") + 1] for args in lite] == (["litellm=1", "litellm=2"] if updated_service == "litellm" else ["litellm=2"])
    assert images == (["litellm", "presidio-analyzer"] if explicit_image else [f"test{separator}litellm", f"test{separator}presidio-analyzer"])


def test_failed_model_readiness_leaves_ingress_closed(monkeypatch):
    context = deployment.Context()
    context._model = {"name": "test"}
    calls = []
    monkeypatch.setattr(context, "containers", lambda: [container("nginx", "nginx")])
    monkeypatch.setattr(context, "summary", lambda: None)
    monkeypatch.setattr(context, "guard", lambda _: None)
    monkeypatch.setattr(context, "compose", lambda *args: calls.append(args))

    def readiness(wait=0, services=None):
        if "presidio-analyzer" in services:
            raise deployment.DeploymentError("NER не готова")

    monkeypatch.setattr(context, "health", readiness)
    with pytest.raises(deployment.DeploymentError, match="NER"):
        context.start()
    assert ("stop", "nginx") in calls
    assert not any(args[0] == "up" and args[-1] == "nginx" for args in calls)


def test_setup_rejects_live_policy_change_before_writing_env(tmp_path, monkeypatch):
    path = tmp_path / ".env"
    path.write_text("PII_GUARDRAIL_MODE=mask\nSECRET=keep\n")
    monkeypatch.setenv("PII_GUARDRAIL_MODE", "BLOCK")
    monkeypatch.setenv("PII_MODE_ORIGIN", "command line")
    context = deployment.Context(env_file=str(path))
    monkeypatch.setattr(context, "containers", lambda: [container("litellm", "proxy")])
    monkeypatch.setattr(context, "guard", lambda _: None)
    with pytest.raises(deployment.DeploymentError, match="make down"):
        context.setup()
    assert path.read_text() == "PII_GUARDRAIL_MODE=mask\nSECRET=keep\n"


def test_mutation_requires_actual_topology_and_complete_stack(monkeypatch):
    context = deployment.Context()
    item = container("litellm", "proxy")
    item["Config"]["Labels"]["ru-llm-proxy.topology"] = "production"
    monkeypatch.setattr(context, "containers", lambda: [item])
    with pytest.raises(deployment.DeploymentError, match="TOPOLOGY=production"):
        context.guard("mutation")
    context.guard("switch")
    monkeypatch.setattr(context, "containers", lambda: [container("codex-lb", "codex")])
    with pytest.raises(deployment.DeploymentError, match="расширенный состав"):
        context.guard("switch")


def test_duplicate_mode_and_resource_settings_are_rejected(tmp_path, monkeypatch):
    path = tmp_path / "settings.env"
    path.write_text("PII_GUARDRAIL_MODE=mask\nPII_GUARDRAIL_MODE=block\n")
    with pytest.raises(deployment.DeploymentError, match="Повторная"):
        deployment.Context(env_file=str(path))
    path.write_text("ANALYZER_REPLICAS=2\nANALYZER_REPLICAS=4\n")
    with pytest.raises(deployment.DeploymentError, match="Повторная"):
        deployment.resources(path)


def test_documentation_and_mock_gate_cover_context_contract():
    import yaml

    doc = (ROOT / "docs/deployment.md").read_text()
    for name in ["STACK", "TOPOLOGY", "PII_GUARDRAIL_MODE", "TOPOLOGY_ENV", "ENV_FILE"]:
        assert name in doc
    config = yaml.safe_load((ROOT / "tests/e2e/litellm-config.topologies.yaml").read_text())
    assert all(item["litellm_params"]["api_base"] == "http://mock-upstream:8080/v1" for item in config["model_list"])
    assert config["general_settings"]["database_url"] == "os.environ/DATABASE_URL"
    assert "deployment_affinity" in config["router_settings"]["optional_pre_call_checks"]
    makefile = (ROOT / "Makefile").read_text()
    assert "tests/e2e/deployment_topology_gate.py" in makefile.split("test-ner-proxy:", 1)[1].split("test-ner-integration:", 1)[0]


@pytest.fixture
def topology_gate():
    module_spec = importlib.util.spec_from_file_location(
        "deployment_topology_gate", ROOT / "tests/e2e/deployment_topology_gate.py",
    )
    module = importlib.util.module_from_spec(module_spec)
    module_spec.loader.exec_module(module)
    return module.Gate


def gate_container(host="127.0.0.1", published="54321", *, running=True, service="nginx"):
    return {"State": {"Running": running},
            "Config": {"Labels": {"com.docker.compose.service": service}},
            "NetworkSettings": {"Ports": {"80/tcp": [{"HostIp": host, "HostPort": published}]}}}


@pytest.mark.parametrize("host,expected", [
    ("127.0.0.1", "127.0.0.1"), ("0.0.0.0", "127.0.0.1"),
    ("", "127.0.0.1"), (None, "127.0.0.1"), ("::", "[::1]"), ("::1", "[::1]"),
])
def test_local_gate_url_uses_inspected_binding_without_compose_port(topology_gate, host, expected):
    context = MagicMock()
    context.containers.return_value = [gate_container(host)]
    assert topology_gate.url(None, context, "nginx", 80) == f"http://{expected}:54321"
    context.compose.assert_not_called()


def test_local_gate_url_skips_stopped_or_different_services(topology_gate):
    context = MagicMock()
    context.containers.return_value = [gate_container(published="1", running=False),
                                       gate_container(published="2", service="litellm"), gate_container()]
    assert topology_gate.url(None, context, "nginx", 80) == "http://127.0.0.1:54321"


@pytest.mark.parametrize("binding", [gate_container("invalid IP"), gate_container(published="0"),
                                      gate_container(published="65536"), gate_container(published="not-a-port")])
def test_invalid_local_gate_binding_fails_explicitly(topology_gate, binding):
    context = MagicMock()
    context.containers.return_value = [binding]
    with pytest.raises(RuntimeError, match="No local published TCP port"):
        topology_gate.url(None, context, "nginx", 80)
