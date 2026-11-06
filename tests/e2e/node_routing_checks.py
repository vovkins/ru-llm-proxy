"""Real stock Nginx: two logical pools, one local attempt and bounded failover."""

import hashlib
import ipaddress
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from concurrent.futures import ThreadPoolExecutor

from ner_test_support import ROOT, docker, wait_ready

sys.path.insert(0, str(ROOT / "scripts"))
import node_bundle
from node_config import NodeConfiguration

CLIENT = """
import json,sys,urllib.request,urllib.error
d=json.load(sys.stdin)
r=urllib.request.Request(d['url'], data=json.dumps(d['payload']).encode() if 'payload' in d else None,
    headers={'Content-Type':'application/json',**d.get('headers',{})})
try: response=urllib.request.build_opener(urllib.request.ProxyHandler({})).open(r,timeout=d.get('timeout',90))
except urllib.error.HTTPError as e: response=e
with response:
 b=response.read(16384)
 try: b=json.loads(b)
 except ValueError: b=None
 print(json.dumps({'status':response.status,'body':b,'retry_after':response.headers.get('Retry-After')}))
"""


def output(*args):
    return docker(*args, capture_output=True, text=True).stdout.strip()


def call(client, url, payload=None, headers=None, timeout=90):
    data = {"url": url, "headers": headers or {}, "timeout": timeout}
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


def check_node_responses(*, budget_probe=None):
    prefix = "ru-node-routing-" + uuid.uuid4().hex[:8]
    a, b, first, second, client = [prefix + suffix for suffix in ("-a", "-b", "-analyzer-a", "-analyzer-b", "-client")]
    networks = [prefix + suffix for suffix in ("-link", "-local-a", "-local-b")]
    names = [a, b, first, second, client]
    blackhole = prefix + "-syn-blackhole"
    names.append(blackhole)
    result = {"check": "two_node_transport", "status": "running", "cases": []}
    with tempfile.TemporaryDirectory(prefix="ru-node-routing-") as directory:
        root = Path(directory)
        shutil.copytree(ROOT / "nginx", root / "nginx")
        try:
            for network in networks:
                output("network", "create", "--internal", network)
            subnet = ipaddress.ip_network(json.loads(output("network", "inspect", networks[0]))[0]["IPAM"]["Config"][0]["Subnet"])
            address_a, address_b, address_client = [str(subnet.network_address + n) for n in (20, 21, 22)]
            fixture = f"{ROOT}/tests/e2e/mock_node_analyzer.py:/fixture.py:ro"
            for name, network in ((first, networks[1]), (second, networks[2])):
                output("run", "-d", "--name", name, "--network", network, "--network-alias", "presidio-analyzer",
                       "-v", fixture, "python:3.12-slim", "python", "/fixture.py")
            output("run", "-d", "--name", client, "--network", networks[0], "--ip", address_client,
                   "python:3.12-slim", "sleep", "600")
            config = NodeConfiguration((address_a + "/32",), f"http://{address_b}:5001")
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
            wait_ready(ready, 30)
            base = f"http://{address_a}:5001"
            def control(mode_a, mode_b, delay=0):
                for peer, mode in ((first, mode_a), (second, mode_b)):
                    assert call(peer, "http://127.0.0.1:5001/_control", {"mode": mode, "delay": delay})["status"] == 200
                time.sleep(6)
            def stats():
                return [call(peer, "http://127.0.0.1:5001/_stats")["body"] for peer in (first, second)]
            control("ready", "ready")
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
                result["cases"].append({"semantic": mode, "posts": counts, "retry_after": retry})
            control("drop", "drop")
            assert call(client, base + "/api/v1/analyze", {"text": "synthetic"})["status"] == 502
            counts = [len(item["calls"]) for item in stats()]
            assert counts == [1, 1], counts
            result["cases"].append({"both_network_failures_posts": counts})
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
            control("bad_gateway", "ready")
            for _ in range(4):
                assert call(client, base + "/api/v1/analyze", {"text": "synthetic"})["status"] == 200
            counts = [len(item["calls"]) for item in stats()]
            assert counts[0] > 0 and counts[1] == 4 and sum(counts) <= 8, counts
            result["cases"].append({"http_502_posts": counts})

            # Both first operations are held; inspect actual entries before
            # cutting B, without assuming round-robin after health requests.
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
            output("network", "connect", "--ip", address_b, networks[0], b)
            control("ready", "ready")
            for _ in range(6):
                assert call(client, base + "/api/v1/analyze", {"text": "synthetic"})["status"] == 200
            assert all(row["calls"] for row in stats())
            result["cases"].append({"link_restored": True})

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
            keys = ("tcp_keepalive_time", "tcp_keepalive_intvl", "tcp_keepalive_probes")
            values = [[output("exec", node, "cat", "/proc/sys/net/ipv4/" + key) for key in keys] for node in (a, b)]
            assert values[0] == ["10", "5", "3"] and values[1] != values[0], values
            result["container_keepalive_values"] = {"primary": values[0], "compute_control": values[1]}
            denied = call(client, f"http://{address_b}:5001/api/v1/health", headers={"X-Forwarded-For": address_a, "X-Real-IP": address_a})
            assert denied["status"] == 403, denied
            assert call(client, base + "/metrics")["status"] == 404
            assert call(client, base + "/api/v1/analyze")["status"] == 405
            for node in (a, b):
                output("exec", node, "nginx", "-t")
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
            template.write_bytes(normal)
            output("rm", "-f", a)
            start_node(a, "primary", networks[1], address_a)
            wait_ready(ready, 30)
            control("ready", "ready")
            if budget_probe is not None:
                result["external_budget"] = budget_probe(networks[1], client, base, control, stats)
            control("ready", "ready")
            output("network", "disconnect", networks[0], b)
            output("run", "-d", "--name", blackhole, "--network", networks[0], "--ip", address_b,
                   "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
                   "-v", f"{ROOT}/tests/e2e/mock_syn_blackhole.py:/fixture.py:ro",
                   "python:3.12-slim", "python", "/fixture.py")
            def silent_listener_ready():
                return subprocess.run(["docker", "exec", blackhole, "test", "-f", "/tmp/blackhole-ready"],
                                      capture_output=True, timeout=10).returncode == 0
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
            result.update(status="passed", acl_denied_untrusted_source=True)
            return result
        except BaseException:
            diagnostic = Path(tempfile.mkdtemp(prefix="ru-node-routing-diagnostic-"))
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
