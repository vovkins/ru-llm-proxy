"""Real stock Nginx: two logical pools, one local attempt and bounded failover."""

import hashlib
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from concurrent.futures import ThreadPoolExecutor

from ner_test_support import ROOT, configure_test_ipv4_subnet, docker, wait_ready
from triton_routing_checks import _command_diagnostic
from mock_syn_blackhole import EVENT_PREFIX, PHASES as SYN_PHASES, STATUSES as SYN_STATUSES, ERROR_TYPES

sys.path.insert(0, str(ROOT / "scripts"))
import node_bundle
from node_config import NodeConfiguration

ROUTING_PHASES = frozenset({
    "network-setup", "fixture-startup", "nginx-startup", "readiness", "healthy-head",
    "healthy-posts", "semantic-responses", "both-network-failures", "network-failover",
    "http-502-failover", "active-link-loss", "link-restored", "client-deadline",
    "keepalive-and-acl", "read-timeout", "restore-normal-timeout", "external-budget",
    "connect-timeout", "completed",
})


def routing_phase(progress, name):
    if name not in ROUTING_PHASES:
        raise ValueError("Unknown node routing diagnostic phase")
    progress.update(phase=name, status="running")
    print("NER NODE ROUTING " + json.dumps(progress, sort_keys=True), flush=True)


def routing_failure(phase, exc):
    if phase not in ROUTING_PHASES:
        raise ValueError("Unknown node routing diagnostic phase")
    result = {"phase": phase, "status": "failed" if isinstance(exc, Exception) else "interrupted",
              "error_type": type(exc).__name__, "operation": "check"}
    origin = exc.__traceback__
    while origin is not None:
        code = origin.tb_frame.f_code
        if code.co_filename == __file__ and code.co_name not in {"output", "call"}:
            result["origin"] = {"file": "node_routing_checks.py", "function": code.co_name,
                                "line": origin.tb_lineno}
        origin = origin.tb_next
    if isinstance(exc, subprocess.CalledProcessError):
        result.update(_command_diagnostic(exc))
    return result


def private_command_output(directory, exc):
    if not isinstance(exc, (subprocess.CalledProcessError, subprocess.TimeoutExpired)):
        return
    for field in ("stdout", "stderr"):
        value = getattr(exc, field, None)
        if isinstance(value, str):
            value = value[:16384].encode("utf-8", errors="replace")
        if isinstance(value, bytes):
            path = directory / ("command." + field)
            path.write_bytes(value[:16384])
            path.chmod(0o600)


def syn_probe_result(result):
    code = result.returncode if type(result.returncode) is int else None
    parts = []
    for raw in (result.stderr, result.stdout):
        if isinstance(raw, bytes):
            parts.append(raw[:4096].decode("utf-8", errors="replace"))
        elif isinstance(raw, str):
            parts.append(raw[:4096])
    text = "\n".join(parts)
    category = "ready" if code == 0 else "marker_not_confirmed" if code == 1 else "command_failed"
    if code != 0:
        if "executable file not found" in text or "executable not found" in text:
            category = "probe_executable_missing"
        elif re.search(r"container[^\r\n]{0,200}is not running", text, re.IGNORECASE):
            category = "container_not_running"
    return {"returncode": code, "category": category}


def syn_listener_ready(name, progress):
    progress["attempts"] = progress.get("attempts", 0) + 1
    try:
        result = subprocess.run(["docker", "exec", name, "test", "-f", "/tmp/blackhole-ready"],
                                capture_output=True, timeout=10)
    except subprocess.TimeoutExpired:
        progress["last_probe"] = {"returncode": None, "category": "command_timeout"}
        raise
    progress["last_probe"] = syn_probe_result(result)
    return result.returncode == 0


def syn_fixture_args(name, network, *, address=None, net_admin=False, image="python:3.12-slim"):
    args = ["run", "-d", "--name", name, "--network", network]
    if address is not None:
        args += ["--ip", address]
    args += ["--cap-drop", "ALL"]
    if net_admin:
        args += ["--cap-add", "NET_ADMIN"]
    return args + ["--security-opt", "no-new-privileges",
                   "-v", f"{ROOT}/tests/e2e/mock_syn_blackhole.py:/fixture.py:ro",
                   image, "python", "/fixture.py"]


def syn_fixture_security(name, network, *, net_admin):
    response = subprocess.run(["docker", "inspect", "--format",
        '{"add":{{json .HostConfig.CapAdd}},"drop":{{json .HostConfig.CapDrop}},"privileged":{{json .HostConfig.Privileged}},"security":{{json .HostConfig.SecurityOpt}},"network":{{json .HostConfig.NetworkMode}},"ports":{{json .HostConfig.PortBindings}},"binds":{{json .HostConfig.Binds}}}', name],
        capture_output=True, text=True, check=True, timeout=10)
    data = json.loads(response.stdout)
    added = {value.removeprefix("CAP_") for value in (data["add"] or [])}
    assert added == ({"NET_ADMIN"} if net_admin else set()), "Unexpected SYN fixture capabilities"
    assert data["drop"] == ["ALL"] and data["privileged"] is False, "Unexpected SYN fixture isolation"
    assert data["security"] in (["no-new-privileges"], ["no-new-privileges=true"]), "Unexpected SYN fixture security options"
    assert data["network"] == network and data["ports"] in ({}, None), "Unexpected SYN fixture network exposure"
    assert data["binds"] == [f"{ROOT}/tests/e2e/mock_syn_blackhole.py:/fixture.py:ro"], "Unexpected SYN fixture mounts"
    return {"net_admin": net_admin, "other_capabilities": False, "privileged": False,
            "no_new_privileges": True, "ports_published": False, "script_read_only": True}


def syn_fixture_diagnostic(name, readiness):
    attempts = readiness.get("attempts")
    last = readiness.get("last_probe", {})
    last = last if isinstance(last, dict) else {}
    code, category = last.get("returncode"), last.get("category")
    report = {"readiness": {
        "attempts": attempts if type(attempts) is int and 0 <= attempts <= 1000000 else None,
        "last_probe": {"returncode": code if type(code) is int else None,
            "category": category if isinstance(category, str) and category in {
                "ready", "marker_not_confirmed", "command_failed", "probe_executable_missing",
                "container_not_running", "command_timeout"} else "unreported"}},
        "errors": [], "events": []}

    def run(operation, *args):
        try:
            response = subprocess.run(["docker", *args], capture_output=True, timeout=10)
        except (OSError, subprocess.SubprocessError):
            report["errors"].append({"operation": operation, "returncode": None})
            return None
        if response.returncode:
            report["errors"].append({"operation": operation,
                "returncode": response.returncode if type(response.returncode) is int else None})
            return None
        return response.stdout[:8192] if isinstance(response.stdout, bytes) else b""

    state = run("container-state", "inspect", "--format",
        '{"running":{{json .State.Running}},"exit_code":{{json .State.ExitCode}},"oom_killed":{{json .State.OOMKilled}},"image_id":{{json .Image}}}', name)
    if state is not None:
        try:
            data = json.loads(state)
            report["container"] = {key: data.get(key) if type(data.get(key)) is bool else None
                                   for key in ("running", "oom_killed")}
            report["container"]["exit_code"] = data.get("exit_code") if type(data.get("exit_code")) is int else None
            identity = data.get("image_id")
            if isinstance(identity, str) and re.fullmatch(r"sha256:[0-9a-f]{64}", identity):
                report["container"]["image_id"] = identity
                architecture = run("image-architecture", "image", "inspect", "--format", '{{json .Architecture}}', identity)
                if architecture is not None:
                    value = json.loads(architecture)
                    report["architecture"] = value if value in ("amd64", "arm64", "arm", "386", "ppc64le", "s390x") else "unreported"
        except (ValueError, TypeError, AttributeError):
            report["errors"].append({"operation": "container-state-parse", "returncode": None})
    marker = run("marker-python", "exec", name, "python", "-c",
                 "from pathlib import Path; print(int(Path('/tmp/blackhole-ready').is_file()))")
    if marker is not None and marker.strip() in (b"0", b"1"):
        report["marker_present"] = marker.strip() == b"1"
    raw = run("fixture-events", "logs", "--tail", "20", name)
    if raw is not None:
        for line in raw.decode("utf-8", errors="replace").splitlines()[:32]:
            if not line.startswith(EVENT_PREFIX):
                continue
            try:
                event = json.loads(line[len(EVENT_PREFIX):])
                phase, status = event.get("phase"), event.get("status")
                if not isinstance(phase, str) or phase not in SYN_PHASES or not isinstance(status, str) or status not in SYN_STATUSES:
                    continue
                safe = {"phase": phase, "status": status}
                if "error_type" in event:
                    kind, number = event.get("error_type"), event.get("errno")
                    safe.update(error_type=kind if isinstance(kind, str) and kind in ERROR_TYPES else "OtherError",
                                errno=number if type(number) is int and 0 <= number < 4096 else None)
                report["events"].append(safe)
            except (ValueError, TypeError, AttributeError):
                continue
            if len(report["events"]) == 12:
                break
    return report


CLIENT = """
import json,sys,urllib.request,urllib.error
d=json.load(sys.stdin)
r=urllib.request.Request(d['url'], data=json.dumps(d['payload']).encode() if 'payload' in d else None,
    headers={'Content-Type':'application/json',**d.get('headers',{})},method=d.get('method'))
try: response=urllib.request.build_opener(urllib.request.ProxyHandler({})).open(r,timeout=d.get('timeout',90))
except urllib.error.HTTPError as e: response=e
with response:
 raw=response.read(16384)
 try: b=json.loads(raw)
 except ValueError: b=None
 result={'status':response.status,'body':b,'retry_after':response.headers.get('Retry-After')}
 if d.get('method')=='HEAD': result['body_bytes']=len(raw)
 print(json.dumps(result))
"""


def output(*args):
    return docker(*args, capture_output=True, text=True).stdout.strip()


def call(client, url, payload=None, headers=None, timeout=90, *, method=None):
    data = {"url": url, "headers": headers or {}, "timeout": timeout, "method": method}
    if payload is not None:
        data["payload"] = payload
    result = subprocess.run(["docker", "exec", "-i", client, "python", "-c", CLIENT],
                            input=json.dumps(data), capture_output=True, text=True, timeout=timeout + 10)
    if result.returncode:
        raise RuntimeError("Node transport fixture request failed")
    return json.loads(result.stdout)


def short_read_timeout_copy(root):
    """Exercise read timeouts without waiting 1500 seconds; return original bytes."""
    path = root / "nginx/templates/node-primary.conf.template"
    normal = path.read_bytes()
    marker = b"        proxy_read_timeout 1500s;\n        proxy_send_timeout 1500s;\n        proxy_next_upstream error timeout http_502 non_idempotent;"
    assert normal.count(marker) == 1
    path.write_bytes(normal.replace(marker, marker.replace(b"proxy_read_timeout 1500s", b"proxy_read_timeout 2s")))
    return normal


def check_node_responses(*, budget_probe=None, diagnostic=None):
    prefix = "ru-node-routing-" + uuid.uuid4().hex[:8]
    a, b, first, second, client = [prefix + suffix for suffix in ("-a", "-b", "-analyzer-a", "-analyzer-b", "-client")]
    networks = [prefix + suffix for suffix in ("-link", "-local-a", "-local-b")]
    names = [a, b, first, second, client]
    blackhole = prefix + "-syn-blackhole"
    names.append(blackhole)
    result = {"check": "two_node_transport", "status": "running", "cases": []}
    progress = {} if diagnostic is None else diagnostic
    syn_readiness = {}
    with tempfile.TemporaryDirectory(prefix="ru-node-routing-") as directory:
        root = Path(directory)
        shutil.copytree(ROOT / "nginx", root / "nginx")
        try:
            routing_phase(progress, "network-setup")
            for network in networks:
                output("network", "create", "--internal", network)
            subnet = configure_test_ipv4_subnet(networks[0])
            address_a, address_b, address_client = [str(subnet.network_address + n) for n in (20, 21, 22)]
            routing_phase(progress, "fixture-startup")
            fixture = f"{ROOT}/tests/e2e/mock_node_analyzer.py:/fixture.py:ro"
            for name, network in ((first, networks[1]), (second, networks[2])):
                output("run", "-d", "--name", name, "--network", network, "--network-alias", "presidio-analyzer",
                       "-v", fixture, "python:3.12-slim", "python", "/fixture.py")
            output("run", "-d", "--name", client, "--network", networks[0], "--ip", address_client,
                   "python:3.12-slim", "sleep", "600")
            config = NodeConfiguration((address_a + "/32",), f"http://{address_b}:5001")
            routing_phase(progress, "nginx-startup")
            def start_node(name, role, network, address):
                bundle = node_bundle.prepare(root, prefix, role, config, "nginx:1.27-alpine")
                args = ["create", "--name", name, "--network", networks[0], "--ip", address,
                        "-v", f"{bundle.directory}:/etc/nginx/node-config:ro",
                        "-v", f"{root}/nginx/conf.d/node-runtime.conf:/etc/nginx/conf.d/default.conf:ro",
                        "-v", f"{root}/nginx/conf.d/triton.conf:/etc/nginx/conf.d/triton.conf:ro"]
                if role == "primary":
                    for key, value in (("tcp_keepalive_time", 10), ("tcp_keepalive_intvl", 5), ("tcp_keepalive_probes", 3)):
                        args += ["--sysctl", f"net.ipv4.{key}={value}"]
                output(*args, "nginx:1.27-alpine")
                output("network", "connect", "--alias", "nginx", network, name)
                output("start", name)
            for name, role, network, address in ((b, "compute", networks[2], address_b), (a, "primary", networks[1], address_a)):
                start_node(name, role, network, address)
            def ready():
                try:
                    return all(call(peer, "http://127.0.0.1:5001/_stats")["status"] == 200 for peer in (first, second)) and call(client, f"http://{address_a}:5001/api/v1/health")["status"] == 200
                except (RuntimeError, subprocess.SubprocessError):
                    return False
            routing_phase(progress, "readiness")
            wait_ready(ready, 30)
            base = f"http://{address_a}:5001"
            def control(mode_a, mode_b, delay=0):
                for peer, mode in ((first, mode_a), (second, mode_b)):
                    assert call(peer, "http://127.0.0.1:5001/_control", {"mode": mode, "delay": delay})["status"] == 200
                time.sleep(6)
            def stats():
                return [call(peer, "http://127.0.0.1:5001/_stats")["body"] for peer in (first, second)]
            routing_phase(progress, "healthy-head")
            control("ready", "ready")
            for _ in range(4):
                response = call(client, base + "/api/v1/health", method="HEAD")
                assert response == {"status": 200, "body": None, "retry_after": None, "body_bytes": 0}, response
            for endpoint in ("http://127.0.0.1:5002", f"http://{address_b}:5001"):
                output("exec", a, "wget", "--spider", "-q", endpoint + "/api/v1/health")
            assert call(client, base + "/api/v1/health", method="PUT")["status"] == 405
            assert call(client, base + "/api/v1/analyze", method="HEAD")["status"] == 405
            assert sum(len(row["calls"]) for row in stats()) == 0
            routing_phase(progress, "healthy-posts")
            for index in range(8):
                assert call(client, base + "/api/v1/analyze", {"text": "synthetic " + str(index)})["status"] == 200
            counts = [len(item["calls"]) for item in stats()]
            assert sum(counts) == 8 and all(counts), counts
            result["cases"].append({"healthy_posts": counts})
            lines = output("logs", b).splitlines()
            records = [json.loads(line) for line in lines if line.startswith('{"request_id"')]
            connections = [row["connection"] for row in records if row["method"] == "POST"]
            assert len(connections) == counts[1] == len(set(connections))
            assert all(row["connection_requests"] == 1 for row in records)
            result["fresh_central_connections"] = True
            routing_phase(progress, "semantic-responses")
            for mode, retry, expected in (
                ("overloaded", "7", {"detail": {"code": "analyzer_overloaded", "reason": "queue_timeout", "retry_after_seconds": 7}}),
                ("unavailable", "5", {"detail": {"code": "required_ner_unavailable", "phase": "windowing", "failure_class": "not_ready"}}),
                ("boundary", None, {"detail": {"code": "required_ner_unavailable", "phase": "windowing", "failure_class": "window_boundary_unresolved"}}),
                ("rate_limited", "11", {"detail": "synthetic_rate_limit"}),
                ("gateway_timeout", None, {"detail": "synthetic_upstream_504"}),
            ):
                control(mode, mode)
                status = {"rate_limited": 429, "gateway_timeout": 504}.get(mode, 503)
                for _ in range(6):
                    response = call(client, base + "/api/v1/analyze", {"text": "synthetic"})
                    assert response == {"status": status, "body": expected, "retry_after": retry}, response
                counts = [len(item["calls"]) for item in stats()]
                assert sum(counts) == 6 and all(counts), counts
                if mode in {"unavailable", "boundary"}:
                    head_id = "head-" + mode
                    response = call(client, base + "/api/v1/health", headers={"X-Ru-LLM-Request-ID": head_id}, method="HEAD")
                    assert response == {"status": 503, "body": None, "retry_after": retry, "body_bytes": 0}, response
                    logs = output("logs", a)
                    rows = [json.loads(line) for line in logs.splitlines() if line.startswith('{"request_id"')]
                    central = [row for row in rows if row["request_id"] == head_id and row["method"] == "HEAD"]
                    assert len(central) == 1 and central[0]["upstream_status"] == "503", central
                result["cases"].append({"semantic": mode, "posts": counts, "retry_after": retry})
            routing_phase(progress, "both-network-failures")
            control("drop", "drop")
            assert call(client, base + "/api/v1/analyze", {"text": "synthetic"})["status"] == 502
            counts = [len(item["calls"]) for item in stats()]
            assert counts == [1, 1], counts
            result["cases"].append({"both_network_failures_posts": counts})
            routing_phase(progress, "network-failover")
            control("drop", "ready")
            for index in range(6):
                headers = {"X-Ru-LLM-Request-ID": "route-" + str(index), "X-Ru-LLM-Text-Field-Index": "3",
                           "X-Ru-LLM-Analysis-Budget-Seconds": "240"}
                assert call(client, base + "/api/v1/analyze", {"text": "synthetic"}, headers)["status"] == 200
            snapshots = stats()
            assert snapshots[0]["calls"] and len(snapshots[1]["calls"]) == 6
            for record in snapshots[0]["calls"]:
                matches = [item for item in snapshots[1]["calls"] if item["id"] == record["id"]]
                assert len(matches) == 1 and matches[0] == record
            result["cases"].append({"failover_posts": [len(s["calls"]) for s in snapshots]})
            routing_phase(progress, "http-502-failover")
            control("bad_gateway", "ready")
            for _ in range(4):
                assert call(client, base + "/api/v1/analyze", {"text": "synthetic"})["status"] == 200
            counts = [len(item["calls"]) for item in stats()]
            assert counts[0] > 0 and counts[1] == 4 and sum(counts) <= 8, counts
            result["cases"].append({"http_502_posts": counts})

            # Both first operations are held; inspect actual entries before
            # cutting B, without assuming round-robin after health requests.
            routing_phase(progress, "active-link-loss")
            control("delay_first", "delay_first", delay=38)
            started = time.monotonic()
            with ThreadPoolExecutor(max_workers=2) as pool:
                first_call = pool.submit(call, client, base + "/api/v1/analyze", {"text": "synthetic-long-a"},
                                         {"X-Ru-LLM-Request-ID": "active-1"})
                wait_ready(lambda: sum(len(row["calls"]) for row in stats()) == 1, 10)
                second_call = pool.submit(call, client, base + "/api/v1/analyze", {"text": "synthetic-long-b"},
                                          {"X-Ru-LLM-Request-ID": "active-2"})
                wait_ready(lambda: all(len(row["calls"]) == 1 for row in stats()), 10)
                original_b = stats()[1]["calls"][0]["id"]
                output("network", "disconnect", networks[0], b)
                assert first_call.result(timeout=80)["status"] == 200
                assert second_call.result(timeout=80)["status"] == 200
            elapsed = time.monotonic() - started
            snapshots = stats()
            counts = [len(row["calls"]) for row in snapshots]
            assert counts == [2, 1], counts
            assert elapsed >= 38, elapsed
            assert any(row["id"] == original_b for row in snapshots[0]["calls"])
            result["cases"].append({"active_link_loss_posts": counts, "pair_elapsed_seconds": round(elapsed, 2),
                                    "healthy_long_operation_passed": True})
            routing_phase(progress, "link-restored")
            output("network", "connect", "--ip", address_b, networks[0], b)
            control("ready", "ready")
            for _ in range(6):
                assert call(client, base + "/api/v1/analyze", {"text": "synthetic"})["status"] == 200
            assert all(row["calls"] for row in stats())
            result["cases"].append({"link_restored": True})

            routing_phase(progress, "client-deadline")
            control("delay_first", "delay_first", delay=5)
            try:
                call(client, base + "/api/v1/analyze", {"text": "synthetic-cancel"}, timeout=1)
            except RuntimeError:
                pass
            else:
                raise AssertionError("Expected caller deadline")
            time.sleep(6)
            assert sum(len(row["calls"]) for row in stats()) == 1
            result["cases"].append({"client_deadline_no_replay": True})
            routing_phase(progress, "keepalive-and-acl")
            keys = ("tcp_keepalive_time", "tcp_keepalive_intvl", "tcp_keepalive_probes")
            values = [[output("exec", node, "cat", "/proc/sys/net/ipv4/" + key) for key in keys] for node in (a, b)]
            assert values[0] == ["10", "5", "3"] and values[1] != values[0], values
            result["container_keepalive_values"] = {"primary": values[0], "compute_control": values[1]}
            denied = call(client, f"http://{address_b}:5001/api/v1/health", headers={"X-Forwarded-For": address_a, "X-Real-IP": address_a})
            assert denied["status"] == 403, denied
            assert call(client, f"http://{address_b}:5001/api/v1/health", method="HEAD")["status"] == 403
            assert call(client, base + "/metrics")["status"] == 404
            assert call(client, base + "/api/v1/analyze")["status"] == 405
            for node in (a, b):
                output("exec", node, "nginx", "-t")
            routing_phase(progress, "read-timeout")
            template = root / "nginx/templates/node-primary.conf.template"
            normal = short_read_timeout_copy(root)
            output("rm", "-f", a)
            start_node(a, "primary", networks[1], address_a)
            wait_ready(ready, 30)
            control("delay_first", "delay_first", delay=6)
            began = time.monotonic()
            response = call(client, base + "/api/v1/analyze", {"text": "synthetic-read-timeout"})
            elapsed = time.monotonic() - began
            counts = [len(row["calls"]) for row in stats()]
            assert response["status"] == 504 and counts == [1, 1], (response, counts)
            assert 3.5 < elapsed < 10, elapsed
            result["cases"].append({"read_timeout_seconds_test_only": 2, "posts": counts,
                                    "status": response["status"], "elapsed_seconds": round(elapsed, 2)})
            routing_phase(progress, "restore-normal-timeout")
            template.write_bytes(normal)
            output("rm", "-f", a)
            start_node(a, "primary", networks[1], address_a)
            wait_ready(ready, 30)
            control("ready", "ready")
            if budget_probe is not None:
                routing_phase(progress, "external-budget")
                result["external_budget"] = budget_probe(networks[1], client, base, control, stats)
            routing_phase(progress, "connect-timeout")
            control("ready", "ready")
            output("network", "disconnect", networks[0], b)
            output(*syn_fixture_args(blackhole, networks[0], address=address_b, net_admin=True))
            result["syn_fixture_security"] = syn_fixture_security(blackhole, networks[0], net_admin=True)
            def silent_listener_ready():
                return syn_listener_ready(blackhole, syn_readiness)
            wait_ready(silent_listener_ready, 10)
            probe = subprocess.run(["docker", "exec", client, "python", "-c",
                "import socket;\ntry: socket.create_connection(('" + address_b + "',5001),timeout=1)\n"
                "except TimeoutError: pass\nelse: raise AssertionError('Fixture accepted TCP')"],
                capture_output=True, text=True, timeout=5)
            assert probe.returncode == 0, "The fixture did not produce a TCP connect timeout"
            began = time.monotonic()
            for index in range(2):
                response = call(client, base + "/api/v1/analyze", {"text": "synthetic"},
                                {"X-Ru-LLM-Request-ID": "connect-timeout-" + str(index)})
                assert response["status"] == 200, {"case": "link_lost_before_connect", "response": response, "completed": result}
            counts = [len(row["calls"]) for row in stats()]
            assert counts == [2, 0], counts
            logs = subprocess.run(["docker", "logs", a], capture_output=True, text=True, timeout=15)
            assert any("upstream timed out" in line and "while connecting to upstream" in line
                       for line in logs.stderr.splitlines()), "Connect timeout was not exercised"
            records = [json.loads(line) for line in logs.stdout.splitlines() if line.startswith('{"request_id"')]
            attempts = [row["upstream_status"].split(", ") for row in records
                        if row["request_id"].startswith("connect-timeout-")]
            assert ["504", "200"] in attempts and all(len(row) <= 2 for row in attempts), attempts
            result["cases"].append({"link_lost_before_connect": "passed", "posts": counts,
                                    "pair_elapsed_seconds": round(time.monotonic() - began, 2)})
            result.update(status="passed", acl_denied_untrusted_source=True, head_health="passed")
            progress.update(phase="completed", status="passed")
            return result
        except BaseException as exc:
            if progress["phase"] == "connect-timeout":
                try:
                    progress["syn_fixture"] = syn_fixture_diagnostic(blackhole, syn_readiness)
                except Exception:
                    progress["syn_fixture"] = {"diagnostic_status": "collection_failed"}
            progress.update(routing_failure(progress["phase"], exc))
            print("NER NODE ROUTING " + json.dumps(progress, sort_keys=True), flush=True)
            diagnostic = Path(tempfile.mkdtemp(prefix="ru-node-routing-diagnostic-"))
            private_command_output(diagnostic, exc)
            for name in names:
                logs = subprocess.run(["docker", "logs", "--tail", "100", name], capture_output=True, timeout=15)
                path = diagnostic / (name + ".log")
                path.write_bytes(logs.stdout + logs.stderr)
                path.chmod(0o600)
            state = subprocess.run(["docker", "inspect", "--format", "{{.Name}} {{json .State}} {{json .NetworkSettings.Networks}}", *names],
                                   capture_output=True, timeout=15)
            path = diagnostic / "state.jsonl"
            path.write_bytes(state.stdout)
            path.chmod(0o600)
            print("Private node routing diagnostics:", diagnostic, flush=True)
            raise
        finally:
            subprocess.run(["docker", "rm", "-f", *names], capture_output=True, timeout=45)
            for network in reversed(networks):
                removed = subprocess.run(["docker", "network", "rm", network], capture_output=True, timeout=30)
                if removed.returncode and subprocess.run(["docker", "network", "inspect", network], capture_output=True).returncode == 0:
                    raise RuntimeError("Node routing network cleanup failed")


if __name__ == "__main__":
    before = hashlib.sha256((ROOT / "nginx/templates/node-primary.conf.template").read_bytes()).hexdigest()
    result = check_node_responses()
    assert before == hashlib.sha256((ROOT / "nginx/templates/node-primary.conf.template").read_bytes()).hexdigest()
    print(json.dumps(result, indent=2))
