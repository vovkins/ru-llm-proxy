"""Check the real Nginx against disposable Analyzer response fixtures."""
import json
import subprocess
import time
import uuid

from ner_test_support import ROOT, docker, wait_ready


CLIENT = """
import json,sys,urllib.request,urllib.error
data=json.load(sys.stdin)
request=urllib.request.Request(data['url'],
    data=json.dumps(data['payload']).encode() if 'payload' in data else None,
    headers={'Content-Type':'application/json'})
try:
    response=urllib.request.urlopen(request,timeout=10)
except urllib.error.HTTPError as error:
    response=error
with response:
    try:
        body=json.loads(response.read(8193))
    except ValueError:
        body=None
    print(json.dumps({'status':response.status,'body':body,
                      'retry_after':response.headers.get('Retry-After')}))
"""


def output(*args):
    return docker(*args, capture_output=True, text=True).stdout.strip()


def fixture_request(client, url, payload=None):
    data = {"url": url}
    if payload is not None:
        data["payload"] = payload
    response = subprocess.run(["docker", "exec", "-i", client, "python", "-c", CLIENT],
                              input=json.dumps(data), text=True, capture_output=True, timeout=15)
    if response.returncode:
        raise RuntimeError("Disposable Analyzer fixture request failed")
    return json.loads(response.stdout)


def check_analyzer_responses():
    prefix = "ru-analyzer-routing-" + uuid.uuid4().hex[:8]
    first, second, gateway, dead = [prefix + suffix for suffix in ("-a", "-b", "-nginx", "-dead")]
    names = [first, second, gateway, dead]
    fixture = f"{ROOT}/tests/e2e/mock_capacity_analyzer.py:/fixture.py:ro"
    try:
        output("network", "create", "--internal", prefix)
        for name in (first, second):
            output("run", "-d", "--name", name, "--network", prefix,
                   "--network-alias", "presidio-analyzer", "-v", fixture,
                   "python:3.12-slim", "python", "/fixture.py")
        output("run", "-d", "--name", gateway, "--network", prefix,
               "-v", f"{ROOT}/nginx/conf.d/production.conf:/etc/nginx/conf.d/default.conf:ro",
               "-v", f"{ROOT}/nginx/conf.d/triton.conf:/etc/nginx/conf.d/triton.conf:ro",
               "nginx:1.27-alpine")
        output("exec", gateway, "nginx", "-t")

        def ready():
            try:
                return all(fixture_request(peer, "http://127.0.0.1:5001/_stats")["status"] == 200
                           for peer in (first, second))
            except (RuntimeError, subprocess.SubprocessError):
                return False

        wait_ready(ready, 30)
        address = json.loads(output("inspect", gateway))[0]["NetworkSettings"]["Networks"][prefix]["IPAddress"]
        base = f"http://{address}:5001"

        def route_ready():
            try:
                return fixture_request(first, base + "/api/v1/health")["status"] == 200
            except (RuntimeError, subprocess.SubprocessError):
                return False

        wait_ready(route_ready, 30)
        result = {"check": "analyzer_503_routing", "status": "passed"}
        for mode, code, retry in (("overloaded", "analyzer_overloaded", "7"),
                                  ("unavailable", "required_ner_unavailable", "5")):
            for peer in (first, second):
                fixture_request(peer, "http://127.0.0.1:5001/_control", {"mode": mode})
            for _ in range(8):
                response = fixture_request(first, base + "/api/v1/analyze", {"text": "synthetic"})
                assert response["status"] == 503, f"Expected stable HTTP 503, received {response['status']}"
                assert response["body"]["detail"]["code"] == code
                assert response["retry_after"] == retry
            calls = [fixture_request(peer, "http://127.0.0.1:5001/_stats")["body"]["calls"]
                     for peer in (first, second)]
            assert sum(calls) == 8 and all(calls), "A 503 was replayed or a responding peer was excluded"
            result[mode + "_responses"] = sum(calls)
        log = docker("logs", gateway, capture_output=True, text=True)
        assert "no live upstreams" not in log.stderr + log.stdout
        assert "temporarily disabled" not in log.stderr + log.stdout
        # A semantic 503 must remain visible even beside a healthy peer.
        fixture_request(first, "http://127.0.0.1:5001/_control", {"mode": "ready"})
        statuses = [fixture_request(first, base + "/api/v1/analyze", {"text": "synthetic"})["status"]
                    for _ in range(8)]
        assert set(statuses) == {200, 503}
        result["mixed_peer_statuses"] = statuses
        fixture_request(second, "http://127.0.0.1:5001/_control", {"mode": "ready"})
        for _ in range(8):
            assert fixture_request(first, base + "/api/v1/analyze", {"text": "synthetic"})["status"] == 200
        result["recovered_responses"] = 8
        output("run", "-d", "--name", dead, "--network", prefix,
               "--network-alias", "presidio-analyzer", "python:3.12-slim", "sleep", "120")
        time.sleep(3)
        for _ in range(16):
            assert fixture_request(first, base + "/api/v1/analyze", {"text": "synthetic"})["status"] == 200
        log = docker("logs", gateway, capture_output=True, text=True)
        assert "Connection refused" in log.stderr + log.stdout, "Network failover was not exercised"
        result["network_failover_responses"] = 16
        return result
    finally:
        subprocess.run(["docker", "rm", "-f", *names], capture_output=True, timeout=30)
        removed = subprocess.run(["docker", "network", "rm", prefix], capture_output=True, timeout=30)
        if removed.returncode and subprocess.run(
            ["docker", "network", "inspect", prefix], capture_output=True, timeout=30
        ).returncode == 0:
            raise RuntimeError("Analyzer routing test network cleanup failed")
