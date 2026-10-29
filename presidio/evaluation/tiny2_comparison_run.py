"""Isolated E9 HTTP study, not a deployment mode or a new Make target."""
from __future__ import annotations

import argparse
from dataclasses import asdict
import hashlib
import importlib.metadata
import json
from pathlib import Path
import subprocess
import sys
import time
import urllib.error
import urllib.request

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))


def request(url, path, payload=None, timeout=240):
    req = urllib.request.Request(url.rstrip("/") + path,
                                 data=json.dumps(payload, ensure_ascii=False).encode() if payload is not None else None,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as response:
        return json.load(response)


def serve():
    # The runtime imports /app from the selected immutable image. Only this
    # disposable research process receives the two extra internal-network routes.
    import analyzer_server as server
    from fastapi import HTTPException
    import uvicorn

    @server.app.post("/__evaluation__/ner")
    def analyze_ner(payload: dict):
        value = server.AnalyzeRequest(**payload)
        try:
            results = server.ner_recognizer.analyze(value.text, score_threshold=value.score_threshold,
                                                    entities=value.entities)
        except Exception as exc:
            raise HTTPException(503, detail={"error_type": type(exc).__name__,
                                            "phase": getattr(exc, "phase", None),
                                            "failure_class": getattr(exc, "failure_class", None)}) from None
        return {"entities": [{"entity_type": r.entity_type, "start": r.start, "end": r.end,
                               "score": r.score} for r in results]}

    @server.app.get("/__evaluation__/info")
    def info():
        names = ("presidio-analyzer", "spacy", "ru-core-news-sm", "torch", "transformers",
                 "tokenizers", "numpy", "httpx", "preshed", "cymem", "murmurhash", "srsly")
        packages = {}
        for name in names:
            try:
                packages[name] = importlib.metadata.version(name)
            except importlib.metadata.PackageNotFoundError:
                packages[name] = None
        base = Path(server.__file__).parent
        files = [base / "analyzer_server.py", base / "result_merging.py", base / "string_spans.py",
                 *sorted((base / "ner").glob("*.py")), *sorted((base / "recognizers").glob("*.py"))]
        return {"packages": packages,
                "sources_sha256": {str(p.relative_to(base)): hashlib.sha256(p.read_bytes()).hexdigest()
                                   for p in files if p.is_file()},
                "spacy_pipes": server.nlp_engine.nlp["ru"].pipe_names,
                "recognizer_runtime": server.ner_recognizer.runtime_info()}

    uvicorn.run(server.app, host="0.0.0.0", port=5001, log_level="warning", access_log=False)


def git_metadata():
    def git(*args):
        return subprocess.check_output(["git", *args], cwd=ROOT, text=True).strip()
    return {"head": git("rev-parse", "HEAD"), "dirty": bool(git("status", "--porcelain")),
            "evaluator_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}


def evaluate(args):
    from presidio.evaluation.corpus import CorpusCase, EntitySpan
    from presidio.evaluation.tiny2_comparison_data import load_cases
    from presidio.evaluation.tiny2_comparison import ComparisonCase, manifest
    if args.input_cases:
        packet = json.loads(args.input_cases.read_text())
        cases = []
        for row in packet["cases"]:
            values = dict(row["case"])
            values["expected"] = tuple(EntitySpan(**e) for e in values["expected"])
            values["tags"] = tuple(values["tags"])
            cases.append(ComparisonCase(CorpusCase(**values), row["corpus"], frozenset(row["scope"]),
                                        row["complete_annotation"], row["origin"]))
        sources, git = packet["source"], packet["git"]
        if hashlib.sha256(json.dumps(manifest(cases), sort_keys=True).encode()).hexdigest() != sources["manifest_sha256"]:
            raise ValueError("Input case packet differs from the frozen corpus manifest")
    else:
        cases, sources = load_cases(ROOT, args.public_data)
        git = git_metadata()
    result = {"system": args.system, "mode": args.mode, "source": sources, "git": git,
              "runtime": request(args.url, "/__evaluation__/info"),
              "health_before": request(args.url, "/api/v1/health"), "results": {}}
    if result["health_before"].get("ner_state") != "ready":
        raise RuntimeError("Study model is not ready")
    result["manifest"] = manifest(cases)
    if args.resume and args.output.exists():
        previous = json.loads(args.output.read_text())
        if (previous["source"] != sources or previous["manifest"] != result["manifest"]
                or previous["system"] != args.system or previous["mode"] != args.mode
                or any(previous["runtime"][key] != result["runtime"][key]
                       for key in ("packages", "sources_sha256"))):
            raise RuntimeError("Resume inputs/runtime differ from the saved checkpoint")
        result["results"] = previous["results"]
        result["research_process_restarts"] = previous.get("research_process_restarts", 0) + 1
    args.output.parent.mkdir(parents=True, exist_ok=True)
    for index, item in enumerate(cases, 1):
        case = item.case
        if case.case_id in result["results"]:
            continue
        payload = {"text": case.text, "score_threshold": case.score_threshold, "language": case.language}
        started = time.monotonic()
        try:
            value = request(args.url, "/api/v1/analyze" if args.mode == "analyzer" else "/__evaluation__/ner", payload)
            row = {"status": "ok", "entities": value["entities"]}
        except urllib.error.HTTPError as exc:
            row = {"status": "error", "http_status": exc.code, "error_type": type(exc).__name__}
            try:
                detail = json.load(exc).get("detail", {})
                if isinstance(detail, dict):
                    row.update({k:detail[k] for k in ("phase", "failure_class", "reason", "error_type")
                                if k in detail and (detail[k] is None or isinstance(detail[k], str))})
            except (ValueError, AttributeError):
                pass
        except (OSError, TimeoutError, ValueError, KeyError) as exc:
            row = {"status": "error", "error_type": type(exc).__name__}
        row["diagnostic_seconds"] = round(time.monotonic() - started, 6)
        result["results"][case.case_id] = row
        if index % 100 == 0 or row["status"] != "ok" or index == len(cases):
            args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
            print(f"{args.system}/{args.mode}: {index}/{len(cases)}, errors="
                  f"{sum(r['status'] != 'ok' for r in result['results'].values())}", flush=True)
        if row["status"] != "ok":
            # A poisoned reference process must not turn every subsequent case
            # into an apparent model miss. Preserve partial results and stop.
            try:
                health = request(args.url, "/api/v1/health", timeout=15)
                ready = health.get("ner_state") == "ready"
            except Exception:
                ready = False
            if not ready:
                raise RuntimeError("Study runtime became unavailable; checkpoint preserved")
    result["health_after"] = request(args.url, "/api/v1/health")
    result["complete"] = True
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("serve")
    freeze = sub.add_parser("freeze")
    freeze.add_argument("--public-data", type=Path)
    freeze.add_argument("--output", type=Path, required=True)
    run = sub.add_parser("evaluate")
    run.add_argument("--url", required=True)
    run.add_argument("--system", required=True, choices=("reference", "candidate"))
    run.add_argument("--mode", required=True, choices=("ner", "analyzer"))
    run.add_argument("--public-data", type=Path)
    run.add_argument("--output", type=Path, required=True)
    run.add_argument("--resume", action="store_true")
    run.add_argument("--input-cases", type=Path)
    args = parser.parse_args()
    if args.command == "serve":
        serve()
    elif args.command == "freeze":
        from presidio.evaluation.tiny2_comparison_data import load_cases
        cases, source = load_cases(ROOT, args.public_data)
        packet = {"source": source, "git": git_metadata(),
                  "cases": [{"case": asdict(item.case), "corpus": item.corpus, "scope": sorted(item.scope),
                             "complete_annotation": item.complete_annotation, "origin": item.origin} for item in cases]}
        args.output.write_text(json.dumps(packet, ensure_ascii=False) + "\n")
        print(json.dumps(source))
    else:
        evaluate(args)


if __name__ == "__main__":
    main()
