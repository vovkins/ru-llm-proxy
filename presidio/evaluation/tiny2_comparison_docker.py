"""Disposable CPU comparison stands; never reads .env or changes a Compose stack."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import subprocess
import sys
import uuid

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT), str(ROOT / "tests/e2e")]
from ner_test_support import container_states, docker, identities, image_id, wait_ready


def quiet(*args):
    return docker(*args, capture_output=True, text=True).stdout


def ready(container):
    state = json.loads(quiet("inspect", container))[0]["State"]
    if not state["Running"]:
        raise RuntimeError("Isolated research process exited before readiness")
    result = subprocess.run(["docker", "exec", container, "python", "-c",
        "import urllib.request; urllib.request.urlopen('http://127.0.0.1:5001/api/v1/health',timeout=3).read()"],
        capture_output=True, timeout=10)
    return result.returncode == 0


def run(args):
    output_dir = args.report_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    packet = args.case_packet.resolve(strict=True)
    prefix = "ru-ner-e9-" + uuid.uuid4().hex[:12]
    analyzer, triton, nginx = [prefix + suffix for suffix in ("-analyzer", "-triton", "-nginx")]
    names = (analyzer, triton, nginx)
    output = output_dir / f"{args.system}-{args.mode}.json"
    if output.exists():
        raise ValueError("Report already exists; select a new output directory")
    state = {"system": args.system, "mode": args.mode, "status": "preparing", "network": prefix}
    try:
        images = {"presidio-analyzer": image_id(args.reference_image if args.system == "reference" else args.analyzer_image)}
        if args.system == "candidate":
            images["ner-triton"] = image_id(args.triton_image)
            state["trusted_model"] = identities(images)
        state["images"] = images
        quiet("network", "create", "--internal", prefix)
        if args.system == "candidate":
            quiet("run", "-d", "--name", nginx, "--network", prefix, "--network-alias", "nginx",
                  "-v", f"{ROOT}/nginx/conf.d/triton.conf:/etc/nginx/conf.d/default.conf:ro", args.nginx_image)
            quiet("run", "-d", "--name", triton, "--network", prefix, "--network-alias", "ner-triton",
                  "--cpus", "2", "--memory", "2g", images["ner-triton"])

            def route_ready():
                backend = subprocess.run(["docker", "exec", triton, "python3", "/opt/ru-ner/verify_triton_service.py"],
                                         capture_output=True, timeout=15)
                route = subprocess.run(["docker", "exec", nginx, "wget", "-qO-", "-T", "3",
                    "http://127.0.0.1:8000/v2/models/tiny2/versions/1/ready"], capture_output=True, timeout=10)
                return backend.returncode == route.returncode == 0

            wait_ready(route_ready, 150)
        quiet("run", "-d", "--name", analyzer, "--network", prefix,
              "--cpus", "4" if args.system == "reference" else "2",
              "--memory", "4g" if args.system == "reference" else "2g",
              "-e", "OMP_NUM_THREADS=2", "-e", "MKL_NUM_THREADS=2", "-e", "OPENBLAS_NUM_THREADS=2",
              "-e", "PYTHONPATH=/app:/workspace", "-e", "PRESIDIO_ANALYZER_BASE64_POLICY=content",
              "-v", f"{ROOT}:/workspace:ro", "-v", f"{packet}:/study/cases.json:ro",
              "-v", f"{output_dir}:/results", images["presidio-analyzer"], "python",
              "/workspace/presidio/evaluation/tiny2_comparison_run.py", "serve")
        wait_ready(lambda: ready(analyzer), 150)
        state["status"] = "evaluating"
        command = ["docker", "exec", analyzer, "python", "/workspace/presidio/evaluation/tiny2_comparison_run.py",
                   "evaluate", "--url", "http://127.0.0.1:5001", "--system", args.system, "--mode", args.mode,
                   "--input-cases", "/study/cases.json", "--output", f"/results/{output.name}"]
        previous_count = -1
        for attempt in range(64):
            result = subprocess.run(command + (["--resume"] if attempt else []), cwd=ROOT, timeout=7200)
            if result.returncode == 0:
                break
            if not output.exists():
                raise RuntimeError("Research run failed before checkpoint")
            count = len(json.loads(output.read_text())["results"])
            if count <= previous_count:
                raise RuntimeError("Research run did not advance after process restart")
            previous_count = count
            quiet("restart", analyzer)
            wait_ready(lambda: ready(analyzer), 150)
        else:
            raise RuntimeError("Research-process restart budget exhausted")
        if not output.is_file() or json.loads(output.read_text()).get("complete") is not True:
            raise RuntimeError("Research subprocess did not produce a complete report")
        state["status"] = "completed"
    except BaseException as exc:
        state.update(status="failed", error_type=type(exc).__name__)
        raise
    finally:
        try:
            existing = quiet("ps", "-aq", "--filter", f"name=^{prefix}-").split()
            state["containers"] = container_states(existing)
            if state["status"] == "failed":
                for name in names:
                    value = subprocess.run(["docker", "logs", "--tail", "100", name], capture_output=True, text=True)
                    (output_dir / (name + ".private.log")).write_text(value.stdout + value.stderr)
        finally:
            subprocess.run(["docker", "rm", "-f", *names], capture_output=True, timeout=30)
            subprocess.run(["docker", "network", "rm", prefix], capture_output=True, timeout=30)
        state["cleanup_ok"] = (not quiet("ps", "-aq", "--filter", f"name=^{prefix}-").strip()
                               and prefix not in quiet("network", "ls", "--format", "{{.Name}}").splitlines())
        (output_dir / f"{args.system}-{args.mode}-state.json").write_text(json.dumps(state, indent=2) + "\n")
        if not state["cleanup_ok"]:
            raise RuntimeError("Research cleanup failed")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("system", choices=("reference", "candidate"))
    parser.add_argument("mode", choices=("analyzer", "ner"))
    parser.add_argument("--case-packet", required=True, type=Path)
    parser.add_argument("--report-dir", required=True, type=Path)
    parser.add_argument("--reference-image", default="ru-llm-proxy-presidio-analyzer:latest")
    parser.add_argument("--analyzer-image", default="ru-llm-proxy-ner-tests:analyzer")
    parser.add_argument("--triton-image", default="ru-llm-proxy-ner-tests:triton")
    parser.add_argument("--nginx-image", default="nginx:1.27-alpine")
    run(parser.parse_args())


if __name__ == "__main__":
    main()
