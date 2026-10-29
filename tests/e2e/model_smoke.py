"""Implementation of the existing model smoke target, with no external network."""
import argparse
import json
from pathlib import Path
import subprocess
import sys
import uuid
import signal

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(Path(__file__).parent))
from deployment import Context
from ner_test_support import (
    IMAGE_TAGS, check_internal_network, container_states, docker,
    identities, image_id, interruption, offline_probe, wait_ready, write_report,
)


def run(analyzer_image, triton_image=None, report_path=None):
    context = Context(stack="litellm-presidio", env_file=str(ROOT / ".env.example"))
    images = {"presidio-analyzer": image_id(analyzer_image),
              "ner-triton": image_id(triton_image or IMAGE_TAGS["ner-triton"])}
    nginx_image = context.model["services"]["nginx"]["image"]
    prefix = "ru-ner-smoke-" + uuid.uuid4().hex[:12]
    network, nginx, triton, analyzer = prefix, prefix + "-nginx", prefix + "-triton", prefix + "-analyzer"
    report = {"status": "running", "images": images, "phase": "artifact"}
    try:
        report["model"] = identities(images)
        offline_probe(images["presidio-analyzer"], "python", "verify_cpu_runtime.py")
        docker("network", "create", "--internal", network, stdout=subprocess.DEVNULL)
        check_internal_network(network)
        docker("run", "-d", "--name", nginx, "--network", network, "--network-alias", "nginx",
               "-v", f"{ROOT / 'nginx/conf.d/triton.conf'}:/etc/nginx/conf.d/triton.conf:ro",
               nginx_image, stdout=subprocess.DEVNULL)
        docker("exec", nginx, "nginx", "-t")
        docker("run", "-d", "--name", triton, "--network", network, "--network-alias", "ner-triton",
               images["ner-triton"], stdout=subprocess.DEVNULL)
        report["phase"] = "readiness"

        def ready():
            result = subprocess.run(["docker", "exec", triton, "python3", "/opt/ru-ner/verify_triton_service.py"],
                                    capture_output=True, timeout=10)
            # Backend health can precede Docker DNS propagation to Nginx.
            route = subprocess.run(["docker", "exec", nginx, "wget", "-qO-", "-T", "5",
                "http://127.0.0.1:8000/v2/models/tiny2/versions/1/ready"], capture_output=True, timeout=10)
            if result.returncode == 0:
                assert json.loads(result.stdout) == report["model"], "Live Triton identity differs from built artifact"
            return result.returncode == 0 and route.returncode == 0

        wait_ready(ready, context.start_timeout)
        report["phase"] = "inference"
        result = docker("run", "--rm", "--name", analyzer, "--network", network,
                        images["presidio-analyzer"], "python", "real_model_smoke.py",
                        timeout=180, capture_output=True, text=True)
        report["inference"] = json.loads(result.stdout.strip().splitlines()[-1])
        assert report["inference"]["status"] == "ok"
        report.update(status="passed", phase="completed", internal_network=True)
    except BaseException as exc:
        report.update(status="failed", error_type=type(exc).__name__)
        existing = subprocess.run(["docker", "ps", "-aq", "--filter", f"name=^{prefix}-"],
                                  capture_output=True, text=True, timeout=30)
        report["containers"] = container_states(existing.stdout.split())
        raise
    finally:
        subprocess.run(["docker", "rm", "-f", nginx, triton, analyzer], capture_output=True, timeout=30)
        subprocess.run(["docker", "network", "rm", network], capture_output=True, timeout=30)
        containers = subprocess.run(["docker", "ps", "-aq", "--filter", f"name=^{prefix}-"],
                                    capture_output=True, text=True, timeout=30)
        networks = subprocess.run(["docker", "network", "ls", "--filter", f"name=^{network}$", "--format", "{{.Name}}"],
                                  capture_output=True, text=True, timeout=30)
        report["cleanup_ok"] = (containers.returncode == networks.returncode == 0
                                and not containers.stdout.strip() and not networks.stdout.strip())
        if not report["cleanup_ok"]:
            report["status"] = "failed"
        write_report(report_path, report)
    if not report["cleanup_ok"]:
        raise RuntimeError("Model test network cleanup failed")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--analyzer-image", required=True)
    args = parser.parse_args()
    signal.signal(signal.SIGTERM, interruption)
    run(args.analyzer_image)
