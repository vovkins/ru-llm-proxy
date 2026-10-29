"""Fault injection in disposable test containers only."""
from __future__ import annotations

from contextlib import contextmanager
import ipaddress
import json
import re
import subprocess
import uuid

from ner_test_support import ROOT, docker, wait_ready


def output(*args):
    return docker(*args, capture_output=True, text=True).stdout.strip()


RECOVERY_STEPS = frozenset({
    "select-replicas", "alter-test-manifest", "reject-mismatched-manifest",
    "restore-test-manifest", "verify-restored-manifest", "stop-replica",
    "reject-stopped-replica", "remaining-peer-capacity", "remove-stopped-replica",
    "reserve-old-address", "start-replacement", "verify-new-address", "verify-restored-response",
})
STDERR_SIGNATURES = (
    (r"user specified IP address is supported only when connecting to networks with user configured subnets",
     "explicit_ip_requires_configured_subnet", "user specified IP address requires a user configured subnet"),
    (r"address already in use", "address_in_use", "address already in use"),
    (r"invalid endpoint settings", "invalid_endpoint_settings", "invalid endpoint settings"),
    (r"invalid config for network", "invalid_network_config", "invalid config for network"),
    (r"no such container", "container_missing", "no such container"),
    (r"container[^\r\n]{0,200}is not running", "container_stopped", "container is not running"),
    (r"network[^\r\n]{0,200}not found", "network_missing", "network not found"),
    (r"no space left on device", "disk_full", "no space left on device"),
    (r"cannot allocate memory", "allocation_failed", "cannot allocate memory"),
    (r"permission denied", "permission_denied", "permission denied"),
    (r"connection refused", "connection_refused", "connection refused"),
    (r"context deadline exceeded", "deadline_exceeded", "context deadline exceeded"),
)


def _command_diagnostic(exc):
    # Never serialize argv or raw stderr: either may contain credentials or bodies.
    command = exc.cmd
    operation = "unreported-command"
    if (isinstance(command, (list, tuple)) and len(command) >= 2
            and command[0] == "docker" and isinstance(command[1], str)
            and command[1] in {"run", "exec", "stop", "rm", "inspect", "network"}):
        operation = "docker " + command[1]
    stderr = exc.stderr
    if isinstance(stderr, bytes):
        stderr = stderr[:8192].decode("utf-8", errors="replace")
    elif isinstance(stderr, str):
        stderr = stderr[:8192]
    else:
        stderr = ""
    recognized = [(category, excerpt) for pattern, category, excerpt in STDERR_SIGNATURES
                  if re.search(pattern, stderr, re.IGNORECASE)]
    return {"operation": operation, "returncode": exc.returncode if type(exc.returncode) is int else None,
            "stderr_classes": [category for category, _ in recognized],
            "stderr_excerpt": "; ".join(excerpt for _, excerpt in recognized)[:512]
            if recognized else "stderr omitted: no allowlisted diagnostic"}


@contextmanager
def _recovery_step(gate, name):
    if name not in RECOVERY_STEPS:
        raise ValueError("Unknown recovery diagnostic step")
    result = {"step": name, "status": "running"}
    gate.result.setdefault("recovery_steps", []).append(result)
    try:
        yield result
    except BaseException as exc:
        result.update(status="failed" if isinstance(exc, Exception) else "interrupted",
                      error_type=type(exc).__name__)
        if isinstance(exc, subprocess.CalledProcessError):
            result.update(_command_diagnostic(exc))
        raise
    else:
        result["status"] = "passed"
    finally:
        print("NER RECOVERY " + json.dumps(result, sort_keys=True), flush=True)


COUNTER_SERVER = """
from http.server import BaseHTTPRequestHandler, HTTPServer
count = 0
class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        global count
        count += 1
        self.rfile.read(int(self.headers.get('Content-Length', '0')))
        self.send_response(503)
        self.end_headers()
        self.wfile.write(b'busy')
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(str(count).encode())
    def log_message(self, *args): pass
HTTPServer(('0.0.0.0', 8000), Handler).serve_forever()
"""


def network_ipv4_subnet(network):
    try:
        rows = json.loads(output("network", "inspect", network))
        if not isinstance(rows, list) or len(rows) != 1 or rows[0]["Internal"] is not True:
            raise ValueError
        configs = rows[0]["IPAM"]["Config"]
        if not isinstance(configs, list):
            raise ValueError
        subnets = [ipaddress.ip_network(item["Subnet"]) for item in configs]
        ipv4 = [subnet for subnet in subnets if subnet.version == 4]
        if len(ipv4) != 1 or ipv4[0].num_addresses < 4:
            raise ValueError
        return str(ipv4[0])
    except (ValueError, TypeError, KeyError, IndexError):
        raise RuntimeError("Test network must have one usable IPv4 subnet") from None


def check_no_replay():
    prefix = "ru-ner-no-replay-" + uuid.uuid4().hex[:8]
    nginx, first, second = [prefix + suffix for suffix in ("-nginx", "-a", "-b")]
    names = [nginx, first, second]
    try:
        output("network", "create", "--internal", prefix)
        subnet = network_ipv4_subnet(prefix)
        for name in (first, second):
            output("run", "-d", "--name", name, "--network", prefix, "--network-alias", "ner-triton",
                   "python:3.12-slim", "python", "-c", COUNTER_SERVER)
        output("run", "-d", "--name", nginx, "--network", prefix,
               "-v", f"{ROOT}/nginx/conf.d/triton.conf:/etc/nginx/conf.d/default.conf:ro", "nginx:1.27-alpine")
        output("exec", nginx, "nginx", "-t")
        get = "import urllib.request; print(urllib.request.urlopen('{url}', timeout=3).read().decode())"

        def ready():
            try:
                return all(output("exec", name, "python", "-c", get.format(url="http://127.0.0.1:8000/count")) == "0"
                           for name in (first, second)) and output(
                               "exec", nginx, "wget", "-qO-", "-T", "3",
                               "http://127.0.0.1:8000/v2/models/tiny2/versions/1/ready") == "0"
            except subprocess.CalledProcessError:
                return False

        wait_ready(ready, 30)
        ip = json.loads(output("inspect", nginx))[0]["NetworkSettings"]["Networks"][prefix]["IPAddress"]
        post = (
            "import urllib.request,urllib.error; "
            f"r=urllib.request.Request('http://{ip}:8000/v2/models/tiny2/versions/1/infer',data=b'test');\n"
            "try:\n urllib.request.urlopen(r,timeout=10)\n"
            "except urllib.error.HTTPError as e:\n print(e.code)"
        )
        assert output("exec", first, "python", "-c", post) == "503"
        counts = [int(output("exec", name, "python", "-c", get.format(url="http://127.0.0.1:8000/count")))
                  for name in (first, second)]
        assert sorted(counts) == [0, 1], "Nginx replayed one POST to another peer"
        return {"check": "single_post_no_replay", "status": "passed", "peer_post_counts": counts,
                "ipv4_subnet": subnet}
    finally:
        subprocess.run(["docker", "rm", "-f", *names], capture_output=True, timeout=30)
        removed = subprocess.run(["docker", "network", "rm", prefix], capture_output=True, timeout=30)
        if removed.returncode and subprocess.run(
            ["docker", "network", "inspect", prefix], capture_output=True, timeout=30
        ).returncode == 0:
            raise RuntimeError("No-replay test network cleanup failed")


def check_replica_recovery(gate, ctx, token):
    from deployment import DeploymentError
    from deployment_topology_gate import request, assert_request

    with _recovery_step(gate, "select-replicas"):
        replicas = [c for c in ctx.containers() if c["Config"]["Labels"].get("com.docker.compose.service") == "ner-triton"]
        assert len(replicas) == 2
        first = replicas[0]
    patch = (
        "import json; from pathlib import Path; p=Path('/models/tiny2/export-manifest.json'); "
        "p.with_suffix('.test-backup').write_bytes(p.read_bytes()); d=json.loads(p.read_text()); "
        "d['onnx_sha256']='b'*64; p.write_text(json.dumps(d))"
    )
    with _recovery_step(gate, "alter-test-manifest"):
        output("exec", first["Id"], "python3", "-c", patch)
    try:
        with _recovery_step(gate, "reject-mismatched-manifest"):
            try:
                ctx.health()
            except DeploymentError:
                pass
            else:
                raise AssertionError("One mismatched replica was hidden by the healthy peer")
    finally:
        with _recovery_step(gate, "restore-test-manifest"):
            output("exec", first["Id"], "python3", "-c",
                   "from pathlib import Path; p=Path('/models/tiny2/export-manifest.json'); "
                   "p.write_bytes(p.with_suffix('.test-backup').read_bytes()); p.with_suffix('.test-backup').unlink()")
    with _recovery_step(gate, "verify-restored-manifest"):
        ctx.health(20)
    with _recovery_step(gate, "stop-replica"):
        output("stop", first["Id"])
    with _recovery_step(gate, "reject-stopped-replica"):
        try:
            ctx.health()
        except DeploymentError:
            pass
        else:
            raise AssertionError("Stopped replica was accepted as a complete healthy stack")
    url = gate.url(ctx, "nginx", 80)
    attempts = []

    def remaining_capacity():
        status, body = request(url, "/v1/chat/completions", {"model": "mock-chat", "messages": [
            {"role": "user", "content": f"Телефон клиента +79031234569. Проверка {len(attempts)}."}]}, token)
        attempts.append(status)
        if status == 200:
            assert "+79031234569" in body
            return True
        assert status == 503, "Unexpected failure while the remaining same-model peer recovers"
        assert "+79031234569" not in body
        return False

    # These are separate client attempts, not automatic replay of one POST.
    with _recovery_step(gate, "remaining-peer-capacity") as diagnostic:
        try:
            wait_ready(remaining_capacity, 45)
        finally:
            diagnostic["client_attempt_statuses"] = list(attempts)
    with _recovery_step(gate, "remove-stopped-replica"):
        network, old_info = next(iter(first["NetworkSettings"]["Networks"].items()))
        old_ip = old_info["IPAddress"]
        output("rm", first["Id"])
    holder = gate.project + "-address-holder"
    try:
        with _recovery_step(gate, "reserve-old-address"):
            output("run", "-d", "--name", holder, "--network", network, "--ip", old_ip,
                   "python:3.12-slim", "sleep", "240")
        with _recovery_step(gate, "start-replacement"):
            ctx.start()
        with _recovery_step(gate, "verify-new-address"):
            current = [c for c in ctx.containers() if c["Config"]["Labels"].get("com.docker.compose.service") == "ner-triton"]
            replacement = next(c for c in current if c["Id"] not in {r["Id"] for r in replicas})
            assert replacement["NetworkSettings"]["Networks"][network]["IPAddress"] != old_ip
        with _recovery_step(gate, "verify-restored-response"):
            assert_request(gate.url(ctx, "nginx", 80), "mask", "responses", True, token, "+79031234569")
    finally:
        subprocess.run(["docker", "rm", "-f", holder], capture_output=True, timeout=30)
    gate.result["checks"].append({"check": "triton_replica_recovery", "status": "passed",
                                  "wrong_artifact_rejected": True, "stopped_replica_rejected": True,
                                  "client_attempt_statuses": attempts, "replacement_new_ip": True})
