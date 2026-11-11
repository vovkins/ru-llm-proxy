"""Immutable, validated Nginx configuration generations for local node roles."""

from dataclasses import dataclass
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import shutil
from string import Template
import subprocess
import tempfile
from urllib.parse import urlsplit

from node_config import NodeConfiguration, NodeConfigurationError


class BundleError(ValueError):
    pass


def endpoint(config):
    parsed = urlsplit(config.analyzer_url)
    try:
        address = ipaddress.IPv4Address(parsed.hostname)
        if address.is_loopback or address.is_unspecified or address.is_multicast or address.is_reserved:
            raise ValueError
    except ValueError:
        raise BundleError("NODE_CONFIG: рабочий вход B требует фиксированный внутренний IPv4, не DNS/IPv6/loopback") from None
    return str(address), parsed.port or 80


def render(root, role, config):
    address, port = endpoint(config)
    template = Template((root / f"nginx/templates/node-{role}.conf.template").read_text())
    return template.safe_substitute(COMPUTE_ENDPOINT=f"{address}:{port}",
        SOURCE_RULES="\n    ".join(f"allow {source};" for source in config.source_cidrs)).encode()


@dataclass(frozen=True)
class Bundle:
    directory: Path
    digest: str
    project: str
    role: str

    @property
    def overlay(self):
        return self.directory / "compose.json"


def location(root, project, role):
    if role not in {"primary", "compute"} or not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,127}", project):
        raise BundleError("Некорректный локальный контекст bundle")
    return root / "deploy/generated" / project / role


def payload(role, project, config, content):
    return {"version": 1, "role": role, "project": project,
            "sources": list(config.source_cidrs), "analyzer_url": config.analyzer_url,
            "config_sha256": hashlib.sha256(content).hexdigest()}


def checksum(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def overlay(root, directory, role, config, digest):
    nginx = {"volumes": [
        {"type": "bind", "source": str(root / "nginx/conf.d/node-runtime.conf"),
         "target": "/etc/nginx/conf.d/default.conf", "read_only": True},
        {"type": "bind", "source": str(directory), "target": "/etc/nginx/node-config", "read_only": True}],
        "labels": {"ru-llm-proxy.node-config-sha256": digest}}
    if role == "compute":
        address, port = endpoint(config)
        nginx["ports"] = [{"target": 5001, "published": str(port), "host_ip": address, "protocol": "tcp"}]
    else:
        nginx["sysctls"] = {"net.ipv4.tcp_keepalive_time": "10", "net.ipv4.tcp_keepalive_intvl": "5",
                            "net.ipv4.tcp_keepalive_probes": "3"}
    return {"services": {"nginx": nginx}}


def validate_nginx(root, directory, image):
    # No application networks, published ports, secrets, or host sysctls.
    result = subprocess.run(["docker", "run", "--rm", "--pull=never", "--network", "none",
        "--mount", f"type=bind,src={directory},dst=/etc/nginx/node-config,readonly",
        "--mount", f"type=bind,src={root / 'nginx/conf.d/node-runtime.conf'},dst=/etc/nginx/conf.d/default.conf,readonly",
        "--mount", f"type=bind,src={root / 'nginx/conf.d/triton.conf'},dst=/etc/nginx/conf.d/triton.conf,readonly",
        "--entrypoint", "nginx", image, "-t"], capture_output=True, text=True, timeout=30)
    if result.returncode:
        raise BundleError("Nginx не прошёл проверку нового bundle; прежний комплект сохранён")


def prepare(root, project, role, config, image):
    parent = location(root, project, role)
    content = render(root, role, config)
    metadata = payload(role, project, config, content)
    digest = checksum(metadata)
    target = parent / digest
    parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".prepare-", dir=parent) as temp:
        staging = Path(temp)
        (staging / "default.conf").write_bytes(content)
        (staging / "metadata.json").write_text(json.dumps(metadata, sort_keys=True) + "\n")
        (staging / "compose.json").write_text(json.dumps(overlay(root, target, role, config, digest), sort_keys=True) + "\n")
        validate_nginx(root, staging, image)
        if target.exists():
            for name in ("default.conf", "metadata.json", "compose.json"):
                if (target / name).read_bytes() != (staging / name).read_bytes():
                    raise BundleError("Существующий bundle повреждён; автоматическая перезапись запрещена")
        else:
            shutil.copytree(staging, target)
    # Only the pointer changes; a running container retains its old generation.
    descriptor, temp = tempfile.mkstemp(prefix=".current-", dir=parent)
    try:
        with os.fdopen(descriptor, "w") as stream:
            json.dump({"digest": digest}, stream)
            stream.write("\n")
        os.replace(temp, parent / "current.json")
    finally:
        Path(temp).unlink(missing_ok=True)
    return Bundle(target, digest, project, role)


def load(root, project, role):
    parent = location(root, project, role)
    pointer = parent / "current.json"
    if not pointer.exists():
        raise BundleError("Локальный bundle не подготовлен; выполните setup/up с NODE_CONFIG")
    try:
        selected = json.loads(pointer.read_text())
        digest = selected["digest"]
        if set(selected) != {"digest"} or not isinstance(digest, str) or not re.fullmatch(r"[a-f0-9]{64}", digest):
            raise ValueError
        directory = parent / digest
        metadata = json.loads((directory / "metadata.json").read_text())
        if set(metadata) != {"version", "role", "project", "sources", "analyzer_url", "config_sha256"}:
            raise ValueError
        if metadata["version"] != 1 or metadata["project"] != project or metadata["role"] != role:
            raise ValueError
        content = (directory / "default.conf").read_bytes()
        if checksum(metadata) != digest or hashlib.sha256(content).hexdigest() != metadata["config_sha256"]:
            raise ValueError
        config = NodeConfiguration(tuple(metadata["sources"]), metadata["analyzer_url"])
        expected = overlay(root, directory, role, config, digest)
        if json.loads((directory / "compose.json").read_text()) != expected:
            raise ValueError
    except (OSError, ValueError, KeyError, TypeError, NodeConfigurationError):
        raise BundleError("Локальный bundle повреждён или несовместим; конфигурация не применяется") from None
    return Bundle(directory, digest, project, role)
