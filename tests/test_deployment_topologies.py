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
    for key in ["STACK", "TOPOLOGY", "TOPOLOGY_ENV", "PII_GUARDRAIL_MODE", "PII_MODE_ORIGIN", "ENV_FILE", "LITELLM_PORT", "STACK_START_TIMEOUT", "COMPOSE_COMPATIBILITY", *deployment.REMOVED_SETTINGS, *deployment.RESOURCE_KEYS]:
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
    assert len(context.counts) == (8 if extended else 6)
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


def test_triton_is_part_of_the_functional_stack():
    context = deployment.Context()
    assert context.files == ["docker-compose.yml"]
    assert context.counts["ner-triton"] == 1
    assert context.counts["presidio-analyzer"] == 1


def test_production_uses_one_resource_file_and_can_run_two_triton_replicas(tmp_path, monkeypatch):
    monkeypatch.setenv("TOPOLOGY", "production")
    context = deployment.Context()
    assert context.files == [
        "docker-compose.yml",
        "docker-compose.production.yml",
    ]
    assert context.counts["presidio-analyzer"] == 4
    assert context.counts["ner-triton"] == 2
    assert context.values["TRITON_CPUS"] == "2.0"
    assert context.values["TRITON_MODEL_INSTANCES"] == "2"
    path = tmp_path / "override.env"
    path.write_text("ANALYZER_REPLICAS=3\nTRITON_MODEL_INSTANCES=6\nTRITON_REPLICAS=2\n")
    monkeypatch.setenv("TOPOLOGY_ENV", str(path))
    context = deployment.Context()
    assert context.counts["presidio-analyzer"] == 3
    assert context.counts["ner-triton"] == 2
    assert context.values["TRITON_MODEL_INSTANCES"] == "6"


@pytest.mark.parametrize("extended,cpu,gib", [(False, 20, 48.25), (True, 23, 54.25)])
def test_standard_production_resources_leave_server_headroom(monkeypatch, extended, cpu, gib):
    from decimal import Decimal

    monkeypatch.setenv("TOPOLOGY", "production")
    context = deployment.Context(stack="litellm-presidio-codex-lb" if extended else "litellm-presidio")
    assert context.values["ANALYZER_CPUS"] == "2.0"
    assert context.values["ANALYZER_MEMORY"] == "4g"
    assert context.values["REDIS_MEMORY"] == "16g"
    names = {"presidio-analyzer": "ANALYZER", "ner-triton": "TRITON"}
    total_cpu = Decimal(0)
    total_memory = 0
    for service, count in context.counts.items():
        prefix = names.get(service, service.upper().replace("-", "_"))
        total_cpu += Decimal(context.values[prefix + "_CPUS"]) * count
        total_memory += deployment.memory_bytes(context.values[prefix + "_MEMORY"]) * count
    assert total_cpu == cpu
    assert total_memory == int(gib * 1024**3)
    assert total_cpu < 32
    assert total_memory < 64_000_000_000


def test_matching_triton_settings_do_not_require_a_new_override_file(monkeypatch):
    monkeypatch.setenv("TRITON_REPLICAS", "1")
    monkeypatch.setenv("TRITON_MODEL_INSTANCES", "2")
    assert deployment.Context().counts["ner-triton"] == 1
    monkeypatch.setenv("TOPOLOGY", "production")
    monkeypatch.setenv("TRITON_REPLICAS", "2")
    assert deployment.Context().counts["ner-triton"] == 2


@pytest.mark.parametrize("value", ["0", "2.5", "x", "33"])
def test_triton_instances_must_be_a_positive_count(tmp_path, monkeypatch, value):
    path = tmp_path / "production.env"
    path.write_text(f"TRITON_MODEL_INSTANCES={value}\n")
    monkeypatch.setenv("TOPOLOGY", "production")
    monkeypatch.setenv("TOPOLOGY_ENV", str(path))
    with pytest.raises(deployment.DeploymentError):
        deployment.Context()


def test_unified_compose_carries_only_operational_ner_settings():
    import yaml

    triton = yaml.safe_load((ROOT / "docker-compose.yml").read_text())
    analyzer = dict(item.split("=", 1) for item in triton["services"]["presidio-analyzer"]["environment"])
    assert not deployment.REMOVED_SETTINGS.intersection(analyzer)
    assert analyzer["PRESIDIO_ANALYZER_WORKERS"] == "${PRESIDIO_ANALYZER_WORKERS:-1}"
    assert triton["services"]["ner-triton"]["environment"]["TRITON_MODEL_INSTANCES"] == "2"
    assert analyzer["PRESIDIO_ANALYZER_CONCURRENCY_LIMIT"] == "${PRESIDIO_ANALYZER_CONCURRENCY_LIMIT:-2}"
    assert analyzer["PRESIDIO_ANALYZER_QUEUE_LIMIT"] == "${PRESIDIO_ANALYZER_QUEUE_LIMIT:-32}"
    assert analyzer["PRESIDIO_ANALYZER_QUEUE_TIMEOUT_SECONDS"] == "${PRESIDIO_ANALYZER_QUEUE_TIMEOUT_SECONDS:-30}"
    for service in ("litellm", "presidio-analyzer", "ner-triton", "redis", "db", "nginx"):
        assert "mem_limit" not in triton["services"][service]
        assert "cpus" not in triton["services"][service]
    assert "ports" not in triton["services"]["ner-triton"]
    class ComposeLoader(yaml.SafeLoader):
        pass
    ComposeLoader.add_constructor("!reset", lambda loader, node: None)
    production = yaml.load((ROOT / "docker-compose.production.yml").read_text(), Loader=ComposeLoader)["services"]["ner-triton"]
    assert production["cpus"] == "${TRITON_CPUS:-2.0}"
    assert production["mem_limit"] == "${TRITON_MEMORY:-2g}"
    assert production["labels"]["ru-llm-proxy.topology"] == "production"


@pytest.mark.parametrize("target", ["setup", "up", "restart", "down", "logs", "health", "metrics", "guardrails-smoke", "client-auth-smoke", "update-litellm"])
def test_make_invalid_mode_fails_before_any_mutation(tmp_path, target):
    path = tmp_path / ".env"
    path.write_text("SECRET=unchanged\nPII_GUARDRAIL_MODE=block\n")
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
    result = subprocess.run(["make", target, "STACK=litellm-presidio", f"ENV_FILE={path}", "PII_GUARDRAIL_MODE=TYPO"], cwd=ROOT, env=env, capture_output=True, text=True)
    assert result.returncode != 0
    assert "MASK или BLOCK" in result.stderr
    assert path.read_text() == "SECRET=unchanged\nPII_GUARDRAIL_MODE=block\n"


def test_common_build_validation_does_not_require_stack_or_call_docker(tmp_path):
    environment = dict(os.environ)
    for key in ("STACK", "MAKEFLAGS", "MFLAGS", "MAKEOVERRIDES"):
        environment.pop(key, None)
    result = subprocess.run(["make", "require-deployment", f"ENV_FILE={tmp_path / '.env'}",
                             f"PYTHON_LOCAL={sys.executable}"], cwd=ROOT, env=environment,
                            capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def container(service, name, mode="mask", healthy=True):
    return {"Id": name, "Image": "current-image", "Name": "/" + name, "State": {"Running": True, "Health": {"Status": "healthy" if healthy else "unhealthy"}}, "Config": {"Labels": {"com.docker.compose.service": service}, "Env": [f"PII_GUARDRAIL_MODE={mode}"]}}


def test_health_checks_each_replica_not_only_load_balancer(monkeypatch):
    monkeypatch.setenv("TOPOLOGY", "production")
    context = deployment.Context()
    items = [container(service, f"{service}-{index}") for service, count in context.counts.items() for index in range(count)]
    monkeypatch.setattr(context, "containers", lambda: items)
    monkeypatch.setattr(context, "ner_ready", lambda _: True)
    monkeypatch.setattr(context, "triton_ready", lambda _: True)
    monkeypatch.setattr(context, "triton_route_ready", lambda _: True)
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
    monkeypatch.setattr(context, "triton_ready", lambda _: True)
    monkeypatch.setattr(context, "triton_route_ready", lambda _: True)
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
    monkeypatch.setattr(context, "triton_ready", lambda _: True)
    monkeypatch.setattr(context, "triton_route_ready", lambda _: True)
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
    assert '["docker", "exec", item["Id"], "python3", "-c", code]' in source
    assert "context.metrics()" in source


@pytest.mark.parametrize("only_litellm", [False, True])
def test_force_restart_starts_full_nginx_then_models_sequentially(monkeypatch, only_litellm):
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
    assert ("stop", "nginx") not in calls
    assert calls.index(("stop", "litellm")) < calls.index(("persist",))
    up = [args for args in calls if args[0] == "up"]
    analyzer = [args for args in up if args[-1] == "presidio-analyzer"]
    assert len(analyzer) == (0 if only_litellm else 4)
    if analyzer:
        assert [args[args.index("--scale") + 1] for args in analyzer] == [f"presidio-analyzer={i}" for i in range(1, 5)]
    lite = [args for args in up if args[-1] == "litellm"]
    assert [args[args.index("--scale") + 1] for args in lite] == ["litellm=1", "litellm=2"]
    assert "--force-recreate" in lite[0] and "--force-recreate" not in lite[1]
    assert up[0][-1] == "nginx"
    assert "--force-recreate" in up[0]
    assert ("exec", "-T", "nginx", "nginx", "-t") in calls
    triton = [args for args in up if args[-1] == "ner-triton"]
    assert len(triton) == (0 if only_litellm else 2)
    if triton:
        assert [args[args.index("--scale") + 1] for args in triton] == ["ner-triton=1", "ner-triton=2"]
        assert "--force-recreate" in triton[0] and "--force-recreate" not in triton[1]
    if triton:
        assert up.index(triton[0]) < up.index(analyzer[0]) < up.index(lite[0])
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
    assert ("stop", "nginx") not in calls
    assert (("stop", "litellm") in calls) == stop_lite
    if stop_lite:
        assert calls.index(("stop", "litellm")) < calls.index(("persist",))


@pytest.mark.parametrize("updated_service", ["presidio-analyzer", "litellm", "ner-triton"])
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
    assert images == (["litellm", "presidio-analyzer", "ner-triton"] if explicit_image else [f"test{separator}{s}" for s in ("litellm", "presidio-analyzer", "ner-triton")])


def test_failed_model_readiness_does_not_start_litellm_but_keeps_full_nginx(monkeypatch):
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
    assert ("stop", "nginx") not in calls
    assert any(args[0] == "up" and args[-1] == "nginx" for args in calls)
    assert not any(args[0] == "up" and args[-1] == "litellm" for args in calls)


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


@pytest.mark.parametrize("key", sorted(deployment.REMOVED_SETTINGS))
@pytest.mark.parametrize("source", ["environment", "file"])
def test_removed_settings_fail_before_docker_or_file_mutation(tmp_path, monkeypatch, key, source):
    path = tmp_path / ".env"
    path.write_text(f"{key}=synthetic-private-value\n" if source == "file" else "SECRET=preserved\n")
    before = path.read_bytes()
    if source == "environment":
        monkeypatch.setenv(key, "synthetic-private-value")
    monkeypatch.setattr(deployment.subprocess, "run", lambda *_a, **_k: pytest.fail("Docker was called"))
    with pytest.raises(deployment.DeploymentError, match=key) as error:
        deployment.Context(env_file=str(path))
    assert "synthetic-private-value" not in str(error.value)
    assert path.read_bytes() == before


@pytest.mark.parametrize("value", ["0", "3", "1.5", "-1", "bad"])
def test_only_one_or_two_triton_containers_are_supported(tmp_path, monkeypatch, value):
    path = tmp_path / "resources.env"
    path.write_text("TRITON_REPLICAS=" + value + "\n")
    monkeypatch.setenv("TOPOLOGY", "production")
    monkeypatch.setenv("TOPOLOGY_ENV", str(path))
    with pytest.raises(deployment.DeploymentError, match="TRITON_REPLICAS"):
        deployment.Context()


@pytest.mark.parametrize("key,value", [
    ("TRITON_REPLICAS", "1"), ("TRITON_MODEL_INSTANCES", "1"),
    ("TRITON_CPUS", "4"), ("TRITON_MEMORY", "4g"),
])
def test_triton_resource_conflicts_are_not_silently_overwritten(monkeypatch, key, value):
    monkeypatch.setenv("TOPOLOGY", "production")
    monkeypatch.setenv(key, value)
    with pytest.raises(deployment.DeploymentError, match="противоречит"):
        deployment.Context()


@pytest.mark.parametrize("key,value", [
    ("PRESIDIO_ANALYZER_WORKERS", "0"), ("PRESIDIO_ANALYZER_CONCURRENCY_LIMIT", "-1"),
    ("PRESIDIO_ANALYZER_QUEUE_LIMIT", "-1"), ("PRESIDIO_ANALYZER_QUEUE_TIMEOUT_SECONDS", "nan"),
    ("PRESIDIO_ANALYZER_TRITON_TIMEOUT_SECONDS", "0"),
    ("PRESIDIO_ANALYZER_NER_BATCH_SIZE", "33"),
])
def test_invalid_operational_ner_settings_fail_early(monkeypatch, key, value):
    monkeypatch.setenv(key, value)
    with pytest.raises(deployment.DeploymentError, match=key):
        deployment.Context()


def test_each_triton_must_match_independent_target_image_metadata(monkeypatch):
    context = deployment.Context()
    expected = {"model_id": "fixed/model", "revision": "fixed-revision", "onnx_sha256": "a" * 64}
    context._model = {"services": {"ner-triton": {"image": "trusted-target-image"}}}
    commands = []
    def image(command, **kwargs):
        commands.append(command)
        return json.dumps(expected)
    monkeypatch.setattr(deployment.subprocess, "check_output", image)
    responses = iter([expected, {**expected, "onnx_sha256": "b" * 64}])
    monkeypatch.setattr(deployment.subprocess, "run", lambda command, **kw:
                        subprocess.CompletedProcess(command, 0, json.dumps(next(responses))))
    assert context.triton_ready(container("ner-triton", "first"))
    assert not context.triton_ready(container("ner-triton", "second"))
    assert len(commands) == 1
    assert commands[0][1:6] == ["run", "--rm", "--network", "none", "--entrypoint"]
    assert "trusted-target-image" in commands[0]


def test_ready_replicas_do_not_hide_an_unready_internal_nginx_route(monkeypatch):
    context = deployment.Context()
    items = [container(service, service) for service in context.counts]
    monkeypatch.setattr(context, "containers", lambda: items)
    monkeypatch.setattr(context, "ner_ready", lambda _: True)
    monkeypatch.setattr(context, "triton_ready", lambda _: True)
    monkeypatch.setattr(context, "gateway_ready", lambda _: True)
    monkeypatch.setattr(context, "triton_route_ready", lambda _: False)
    with pytest.raises(deployment.DeploymentError, match="внутренний маршрут"):
        context.health(services={"ner-triton": 1})


def test_internal_route_probe_is_only_a_control_get(monkeypatch):
    context = deployment.Context()
    calls = []
    def run(command, **kwargs):
        calls.append(command)
        return subprocess.CompletedProcess(command, 0)
    monkeypatch.setattr(deployment.subprocess, "run", run)
    assert context.triton_route_ready([container("nginx", "gateway")])
    assert calls[0][-1] == "http://127.0.0.1:8000/v2/models/tiny2/versions/1/ready"
    assert "wget" in calls[0] and "POST" not in calls[0]


def test_internal_triton_route_does_not_replay_post_and_is_not_published():
    config = (ROOT / "nginx/conf.d/triton.conf").read_text()
    assert "server ner-triton:8000 resolve" in config
    assert "least_conn;" in config and "proxy_next_upstream off;" in config
    assert "listen 8000;" in config
    assert "non_idempotent" not in config


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
    assert "$(NER_TEST) proxy" in makefile.split("test-ner-proxy:", 1)[1].split("test-ner-integration:", 1)[0]
    runner = (ROOT / "tests/e2e/ner_integration.py").read_text()
    assert "from deployment_topology_gate import Gate" in runner


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
