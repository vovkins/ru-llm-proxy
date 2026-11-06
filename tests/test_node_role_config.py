"""Two-node configuration and local lifecycle contracts."""

import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from node_config import MAX_CONFIG_BYTES, NodeConfigurationError, load_node_configuration

spec = importlib.util.spec_from_file_location("node_role_deployment", ROOT / "scripts/deployment.py")
deployment = importlib.util.module_from_spec(spec)
spec.loader.exec_module(deployment)


@pytest.fixture(autouse=True)
def clean_context(monkeypatch):
    for key in ("NODE_ROLE", "NODE_CONFIG", "STACK", "TOPOLOGY", "TOPOLOGY_ENV", "ENV_FILE",
                "PII_GUARDRAIL_MODE", "PII_MODE_ORIGIN", "STACK_START_TIMEOUT", "COMPOSE_PROJECT_NAME",
                "PRESIDIO_ANALYZER_QUEUE_TIMEOUT_SECONDS", *deployment.RESOURCE_KEYS,
                *deployment.REMOVED_SETTINGS):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("ENV_FILE", str(ROOT / ".env.compute.example"))
    monkeypatch.setenv("COMPOSE_PROJECT_NAME", "node-config-contract-only")


@pytest.fixture
def node_file(tmp_path):
    path = tmp_path / "nodes with spaces.json"
    path.write_text((ROOT / "deploy/nodes.example.json").read_text())
    return path


def write_config(path, *, url="http://ner-b.internal:5001", sources=None):
    path.write_text(json.dumps({"primary": {"source_cidrs": sources if sources is not None else ["192.0.2.10/32"]},
                                "compute": {"analyzer_url": url}}))


@pytest.mark.parametrize("url,expected", [
    ("http://192.0.2.20:5001", "http://192.0.2.20:5001"),
    ("http://NER-B.Internal:5001/", "http://ner-b.internal:5001"),
    ("http://ner-b.internal", "http://ner-b.internal:80"),
    ("http://ner-b.internal.:5001", "http://ner-b.internal:5001"),
    ("http://[2001:db8::20]:5001", "http://[2001:db8::20]:5001"),
])
def test_node_json_uses_standard_url_and_cidr_parsers(node_file, url, expected):
    write_config(node_file, url=url, sources=["192.0.2.10/32", "2001:db8::/64"])
    config = load_node_configuration(node_file)
    assert config.analyzer_url == expected
    assert config.source_cidrs == ("192.0.2.10/32", "2001:db8::/64")


@pytest.mark.parametrize("url", [
    None, True, 5001, [], {}, "", "https://ner-b:5001", "ftp://ner-b:5001", "//ner-b:5001",
    "http://", "http:///ner-b", "http://ner-b:0", "http://ner-b:65536", "http://ner-b:-1",
    "http://ner-b:bad", "http://ner-b:", "http://ner-b:5001/api", "http://ner-b:5001//",
    "http://ner-b:5001?", "http://ner-b:5001#", "http://ner-b:5001?token=private-sentinel",
    "http://user:private-sentinel@ner-b:5001", "http://user@ner-b:5001", "http://@ner-b:5001",
    " http://ner-b:5001", "http://ner-b:5001\n", "http://ner-b\r\n:5001", "http://ner-b\\:5001",
    "http://ner-b;return:5001", "http://${PRIVATE}:5001", "http://ner_b:5001", "http://-ner:5001",
    "http://ner..internal:5001", "http://999.999.999.999:5001", "http://127.1:5001",
    "http://[::1]suffix:5001", "http://[::1]:", "http://[fe80::1%eth0]:5001",
])
def test_node_json_rejects_ambiguous_or_credentialed_endpoints_without_echo(node_file, url):
    write_config(node_file, url=url)
    with pytest.raises(NodeConfigurationError) as error:
        load_node_configuration(node_file)
    assert "compute.analyzer_url" in str(error.value)
    assert "private-sentinel" not in str(error.value)


@pytest.mark.parametrize("sources", [
    None, "192.0.2.10/32", True, [], [True], [123], ["192.0.2.10"], ["bad/32"],
    ["0.0.0.0/0"], ["::/0"], ["192.0.2.10/24"], ["192.0.2.0/33"],
    ["192.0.2.10/32", "192.0.2.10/32"], ["192.0.2.10/32\n"], ["fe80::%eth0/64"],
])
def test_node_json_rejects_invalid_or_unrestricted_sources(node_file, sources):
    node_file.write_text(json.dumps({"primary": {"source_cidrs": sources},
                                     "compute": {"analyzer_url": "http://ner-b:5001"}}))
    with pytest.raises(NodeConfigurationError, match="primary.source_cidrs"):
        load_node_configuration(node_file)


@pytest.mark.parametrize("raw", [
    "not-json", "{}", "[]", "null", '"private-sentinel"',
    '{"primary":{},"compute":{}}',
    '{"primary":{"source_cidrs":["192.0.2.1/32"]},"primary":{},"compute":{}}',
    '{"primary":{"source_cidrs":[],"source_cidrs":[]},"compute":{}}',
    '{"primary":{"source_cidrs":[]},"compute":{"analyzer_url":NaN}}',
    '{"primary":{"source_cidrs":[]},"compute":{"analyzer_url":Infinity}}',
    '{"primary":{"source_cidrs":[]},"compute":{},"api_key":"private-sentinel"}',
    '{"primary":{"source_cidrs":[],"unexpected":"private-sentinel"},"compute":{}}',
])
def test_node_json_rejects_invalid_schema_and_duplicate_fields_without_disclosure(node_file, raw):
    node_file.write_text(raw)
    with pytest.raises(NodeConfigurationError) as error:
        load_node_configuration(node_file)
    assert "private-sentinel" not in str(error.value)


def test_node_json_bounds_reads_and_hides_private_path(node_file):
    node_file.write_bytes(b" " * (MAX_CONFIG_BYTES + 1))
    with pytest.raises(NodeConfigurationError, match="64"):
        load_node_configuration(node_file)
    node_file.write_bytes(b"\xff\xfe")
    with pytest.raises(NodeConfigurationError, match="UTF-8"):
        load_node_configuration(node_file)
    with pytest.raises(NodeConfigurationError) as error:
        load_node_configuration(node_file / "private-sentinel")
    assert "private-sentinel" not in str(error.value)


def role_context(monkeypatch, node_file, role, stack="litellm-presidio"):
    monkeypatch.setenv("TOPOLOGY", "production")
    monkeypatch.setenv("NODE_ROLE", role)
    monkeypatch.setenv("NODE_CONFIG", str(node_file))
    return deployment.Context(stack=stack)


@pytest.mark.parametrize("role", ["primary", "compute"])
@pytest.mark.parametrize("stack", sorted(deployment.STACKS))
def test_roles_select_only_their_local_components(monkeypatch, node_file, role, stack):
    context = role_context(monkeypatch, node_file, role, stack)
    assert context.node_config.source_cidrs == ("192.0.2.10/32",)
    assert context.counts["presidio-analyzer"] == 24
    assert context.counts["ner-triton"] == 12
    if role == "compute":
        assert context.files == ["docker-compose.production.compute.yml"]
        assert set(context.counts) == {"nginx", "presidio-analyzer", "ner-triton"}
    else:
        assert context.counts["litellm"] == 2
        assert "docker-compose.production.primary.yml" in context.files
        assert ("docker-compose.production.primary.codex-lb.yml" in context.files) == context.extended
        assert ("codex-lb" in context.counts) == (stack == "litellm-presidio-codex-lb")


@pytest.mark.parametrize("role", ["", "PRIMARY", "worker", "standalone"])
def test_invalid_roles_are_not_silently_ignored(monkeypatch, role):
    monkeypatch.setenv("NODE_ROLE", role)
    with pytest.raises(deployment.DeploymentError, match="NODE_ROLE"):
        deployment.Context()


def test_role_requires_production_and_node_config_requires_a_role(monkeypatch):
    monkeypatch.setenv("NODE_ROLE", "compute")
    with pytest.raises(deployment.DeploymentError, match="production"):
        deployment.Context()
    monkeypatch.delenv("NODE_ROLE")
    monkeypatch.setenv("NODE_CONFIG", "private-sentinel")
    with pytest.raises(deployment.DeploymentError) as error:
        deployment.Context()
    assert "private-sentinel" not in str(error.value)


@pytest.mark.parametrize("key", ["NODE_ROLE", "NODE_CONFIG"])
def test_role_context_is_not_hidden_in_env_file(tmp_path, key):
    path = tmp_path / ".env"
    path.write_text(key + "=private-sentinel\n")
    with pytest.raises(deployment.DeploymentError) as error:
        deployment.Context(env_file=str(path))
    assert "ENV_FILE" in str(error.value)
    assert "private-sentinel" not in str(error.value)


def test_model_selection_does_not_require_runtime_addresses_or_keys(monkeypatch):
    monkeypatch.setenv("NODE_ROLE", "compute")
    monkeypatch.setenv("TOPOLOGY", "production")
    monkeypatch.setenv("NODE_CONFIG", "/missing/private-sentinel")
    context = deployment.Context(stack="litellm-presidio-codex-lb")
    assert set(context.counts) == {"nginx", "presidio-analyzer", "ner-triton"}
    with pytest.raises(deployment.DeploymentError) as error:
        context.node_config
    assert "private-sentinel" not in str(error.value)


def test_compute_retains_existing_resource_overrides(tmp_path, monkeypatch, node_file):
    override = tmp_path / "resources.env"
    override.write_text("ANALYZER_REPLICAS=3\nTRITON_REPLICAS=2\nTRITON_MODEL_INSTANCES=4\nANALYZER_MEMORY=3g\n")
    monkeypatch.setenv("TOPOLOGY_ENV", str(override))
    context = role_context(monkeypatch, node_file, "compute")
    assert context.counts == {"nginx": 1, "presidio-analyzer": 3, "ner-triton": 2}
    assert context.values["ANALYZER_MEMORY"] == "3g"
    assert context.values["TRITON_MODEL_INSTANCES"] == "4"


@pytest.mark.parametrize("role", ["primary", "compute"])
def test_start_and_setup_reject_bad_addresses_before_subprocess_or_env_writes(tmp_path, monkeypatch, node_file, role):
    context = role_context(monkeypatch, node_file, role)
    node_file.write_text("{}")
    monkeypatch.setattr(deployment.subprocess, "run", lambda *_a, **_k: pytest.fail("unexpected Docker/process call"))
    for operation in (lambda: context.guard("preflight"), lambda: context.setup(),
                      lambda: context.start()):
        with pytest.raises(deployment.DeploymentError, match="NODE_CONFIG"):
            operation()


@pytest.mark.parametrize("action", ["exec true", "url", "start --only-litellm"])
def test_compute_rejects_primary_cli_operations_before_docker(tmp_path, monkeypatch, node_file, action):
    role_context(monkeypatch, node_file, "compute")
    fake = tmp_path / "docker"
    trace = tmp_path / "trace"
    fake.write_text('#!/bin/sh\nprintf "%s\\n" "$*" >> "$DOCKER_TRACE"\nexit 99\n')
    fake.chmod(0o755)
    env = dict(os.environ, PATH=str(tmp_path) + os.pathsep + os.environ["PATH"], DOCKER_TRACE=str(trace))
    result = subprocess.run([sys.executable, str(ROOT / "scripts/deployment.py"), *action.split()],
                            env=env, text=True, capture_output=True)
    assert result.returncode == 2
    assert "primary" in result.stderr
    assert not trace.exists()


@pytest.mark.parametrize("raw", [None, "{", '{}', '{"primary":{},"compute":{}}'])
def test_invalid_node_config_stops_read_only_cli_before_docker(tmp_path, monkeypatch, node_file, raw):
    role_context(monkeypatch, node_file, "compute")
    if raw is None:
        node_file.unlink()
    else:
        node_file.write_text(raw)
    fake = tmp_path / "docker"
    trace = tmp_path / "trace"
    fake.write_text('#!/bin/sh\nprintf called >> "$DOCKER_TRACE"\nexit 99\n')
    fake.chmod(0o755)
    env = dict(os.environ, PATH=str(tmp_path) + os.pathsep + os.environ["PATH"], DOCKER_TRACE=str(trace))
    result = subprocess.run([sys.executable, str(ROOT / "scripts/deployment.py"), "compose", "config", "--quiet"],
                            env=env, text=True, capture_output=True)
    assert result.returncode == 2
    assert "NODE_CONFIG" in result.stderr
    assert not trace.exists()


@pytest.mark.parametrize("target", sorted(deployment.PRIMARY_COMMANDS))
def test_make_rejects_frontend_commands_on_compute_before_docker(tmp_path, monkeypatch, node_file, target):
    role_context(monkeypatch, node_file, "compute")
    trace = tmp_path / "docker.trace"
    fake = tmp_path / "docker"
    fake.write_text('#!/bin/sh\nprintf "%s\\n" "$*" >> "$DOCKER_TRACE"\nexit 99\n')
    fake.chmod(0o755)
    env = dict(os.environ, PATH=str(tmp_path) + os.pathsep + os.environ["PATH"], DOCKER_TRACE=str(trace))
    for key in ("MAKEFLAGS", "MFLAGS", "MAKELEVEL", "MAKEOVERRIDES"):
        env.pop(key, None)
    result = subprocess.run(["make", "--no-print-directory", target, "STACK=litellm-presidio-codex-lb",
        "PYTHON_LOCAL=" + sys.executable, "DOCKER_DESKTOP_BIN=" + str(tmp_path)],
        cwd=ROOT, env=env, text=True, capture_output=True)
    assert result.returncode != 0
    assert "primary" in result.stderr
    assert not trace.exists(), result.stdout


@pytest.mark.skipif(shutil.which("docker") is None, reason="Compose CLI not installed")
@pytest.mark.parametrize("stack", sorted(deployment.STACKS))
@pytest.mark.parametrize("override", [False, True])
def test_resolved_compute_model_has_no_primary_components_or_secrets(tmp_path, monkeypatch, node_file, stack, override):
    for key in ("CODEX_LB_API_KEY", "CODEX_LB_POSTGRES_PASSWORD", "ZAI_API_KEY", "ZAI_API_KEY_2",
                "LITELLM_MASTER_KEY", "LITELLM_SALT_KEY", "POSTGRES_PASSWORD", "UI_PASSWORD"):
        monkeypatch.setenv(key, "primary-private-sentinel")
    monkeypatch.setenv("PRESIDIO_ANALYZER_QUEUE_TIMEOUT_SECONDS", "19" if override else "45")
    context = role_context(monkeypatch, node_file, "compute", stack)
    model = context.model
    assert set(model["services"]) == set(context.counts)
    assert not model.get("volumes")
    assert set(model["networks"]) == {"ru-llm-proxy"}
    assert "primary-private-sentinel" not in json.dumps(model)
    for name, service in model["services"].items():
        assert not service.get("ports"), name
        assert not service.get("container_name"), name
        assert service["labels"]["ru-llm-proxy.node-role"] == "compute"
        assert set(service.get("depends_on", {})) <= set(context.counts)
    analyzer = model["services"]["presidio-analyzer"]
    assert analyzer["environment"]["PRESIDIO_ANALYZER_QUEUE_TIMEOUT_SECONDS"] == ("19" if override else "45")
    assert analyzer["environment"]["PRESIDIO_ANALYZER_CONCURRENCY_LIMIT"] == "2"
    assert analyzer["environment"]["PRESIDIO_ANALYZER_QUEUE_LIMIT"] == "32"
    assert analyzer["scale"] == 24
    assert model["services"]["ner-triton"]["scale"] == 12


@pytest.mark.skipif(shutil.which("docker") is None, reason="Compose CLI not installed")
@pytest.mark.parametrize("stack", sorted(deployment.STACKS))
def test_resolved_primary_model_changes_only_the_role_marker(monkeypatch, node_file, stack):
    monkeypatch.setenv("TOPOLOGY", "production")
    monkeypatch.setenv("ENV_FILE", str(ROOT / ".env.example"))
    monkeypatch.setenv("CODEX_LB_API_KEY", "synthetic-only")
    monkeypatch.setenv("CODEX_LB_POSTGRES_PASSWORD", "synthetic-only")
    before = deployment.Context(stack=stack).model
    context = role_context(monkeypatch, node_file, "primary", stack)
    after = context.model
    for service in context.counts:
        assert after["services"][service]["labels"].pop("ru-llm-proxy.node-role") == "primary"
    assert after == before


@pytest.mark.skipif(shutil.which("docker") is None, reason="Compose CLI not installed")
@pytest.mark.parametrize("stack", sorted(deployment.STACKS))
def test_compute_requires_no_primary_secrets(monkeypatch, node_file, stack):
    for key in ("CODEX_LB_API_KEY", "CODEX_LB_POSTGRES_PASSWORD", "LITELLM_MASTER_KEY",
                "POSTGRES_PASSWORD", "ZAI_API_KEY", "ZAI_API_KEY_2"):
        monkeypatch.delenv(key, raising=False)
    context = role_context(monkeypatch, node_file, "compute", stack)
    assert set(context.model["services"]) == {"nginx", "presidio-analyzer", "ner-triton"}


def test_shared_ner_definitions_use_standard_extends_without_publication():
    import yaml

    shared = yaml.safe_load((ROOT / "docker-compose.ner.yml").read_text())["services"]
    base = yaml.safe_load((ROOT / "docker-compose.yml").read_text())["services"]
    compute = yaml.safe_load((ROOT / "docker-compose.production.compute.yml").read_text())["services"]
    assert set(shared) == {"presidio-analyzer", "ner-triton"}
    for name, service in shared.items():
        assert "ports" not in service and "container_name" not in service
        for caller in (base, compute):
            assert caller[name]["extends"] == {"file": "docker-compose.ner.yml", "service": name}
        assert service["networks"] == ["ru-llm-proxy"]
    assert base["presidio-analyzer"]["container_name"] == "presidio-analyzer"
    assert base["presidio-analyzer"]["ports"] == ["5001:5001"]


def test_ci_and_docs_keep_local_only_stage_explicit():
    import yaml

    baseline = (ROOT / ".github/workflows/baseline.yml").read_text()
    for path in ("docker-compose.ner.yml", "docker-compose.production.primary.yml", "docker-compose.production.primary.codex-lb.yml",
                 "docker-compose.production.compute.yml", "deploy/nodes.example.json"):
        assert path in baseline
    assert "for role in primary compute" in baseline
    yaml.safe_load(baseline)
    assert "tests/test_node_role_config.py" in (ROOT / "Makefile").read_text()
    for filename in (".gitignore", ".dockerignore"):
        assert "deploy/nodes.local.json" in (ROOT / filename).read_text().splitlines()
    docs = (ROOT / "docs/deployment.md").read_text()
    assert "местные пулы" in docs
    assert "этапу 4" in docs and "миграц" in docs
    compute = (ROOT / ".env.compute.example").read_text()
    for name in ("LITELLM_MASTER_KEY", "POSTGRES_PASSWORD", "CODEX_LB_API_KEY", "ZAI_API_KEY"):
        assert name not in compute


def test_baseline_yaml_step_parses_all_actual_configs_including_scalar_reset():
    import yaml

    workflow = yaml.safe_load((ROOT / ".github/workflows/baseline.yml").read_text())
    step = next(item for item in workflow["jobs"]["baseline"]["steps"] if item.get("name") == "Parse YAML configs")
    env = dict(os.environ, PATH=str(Path(sys.executable).parent) + os.pathsep + os.environ["PATH"])
    result = subprocess.run(["bash", "-e", "-c", step["run"]], cwd=ROOT, env=env,
                            text=True, capture_output=True)
    assert result.returncode == 0, result.stderr
    assert "docker-compose.production.yml ok" in result.stdout
    assert "docker-compose.production.compute.yml ok" in result.stdout


@pytest.mark.parametrize("role", ["primary", "compute"])
@pytest.mark.parametrize("stack", sorted(deployment.STACKS))
def test_role_build_uses_only_local_services_without_addresses_secrets_or_start(tmp_path, monkeypatch, role, stack):
    monkeypatch.setenv("NODE_ROLE", role)
    monkeypatch.setenv("TOPOLOGY", "production")
    monkeypatch.setenv("NODE_CONFIG", "/missing/private-sentinel")
    for key in ("CODEX_LB_API_KEY", "CODEX_LB_POSTGRES_PASSWORD", "ZAI_API_KEY", "ZAI_API_KEY_2"):
        monkeypatch.delenv(key, raising=False)
    context = deployment.Context(stack=stack, env_file=str(tmp_path / ".env"))
    calls = []
    context._model = {"services": {name: {"build": {}} if name in {"litellm", "presidio-analyzer", "ner-triton", "codex-lb"} else {"image": name}
                                   for name in context.counts}}
    monkeypatch.setattr(context, "containers", lambda: [])
    monkeypatch.setattr(context, "compose", lambda *args: calls.append(args))
    context.validate_operations(["build"])
    context.build()
    assert {command[0] for command in calls} == {"pull", "build"}
    services = set(calls[0][1:]) | set(calls[1][2:])
    assert services == set(context.counts)
    assert calls[1][1] == "--no-cache"
    assert not context.env_file.exists()
    if role == "compute":
        assert calls[0] == ("pull", "nginx")
        assert calls[1] == ("build", "--no-cache", "presidio-analyzer", "ner-triton")


@pytest.mark.parametrize("role", ["primary", "compute"])
def test_role_build_does_not_retag_images_of_a_legacy_project(monkeypatch, node_file, role):
    context = role_context(monkeypatch, node_file, role)
    context._model = {"services": {name: {} for name in context.counts}}
    monkeypatch.setattr(context, "containers", lambda: [role_container("nginx", None)])
    calls = []
    monkeypatch.setattr(context, "compose", lambda *args: calls.append(args))
    with pytest.raises(deployment.DeploymentError, match="NODE_ROLE"):
        context.build()
    assert not calls


@pytest.mark.parametrize("stack", sorted(deployment.STACKS))
def test_compute_setup_is_minimal_private_idempotent_and_never_bootstraps_frontend(tmp_path, monkeypatch, node_file, stack):
    context = role_context(monkeypatch, node_file, "compute", stack)
    context.env_file = tmp_path / ".env.compute"
    checks = []
    monkeypatch.setattr(context, "guard", lambda operation: checks.append(operation))
    monkeypatch.setattr(context, "containers", lambda: [])
    monkeypatch.setattr(deployment.subprocess, "run", lambda *_a, **_k: pytest.fail("frontend/bootstrap subprocess"))
    context.setup()
    assert checks == ["mutation"]
    expected = (ROOT / ".env.compute.example").read_bytes()
    assert context.env_file.read_bytes() == expected
    assert context.env_file.stat().st_mode & 0o777 == 0o600
    context.env_file.write_bytes(expected + b"PRESIDIO_ANALYZER_QUEUE_TIMEOUT_SECONDS=19\n")
    context.env_file.chmod(0o640)
    context.setup()
    assert context.env_file.read_bytes() == expected + b"PRESIDIO_ANALYZER_QUEUE_TIMEOUT_SECONDS=19\n"
    assert context.env_file.stat().st_mode & 0o777 == 0o640


@pytest.mark.parametrize("origin", ["environment", "command line", "file"])
def test_compute_rejects_explicit_pii_policy_before_writes(tmp_path, monkeypatch, node_file, origin):
    monkeypatch.setenv("NODE_ROLE", "compute")
    monkeypatch.setenv("TOPOLOGY", "production")
    path = tmp_path / ".env"
    path.write_text("PII_GUARDRAIL_MODE=mask\n" if origin == "file" else "KEEP=value\n")
    if origin != "file":
        monkeypatch.setenv("PII_GUARDRAIL_MODE", "mask")
        monkeypatch.setenv("PII_MODE_ORIGIN", origin)
    with pytest.raises(deployment.DeploymentError, match="primary"):
        deployment.Context(env_file=str(path))
    assert path.read_text() == ("PII_GUARDRAIL_MODE=mask\n" if origin == "file" else "KEEP=value\n")


def role_container(service, role):
    return {"Id": service, "Name": "/" + service, "State": {"Running": True, "Health": {"Status": "healthy"}},
            "Config": {"Labels": {"com.docker.compose.service": service,
                       **({"ru-llm-proxy.node-role": role} if role else {})}, "Env": []}}


@pytest.mark.parametrize("requested,actual", [(None, "compute"), (None, "primary"),
    ("compute", None), ("compute", "primary"), ("primary", None), ("primary", "compute")])
def test_role_mismatch_is_rejected_including_stopped_projects(monkeypatch, node_file, requested, actual):
    context = role_context(monkeypatch, node_file, requested) if requested else deployment.Context()
    item = role_container("nginx", actual)
    item["State"]["Running"] = False
    with pytest.raises(deployment.DeploymentError, match="NODE_ROLE"):
        context.guard_role([item])


@pytest.mark.parametrize("role", ["primary", "compute"])
def test_matching_role_checks_actual_component_set(monkeypatch, node_file, role):
    context = role_context(monkeypatch, node_file, role)
    context._model = {"services": {name: {} for name in context.counts}}
    items = [role_container(name, role) for name in context.counts]
    context.guard_role(items)
    with pytest.raises(deployment.DeploymentError):
        context.guard_role(items + [role_container("foreign-database", role)])
    items[-1]["Config"]["Labels"].pop("ru-llm-proxy.node-role")
    with pytest.raises(deployment.DeploymentError):
        context.guard_role(items)


@pytest.mark.parametrize("role", ["primary", "compute"])
def test_partial_role_project_cannot_be_treated_as_legacy(monkeypatch, node_file, role):
    context = role_context(monkeypatch, node_file, role)
    context._model = {"services": {name: {} for name in context.counts}}
    monkeypatch.delenv("NODE_ROLE")
    monkeypatch.delenv("NODE_CONFIG")
    legacy = deployment.Context()
    for service in context.counts:
        item = role_container(service, role)
        context.guard_role([item])
        with pytest.raises(deployment.DeploymentError, match="NODE_ROLE"):
            legacy.guard_role([item])


def test_primary_cannot_adopt_an_unlabelled_gateway(monkeypatch, node_file):
    context = role_context(monkeypatch, node_file, "primary")
    with pytest.raises(deployment.DeploymentError, match="NODE_ROLE"):
        context.guard_role([role_container("nginx", None), role_container("ner-triton", "primary")])


@pytest.mark.parametrize("stack", sorted(deployment.STACKS))
def test_primary_setup_retains_stock_env_and_codex_bootstrap(tmp_path, monkeypatch, node_file, stack):
    context = role_context(monkeypatch, node_file, "primary", stack)
    path = tmp_path / ".env"
    path.write_text("KEEP=unchanged\n")
    context.env_file = path
    monkeypatch.setattr(context, "guard", lambda *_: None)
    monkeypatch.setattr(context, "containers", lambda: [])
    monkeypatch.setattr(context, "summary", lambda: None)
    calls = []
    monkeypatch.setattr(deployment.subprocess, "run", lambda command, **kwargs: calls.append(command))
    context.setup()
    assert calls[0] == ["bash", "scripts/setup_env.sh", str(path), ".env.example", stack]
    assert calls[1:] == ([["bash", "scripts/setup_codex_lb.sh", str(path)]] if context.extended else [])
    assert path.read_text() == "KEEP=unchanged\n"


def test_compute_health_checks_all_model_and_analyzer_replicas(monkeypatch, node_file):
    context = role_context(monkeypatch, node_file, "compute")
    items = [role_container(name, "compute") for name, count in context.counts.items() for _ in range(count)]
    monkeypatch.setattr(context, "containers", lambda: items)
    calls = []
    monkeypatch.setattr(context, "ner_ready", lambda item: calls.append(item) or True)
    monkeypatch.setattr(context, "triton_ready", lambda item: calls.append(item) or True)
    monkeypatch.setattr(context, "gateway_ready", lambda *_: True)
    monkeypatch.setattr(context, "triton_route_ready", lambda *_: True)
    context.health()
    assert len(calls) == 36
    items[-1]["State"]["Health"]["Status"] = "unhealthy"
    with pytest.raises(deployment.DeploymentError, match="не готов"):
        context.health()


@pytest.mark.parametrize("force", [False, True])
@pytest.mark.parametrize("stack", sorted(deployment.STACKS))
def test_compute_start_runs_only_local_ner_sequentially(monkeypatch, node_file, force, stack):
    context = role_context(monkeypatch, node_file, "compute", stack)
    context._model = {"name": "unit-test"}
    calls = []
    monkeypatch.setattr(context, "guard", lambda op: calls.append(("guard", op)))
    monkeypatch.setattr(context, "containers", lambda: [])
    monkeypatch.setattr(context, "summary", lambda: None)
    monkeypatch.setattr(context, "compose", lambda *args: calls.append(args))
    monkeypatch.setattr(context, "health", lambda *args, **kwargs: None)
    context.start(force=force)
    up = [args for args in calls if args[0] == "up"]
    assert up[0][-1] == "nginx"
    assert all("--no-build" in args for args in up)
    triton = [args for args in up if args[-1] == "ner-triton"]
    analyzer = [args for args in up if args[-1] == "presidio-analyzer"]
    assert len(triton) == 12 and len(analyzer) == 24
    assert up.index(triton[-1]) < up.index(analyzer[0])
    assert {args[-1] for args in up} == {"nginx", "ner-triton", "presidio-analyzer"}
    assert all(("--force-recreate" in batch[0]) == force for batch in (triton, analyzer))
    assert all("--force-recreate" not in args for batch in (triton, analyzer) for args in batch[1:])


def test_compute_checks_gateway_locally_without_frontend_or_remote_b(monkeypatch, node_file):
    context = role_context(monkeypatch, node_file, "compute", "litellm-presidio-codex-lb")
    calls = []
    def run(command, **kwargs):
        calls.append(command)
        return subprocess.CompletedProcess(command, 0)
    monkeypatch.setattr(deployment.subprocess, "run", run)
    assert context.gateway_ready([role_container("nginx", "compute")])
    assert [item[-1] for item in calls] == ["http://127.0.0.1/nginx-health", "http://127.0.0.1:5001/api/v1/health"]


def test_compute_metrics_are_per_replica_without_frontend_or_addresses(monkeypatch, node_file, capsys):
    context = role_context(monkeypatch, node_file, "compute")
    node_file.unlink()
    items = [role_container(name, "compute") for name in ("presidio-analyzer", "ner-triton")]
    monkeypatch.setattr(context, "guard", lambda *_: None)
    monkeypatch.setattr(context, "health", lambda: None)
    monkeypatch.setattr(context, "containers", lambda: items)
    calls = []
    def output(command, **kwargs):
        calls.append(command)
        return "ru_presidio_analyzer_requests_total 1\n" if command[2] == "presidio-analyzer" else "nv_inference_count 1\n"
    monkeypatch.setattr(deployment.subprocess, "check_output", output)
    context.metrics()
    assert len(calls) == 2
    assert "litellm" not in capsys.readouterr().out


@pytest.mark.parametrize("role", ["primary", "compute"])
def test_all_cleanup_is_rejected_before_docker(monkeypatch, node_file, role):
    context = role_context(monkeypatch, node_file, role)
    with pytest.raises(deployment.DeploymentError, match="STACK=all"):
        context.validate_operations(["clean-all"])


def fake_docker(tmp_path, model):
    fixture, trace = tmp_path / "model.json", tmp_path / "calls.jsonl"
    fixture.write_text(json.dumps(model))
    script = tmp_path / "docker"
    script.write_text(f"#!{sys.executable}\n" +
        "import json, os, sys\n" +
        "with open(os.environ['DOCKER_TRACE'], 'a') as stream: stream.write(json.dumps(sys.argv[1:]) + '\\n')\n" +
        "if 'config' in sys.argv:\n" +
        "    with open(os.environ['DOCKER_MODEL']) as stream: print(stream.read())\n" +
        "elif sys.argv[1] == 'ps':\n" +
        "    if os.environ.get('DOCKER_CONTAINERS'): print('existing-container')\n" +
        "elif sys.argv[1] == 'inspect': print(os.environ['DOCKER_CONTAINERS'])\n" +
        "elif 'pull' not in sys.argv and 'build' not in sys.argv: sys.exit(91)\n")
    script.chmod(0o755)
    env = dict(os.environ, PATH=str(tmp_path) + os.pathsep + os.environ["PATH"],
               DOCKER_TRACE=str(trace), DOCKER_MODEL=str(fixture))
    for key in ("MAKEFLAGS", "MFLAGS", "MAKELEVEL", "MAKEOVERRIDES", "NODE_ROLE", "NODE_CONFIG", "STACK"):
        env.pop(key, None)
    return env, trace


@pytest.mark.skipif(shutil.which("docker") is None, reason="Compose CLI not installed")
@pytest.mark.parametrize("role", ["primary", "compute"])
@pytest.mark.parametrize("stack", sorted(deployment.STACKS))
def test_make_build_exports_role_and_never_needs_node_file_or_creates_env(tmp_path, monkeypatch, node_file, role, stack):
    monkeypatch.setenv("CODEX_LB_API_KEY", "synthetic-only")
    monkeypatch.setenv("CODEX_LB_POSTGRES_PASSWORD", "synthetic-only")
    context = role_context(monkeypatch, node_file, role, stack)
    model = context.model
    env, trace = fake_docker(tmp_path, model)
    env_path = tmp_path / "missing.env"
    result = subprocess.run(["make", "--no-print-directory", "build", "STACK=" + stack,
        "NODE_ROLE=" + role, "NODE_CONFIG=" + str(tmp_path / "missing.json"), "ENV_FILE=" + str(env_path),
        "PYTHON_LOCAL=" + sys.executable, "DOCKER_DESKTOP_BIN=" + str(tmp_path)],
        cwd=ROOT, env=env, text=True, capture_output=True)
    assert result.returncode == 0, result.stderr
    calls = [json.loads(line) for line in trace.read_text().splitlines()]
    mutation = [call for call in calls if "pull" in call or "build" in call]
    assert len(mutation) == 2
    assert set(mutation[0][mutation[0].index("pull") + 1:] + mutation[1][mutation[1].index("--no-cache") + 1:]) == set(context.counts)
    assert not env_path.exists()
    assert all(not ({"up", "run", "create", "exec"} & set(call)) for call in calls)


@pytest.mark.skipif(shutil.which("docker") is None, reason="Compose CLI not installed")
def test_make_compute_setup_without_existing_env_preserves_role_and_creates_no_frontend_keys(tmp_path, monkeypatch, node_file):
    context = role_context(monkeypatch, node_file, "compute", "litellm-presidio-codex-lb")
    env, trace = fake_docker(tmp_path, context.model)
    env_path = tmp_path / ".env.compute"
    result = subprocess.run(["make", "--no-print-directory", "setup", "STACK=litellm-presidio-codex-lb",
        "NODE_ROLE=compute", "NODE_CONFIG=" + str(node_file), "ENV_FILE=" + str(env_path),
        "PYTHON_LOCAL=" + sys.executable, "DOCKER_DESKTOP_BIN=" + str(tmp_path)],
        cwd=ROOT, env=env, text=True, capture_output=True)
    assert result.returncode == 0, result.stderr
    assert env_path.read_bytes() == (ROOT / ".env.compute.example").read_bytes()
    calls = [json.loads(line) for line in trace.read_text().splitlines()]
    assert all("up" not in call and "run" not in call for call in calls)


@pytest.mark.parametrize("role", ["primary", "compute"])
def test_make_all_cleanup_with_role_never_calls_docker(tmp_path, monkeypatch, node_file, role):
    env, trace = fake_docker(tmp_path, {})
    result = subprocess.run(["make", "--no-print-directory", "clean", "STACK=all", "NODE_ROLE=" + role,
        "TOPOLOGY=production", "PYTHON_LOCAL=" + sys.executable, "DOCKER_DESKTOP_BIN=" + str(tmp_path)],
        cwd=ROOT, env=env, text=True, capture_output=True)
    assert result.returncode != 0
    assert "STACK=all" in result.stderr
    assert not trace.exists()


@pytest.mark.parametrize("role", ["primary", "compute"])
@pytest.mark.parametrize("target", ["setup", "up", "restart"])
def test_make_bad_runtime_addresses_have_zero_docker_calls(tmp_path, monkeypatch, node_file, role, target):
    env, trace = fake_docker(tmp_path, {})
    path = tmp_path / ".env"
    path.write_text("KEEP=unchanged\n")
    result = subprocess.run(["make", "--no-print-directory", target, "STACK=litellm-presidio", "NODE_ROLE=" + role,
        "TOPOLOGY=production", "NODE_CONFIG=" + str(tmp_path / "missing.json"), "ENV_FILE=" + str(path),
        "PYTHON_LOCAL=" + sys.executable, "DOCKER_DESKTOP_BIN=" + str(tmp_path)],
        cwd=ROOT, env=env, text=True, capture_output=True)
    assert result.returncode != 0
    assert "NODE_CONFIG" in result.stderr
    assert not trace.exists()
    assert path.read_text() == "KEEP=unchanged\n"


@pytest.mark.parametrize("target", ["down", "logs", "clean"])
def test_make_wrong_role_never_mutates_or_streams_another_project(tmp_path, monkeypatch, node_file, target):
    context = role_context(monkeypatch, node_file, "compute")
    model = {"name": "isolated-test", "services": {name: {} for name in context.counts}}
    env, trace = fake_docker(tmp_path, model)
    env["DOCKER_CONTAINERS"] = json.dumps([role_container("nginx", "primary")])
    result = subprocess.run(["make", "--no-print-directory", target, "STACK=litellm-presidio", "NODE_ROLE=compute",
        "TOPOLOGY=production", "PYTHON_LOCAL=" + sys.executable, "DOCKER_DESKTOP_BIN=" + str(tmp_path)],
        cwd=ROOT, env=env, text=True, capture_output=True, input="y\n")
    assert result.returncode != 0
    assert "NODE_ROLE" in result.stderr
    calls = [json.loads(line) for line in trace.read_text().splitlines()]
    assert all(not ({"down", "logs", "up", "stop", "rm"} & set(call)) for call in calls)
