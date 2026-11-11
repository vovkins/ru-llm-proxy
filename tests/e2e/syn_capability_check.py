"""Bounded socket-filter capability A/B; no host networking or model workload."""

import argparse
import json
from pathlib import Path
import signal
import subprocess
import uuid

from ner_test_support import interruption, wait_ready, write_report
from node_routing_checks import output, syn_fixture_args, syn_fixture_diagnostic, syn_fixture_security, syn_listener_ready

NET_ADMIN_MASK = 1 << 12


def check_capabilities(report):
    report.update(status="running", check="syn_capability_ab", cases=[])
    image = "python:3.12-slim"
    prefix = "ru-node-syn-ab-" + uuid.uuid4().hex[:8]
    try:
        for mode, admin in (("without_capability", False), ("net_admin", True)):
            name = prefix + "-" + mode.replace("_", "-")
            row = {"mode": mode, "ready": False}
            report["cases"].append(row)
            readiness = {}
            try:
                output(*syn_fixture_args(name, "none", net_admin=admin, image=image))
                row["security"] = syn_fixture_security(name, "none", net_admin=admin)
                try:
                    wait_ready(lambda: syn_listener_ready(name, readiness), 10)
                    row["ready"] = True
                except TimeoutError:
                    pass
                row["diagnostic"] = syn_fixture_diagnostic(name, readiness)
                state = row["diagnostic"].get("container", {})
                image = state["image_id"]
                if row["ready"]:
                    caps = output("exec", name, "python", "-c",
                        "import json; from pathlib import Path; rows=dict(line.split(':',1) for line in Path('/proc/self/status').read_text().splitlines() if ':' in line); print(json.dumps({k:int(rows[k].strip(),16) for k in ('CapEff','CapPrm','CapBnd')}))")
                    assert json.loads(caps) == dict.fromkeys(("CapEff", "CapPrm", "CapBnd"), NET_ADMIN_MASK if admin else 0), "Unexpected effective SYN fixture capabilities"
                    probe = subprocess.run(["docker", "exec", name, "python", "-c",
                        "import socket;\ntry: socket.create_connection(('127.0.0.1',5001),timeout=1)\nexcept TimeoutError: pass\nelse: raise AssertionError('Fixture accepted TCP')"],
                        capture_output=True, timeout=5)
                    assert probe.returncode == 0, "SYN capability control did not drop TCP connections"
                    row["connect_timeout"] = "passed"
                    row["outcome"] = "ready_with_capability" if admin else "kernel_allows_unprivileged_filter"
                else:
                    assert not admin, "NET_ADMIN SYN fixture did not become ready"
                    assert state.get("exit_code") == 1 and state.get("oom_killed") is False, "Unexpected negative SYN control state"
                    assert any(event.get("phase") == "filter" and event.get("errno") == 1
                               and event.get("status") == "failed" for event in row["diagnostic"]["events"]), "Negative SYN control failed for an unrelated reason"
                    row["outcome"] = "expected_filter_eperm"
            finally:
                removed = subprocess.run(["docker", "rm", "-f", name], capture_output=True, timeout=30)
                remaining = subprocess.run(["docker", "inspect", name], capture_output=True, timeout=10)
                row["cleanup_returncode"] = removed.returncode
                row["cleanup_ok"] = remaining.returncode != 0
                assert row["cleanup_ok"], "SYN capability control cleanup failed"
        report["status"] = "passed"
    except BaseException as exc:
        report.update(status="failed", error_type=type(exc).__name__)
        raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    signal.signal(signal.SIGTERM, interruption)
    report = {}
    try:
        check_capabilities(report)
    finally:
        write_report(args.report, report)
        print(json.dumps(report, sort_keys=True))


if __name__ == "__main__":
    main()
