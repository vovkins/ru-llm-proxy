#!/usr/bin/env python3
"""Compare completed load-test runs without exposing request contents."""

from __future__ import annotations

import argparse
import json
from pathlib import Path


def format_value(value: object) -> str:
    return "-" if value is None else str(value)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("matrix_dir", type=Path)
    args = parser.parse_args()
    rows = []
    for path in sorted(args.matrix_dir.glob("*/run-summary.json")):
        payload = json.loads(path.read_text(encoding="utf-8"))
        request = payload.get("request_summary", {})
        parameters = request.get("parameters", {})
        resources = payload.get("resources", {})
        rows.append(
            {
                "run": path.parent.name,
                "profile": parameters.get("profile"),
                "input_variation": parameters.get("input_variation"),
                "analyzer_backend": parameters.get("analyzer_backend", "real"),
                "analyzer_replicas": parameters.get("analyzer_replicas"),
                "litellm_replicas": parameters.get("litellm_replicas"),
                "requests_per_second": request.get("requests_per_second"),
                "successful_requests_per_second": request.get(
                    "successful_requests_per_second"
                ),
                "failure_rate": request.get("failure_rate"),
                "success_count": request.get("success_count"),
                "failure_count": request.get("failure_count"),
                "p95_ms": request.get("latency_ms", {}).get("p95"),
                "ttft_p95_ms": request.get("ttft_ms", {}).get("p95"),
                "analyzer_peak_cpu_percent": resources.get(
                    "load-presidio-analyzer", {}
                ).get("peak_total_cpu_percent"),
                "analyzer_peak_memory_bytes": resources.get(
                    "load-presidio-analyzer", {}
                ).get("peak_total_memory_bytes"),
                "litellm_peak_cpu_percent": resources.get("load-litellm", {}).get(
                    "peak_total_cpu_percent"
                ),
                "litellm_peak_memory_bytes": resources.get("load-litellm", {}).get(
                    "peak_total_memory_bytes"
                ),
            }
        )

    (args.matrix_dir / "matrix-summary.json").write_text(
        json.dumps({"schema_version": 1, "runs": rows}, indent=2) + "\n",
        encoding="utf-8",
    )
    headers = (
        "Run",
        "Profile",
        "Input",
        "Analyzer",
        "LiteLLM",
        "RPS",
        "Useful RPS",
        "Failure rate",
        "Success",
        "Failure",
        "p95 ms",
        "TTFT p95 ms",
    )
    lines = [
        "| " + " | ".join(headers) + " |",
        "| " + " | ".join("---" for _ in headers) + " |",
    ]
    for row in rows:
        analyzer = (
            "mock"
            if row["analyzer_backend"] == "mock"
            else row["analyzer_replicas"]
        )
        values = (
            row["run"],
            row["profile"],
            row["input_variation"],
            analyzer,
            row["litellm_replicas"],
            row["requests_per_second"],
            row["successful_requests_per_second"],
            row["failure_rate"],
            row["success_count"],
            row["failure_count"],
            row["p95_ms"],
            row["ttft_p95_ms"],
        )
        lines.append("| " + " | ".join(format_value(value) for value in values) + " |")
    (args.matrix_dir / "matrix-summary.md").write_text(
        "# Сравнение нагрузочных запусков\n\n" + "\n".join(lines) + "\n",
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
