"""Two-node configuration contracts; lifecycle operations remain disabled."""

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
        assert context.files[-1] == "docker-compose.production.primary.yml"
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
def test_new_roles_reject_mutation_before_subprocess_or_env_writes(tmp_path, monkeypatch, node_file, role):
    context = role_context(monkeypatch, node_file, role)
    monkeypatch.setattr(deployment.subprocess, "run", lambda *_a, **_k: pytest.fail("unexpected Docker/process call"))
    for operation in (lambda: context.guard("preflight"), lambda: context.setup(),
                      lambda: context.start(), lambda: context.health(), lambda: context.metrics(),
                      lambda: context.persist_mode(), lambda: context.compose("up", "-d"),
                      lambda: context.compose("config", "--output", str(tmp_path / "forbidden"))):
        with pytest.raises(deployment.DeploymentError, match="этапе 3"):
            operation()
    assert not (tmp_path / "forbidden").exists()


@pytest.mark.parametrize("role", ["primary", "compute"])
@pytest.mark.parametrize("action", ["setup", "start", "guard mutation", "health", "metrics", "exec true", "compose down", "compose run nginx"])
def test_cli_operations_are_disabled_without_even_a_docker_lookup(tmp_path, monkeypatch, node_file, role, action):
    role_context(monkeypatch, node_file, role)
    fake = tmp_path / "docker"
    trace = tmp_path / "trace"
    fake.write_text('#!/bin/sh\nprintf "%s\\n" "$*" >> "$DOCKER_TRACE"\nexit 99\n')
    fake.chmod(0o755)
    env = dict(os.environ, PATH=str(tmp_path) + os.pathsep + os.environ["PATH"], DOCKER_TRACE=str(trace))
    result = subprocess.run([sys.executable, str(ROOT / "scripts/deployment.py"), *action.split()],
                            env=env, text=True, capture_output=True)
    assert result.returncode == 2
    assert "этапе 3" in result.stderr
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


@pytest.mark.parametrize("role", ["primary", "compute"])
@pytest.mark.parametrize("target", ["setup", "build", "up", "restart", "down", "clean"])
def test_old_make_recipes_cannot_bypass_the_temporary_ban(tmp_path, monkeypatch, node_file, role, target):
    role_context(monkeypatch, node_file, role)
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
    assert "этапе 3" in result.stderr
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
    assert after["services"]["nginx"]["labels"].pop("ru-llm-proxy.node-role") == "primary"
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


def test_ci_and_docs_keep_configuration_only_stage_explicit():
    import yaml

    baseline = (ROOT / ".github/workflows/baseline.yml").read_text()
    for path in ("docker-compose.ner.yml", "docker-compose.production.primary.yml",
                 "docker-compose.production.compute.yml", "deploy/nodes.example.json"):
        assert path in baseline
    assert "for role in primary compute" in baseline
    yaml.safe_load(baseline)
    assert "tests/test_node_role_config.py" in (ROOT / "Makefile").read_text()
    for filename in (".gitignore", ".dockerignore"):
        assert "deploy/nodes.local.json" in (ROOT / filename).read_text().splitlines()
    docs = (ROOT / "docs/deployment.md").read_text()
    assert "пока только конфигурация" in docs
    assert "временно" in docs and "этапу 3" in docs and "этапу 4" in docs
    compute = (ROOT / ".env.compute.example").read_text()
    for name in ("LITELLM_MASTER_KEY", "POSTGRES_PASSWORD", "CODEX_LB_API_KEY", "ZAI_API_KEY"):
        assert name not in compute
