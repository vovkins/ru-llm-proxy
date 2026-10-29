"""Implementation of existing Make NER targets; no credentials or live LLMs."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).parent))
from ner_test_support import IMAGE_TAGS, docker, identities, image_id, interruption, run_checks, run_child, write_report


def build_images(directory, services, analyzer_image):
    override = Path(directory) / "build.json"
    tags = dict(IMAGE_TAGS, **{"presidio-analyzer": analyzer_image})
    override.write_text(json.dumps({"services": {name: {"image": tags[name]} for name in services}}))
    # Never load the administrator's .env or mutate its Compose project.
    env = dict(os.environ, CODEX_LB_POSTGRES_PASSWORD="build-only", CODEX_LB_API_KEY="build-only")
    docker("compose", "--project-name", "ru-ner-test-build", "--env-file", str(ROOT / ".env.example"),
           "-f", str(ROOT / "docker-compose.yml"), "-f", str(ROOT / "docker-compose.codex-lb.yml"),
           "-f", str(override), "build", *services, timeout=2400, cwd=ROOT, env=env)


def run(suite, build_model=False, analyzer_image=IMAGE_TAGS["presidio-analyzer"], report_dir=None):
    from model_smoke import run as model_smoke
    from deployment_topology_gate import Gate
    from presidio.model_contract import reject_removed_settings

    reject_removed_settings(os.environ)
    report = {"status": "running", "suite": suite, "phase": "images"}
    destination = Path(report_dir) if report_dir else None
    try:
        with tempfile.TemporaryDirectory(prefix="ru-ner-integration-") as directory:
            if build_model:
                build_images(directory, ["presidio-analyzer", "ner-triton"], analyzer_image)
            images = {name: image_id(analyzer_image if name == "presidio-analyzer" else IMAGE_TAGS[name])
                      for name in ("presidio-analyzer", "ner-triton")}
            report.update(images=dict(images), model=identities(images))
            if suite in {"model", "all"}:
                report["phase"] = "real-model"
                model_smoke(images["presidio-analyzer"], images["ner-triton"],
                            destination / "model.json" if destination else None)
            if suite in {"proxy", "all"}:
                report["phase"] = "platform-images"
                build_images(directory, ["litellm", "codex-lb"], analyzer_image)
                images.update({name: image_id(IMAGE_TAGS[name]) for name in ("litellm", "codex-lb")})
                report["images"] = dict(images)
                env = dict(os.environ, ANALYZER_IMAGE=images["presidio-analyzer"],
                           NER_TEST_TRITON_IMAGE=images["ner-triton"], LITELLM_IMAGE=images["litellm"])
                if destination:
                    env["NER_TEST_REPORT_DIR"] = str(destination.resolve())
                gate = Gate(directory, images=images)

                def proxy_flow():
                    run_child(["bash", str(ROOT / "tests/e2e/test_ner_proxy_flow.sh")], env=env, cwd=ROOT)

                def topologies():
                    try:
                        gate.run()
                    finally:
                        write_report(destination / "topologies.json" if destination else None, gate.result)

                report["phase"] = "independent-checks"
                report["checks"] = []
                run_checks([("proxy-flow", proxy_flow), ("topologies", topologies)], report["checks"])
            report.update(status="passed", phase="completed")
    except BaseException as exc:
        report.update(status="failed", error_type=type(exc).__name__)
        raise
    finally:
        write_report(destination / f"integration-{suite}.json" if destination else None, report)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("suite", choices=["model", "proxy", "all"])
    parser.add_argument("--build-model", action="store_true")
    parser.add_argument("--analyzer-image", default=IMAGE_TAGS["presidio-analyzer"])
    parser.add_argument("--report-dir", type=Path)
    args = parser.parse_args()
    signal.signal(signal.SIGTERM, interruption)
    run(args.suite, args.build_model, args.analyzer_image, args.report_dir)


if __name__ == "__main__":
    main()
