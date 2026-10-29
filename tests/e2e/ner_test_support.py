"""Bounded Docker operations shared by the isolated NER checks, not deployment."""
from __future__ import annotations

import json
from datetime import datetime, timezone
import os
from pathlib import Path
import re
import signal
import subprocess
import time
import uuid

ROOT = Path(__file__).resolve().parents[2]
IMAGE_TAGS = {
    "presidio-analyzer": "ru-llm-proxy-ner-tests:analyzer",
    "ner-triton": "ru-llm-proxy-ner-tests:triton",
    "litellm": "ru-llm-proxy-ner-tests:litellm",
    "codex-lb": "ru-llm-proxy-ner-tests:codex-lb",
}


def docker(*args, timeout=120, **kwargs):
    return subprocess.run(["docker", *args], check=True, timeout=timeout, **kwargs)


def image_id(tag):
    result = docker("image", "inspect", tag, "--format", "{{.Id}}", capture_output=True, text=True)
    value = result.stdout.strip()
    if re.fullmatch(r"sha256:[0-9a-f]{64}", value) is None:
        raise RuntimeError("Docker returned an invalid image identity")
    return value


def offline_probe(image, *command, entrypoint=None):
    name = "ru-ner-probe-" + uuid.uuid4().hex[:12]
    args = ["run", "--rm", "--name", name, "--network", "none"]
    if entrypoint:
        args.extend(["--entrypoint", entrypoint])
    try:
        return docker(*args, image, *command, capture_output=True, text=True)
    finally:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True, timeout=30)


def identities(images):
    """Compare both built artifacts with this checkout, not the server with itself."""
    from presidio.model_artifact import load_manifest
    from presidio.onnx_artifact import validate_export_manifest

    code = (
        "from pathlib import Path; print(Path('/opt/ner-export/export-manifest.json').read_text())"
    )
    result = offline_probe(images["presidio-analyzer"], "python", "-c", code)
    artifact = validate_export_manifest(json.loads(result.stdout), load_manifest(ROOT / "presidio/model_manifest.json"))
    expected = {"model_id": artifact["source_model_id"], "revision": artifact["source_revision"],
                "onnx_sha256": artifact["onnx_sha256"]}
    result = offline_probe(images["ner-triton"], "/opt/ru-ner/verify_triton_service.py", "--identity-only", entrypoint="python3")
    if json.loads(result.stdout) != expected:
        raise RuntimeError("Analyzer and Triton are not the same trusted model export")
    return expected


def wait_ready(probe, timeout, *, sleep=time.sleep, clock=time.monotonic):
    deadline = clock() + timeout
    while True:
        if probe():
            return
        remaining = deadline - clock()
        if remaining <= 0:
            raise TimeoutError("NER test readiness deadline exceeded")
        sleep(min(1, remaining))


def check_internal_network(name):
    result = docker("network", "inspect", name, capture_output=True, text=True)
    if json.loads(result.stdout)[0].get("Internal") is not True:
        raise RuntimeError("NER test compute network is not isolated")


def container_states(ids):
    """No env, command, response bodies, raw health output or database URLs."""
    if not ids:
        return []
    result = docker("inspect", *ids, capture_output=True, text=True)
    return [{"name": row["Name"].lstrip("/"), "image_id": row["Image"],
             "status": row["State"]["Status"], "exit_code": row["State"].get("ExitCode"),
             "oom_killed": row["State"].get("OOMKilled"), "restarts": row["RestartCount"],
             "health": row["State"].get("Health", {}).get("Status")}
            for row in json.loads(result.stdout)]


def write_report(path, result):
    if path:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(dict(result, generated_at=datetime.now(timezone.utc).isoformat()),
                                   ensure_ascii=False, indent=2) + "\n")


def run_checks(checks, results, *, case_ids=()):
    """Finish independent groups, but never turn their failure into success."""
    known = frozenset(case_ids)
    for name, check in checks:
        result = {"check": name, "status": "running"}
        results.append(result)
        try:
            check()
        except Exception as exc:
            result.update(status="failed", error_type=type(exc).__name__)
            status = getattr(exc, "code", None)
            if type(status) is int and 100 <= status <= 599:
                result["http_status"] = status
            # Only report identifiers supplied by the corpus, not arbitrary
            # exception bodies containing requests or credentials.
            value = exc.args[0] if exc.args else None
            for _ in range(3):
                if isinstance(value, str) and value in known:
                    result["case_id"] = value
                    break
                if isinstance(value, str):
                    case = value.split(":", 1)[0]
                    if case in known:
                        result["case_id"] = case
                        break
                value = value[0] if isinstance(value, tuple) and value else None
        except BaseException as exc:
            result.update(status="interrupted", error_type=type(exc).__name__)
            raise
        else:
            result["status"] = "passed"
        print(f"NER CHECK {name}: {result['status']}", flush=True)
    if any(result["status"] != "passed" for result in results):
        raise RuntimeError("One or more independent NER checks failed")


def run_child(args, **kwargs):
    """Let a shell gate run its cleanup trap on timeout/cancellation."""
    with subprocess.Popen(args, start_new_session=True, **kwargs) as child:
        try:
            if child.wait(timeout=3600):
                raise RuntimeError("NER test subprocess failed")
        finally:
            if child.poll() is None:
                os.killpg(child.pid, signal.SIGTERM)
                try:
                    child.wait(timeout=45)
                except subprocess.TimeoutExpired:
                    os.killpg(child.pid, signal.SIGKILL)
                    child.wait(timeout=10)


def interruption(_signum, _frame):
    raise KeyboardInterrupt
