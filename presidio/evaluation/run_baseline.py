"""Run the migration corpus against a live Presidio Analyzer service."""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .corpus import DEFAULT_CORPUS_PATH, CorpusCase, load_corpus
from .metrics import evaluate_predictions


DEFAULT_ANALYZER_URL = "http://localhost:5001"
DEFAULT_JSON_REPORT = (
    Path(__file__).with_name("reports") / "huggingface-candidate.json"
)
DEFAULT_MARKDOWN_REPORT = (
    Path(__file__).resolve().parents[2]
    / "docs"
    / "research"
    / "ner-migration-candidate.md"
)
DEFAULT_BASELINE_REPORT = (
    Path(__file__).with_name("reports") / "deeppavlov-baseline.json"
)
QUALITY_GATE_MINIMUM = 0.95
LEGACY_RECALL_TOLERANCE = 0.02
QUALITY_GATE_ENTITY_TYPES = (
    "LOGIN",
    "PASSWORD",
    "AUTH_TOKEN",
    "SECRET_KEY",
    "CONTRACT_NUMBER",
)
LEGACY_NER_ENTITY_TYPES = ("PERSON", "LOCATION", "ORGANIZATION")


class AnalyzerCaseError(RuntimeError):
    """Request failure with a bounded code safe to write into reports."""

    def __init__(self, safe_code: str, message: str):
        super().__init__(message)
        self.safe_code = safe_code


class AnalyzerClient:
    """Minimal JSON client which never logs request or response text."""

    def __init__(self, base_url: str, *, timeout_seconds: float):
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds

    def _request(self, path: str, payload: dict[str, Any] | None = None) -> Any:
        data = None
        headers = {"Accept": "application/json"}
        method = "GET"
        if payload is not None:
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
            method = "POST"

        request = urllib.request.Request(
            f"{self.base_url}{path}",
            data=data,
            headers=headers,
            method=method,
        )
        try:
            with urllib.request.urlopen(
                request,
                timeout=self.timeout_seconds,
            ) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            raise AnalyzerCaseError(
                f"http_{exc.code}",
                f"Analyzer returned HTTP {exc.code} for {path}",
            ) from exc
        except urllib.error.URLError as exc:
            raise AnalyzerCaseError(
                "request_failed",
                f"Analyzer request failed for {path}: {exc.reason}",
            ) from exc

    def health(self) -> dict[str, Any]:
        payload = self._request("/api/v1/health")
        if not isinstance(payload, dict):
            raise RuntimeError("Analyzer health response must be an object")
        return payload

    def analyze(self, case: CorpusCase) -> list[dict[str, Any]]:
        payload = self._request(
            "/api/v1/analyze",
            {
                "text": case.text,
                "language": case.language,
                "score_threshold": case.score_threshold,
            },
        )
        if not isinstance(payload, dict) or payload.get("text") != case.text:
            raise RuntimeError(f"Analyzer returned an invalid response for {case.case_id}")
        entities = payload.get("entities")
        if not isinstance(entities, list) or any(
            not isinstance(entity, dict) for entity in entities
        ):
            raise RuntimeError(f"Analyzer returned invalid entities for {case.case_id}")
        return entities


def _git_revision() -> str:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return "unknown"
    return result.stdout.strip() or "unknown"


def _git_worktree_dirty() -> bool | None:
    try:
        result = subprocess.run(
            ["git", "status", "--porcelain", "--untracked-files=normal"],
            check=True,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return None
    return bool(result.stdout.strip())


def _validated_git_metadata(*, allow_dirty_worktree: bool) -> dict[str, Any]:
    revision = _git_revision()
    dirty = _git_worktree_dirty()
    if revision == "unknown":
        raise RuntimeError("Git revision is unavailable for the evaluation report")
    if dirty is None:
        raise RuntimeError("Git worktree state is unavailable for the evaluation report")
    if dirty and not allow_dirty_worktree:
        raise RuntimeError(
            "Git worktree has uncommitted changes; use --allow-dirty-worktree "
            "only for a diagnostic run"
        )
    return {
        "git_revision": revision,
        "git_worktree_dirty": dirty,
    }


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _container_runtime_metadata(container_name: str) -> dict[str, Any]:
    """Collect bounded package metadata from the evaluated container."""
    probe = """
import importlib.metadata
import json
import platform

packages = {}
for distribution in (
    "huggingface-hub",
    "numpy",
    "presidio-analyzer",
    "ru-core-news-sm",
    "safetensors",
    "spacy",
    "tokenizers",
    "torch",
    "transformers",
):
    try:
        packages[distribution] = importlib.metadata.version(distribution)
    except importlib.metadata.PackageNotFoundError:
        packages[distribution] = "not-installed"

import torch

print(json.dumps({
    "python": platform.python_version(),
    "packages": packages,
    "torch_cuda_build": torch.version.cuda,
    "torch_cuda_available": torch.cuda.is_available(),
}, sort_keys=True))
""".strip()
    try:
        result = subprocess.run(
            ["docker", "exec", container_name, "python", "-c", probe],
            check=True,
            capture_output=True,
            text=True,
        )
        metadata = json.loads(result.stdout)
    except (OSError, subprocess.CalledProcessError, json.JSONDecodeError) as exc:
        raise RuntimeError(
            f"failed to collect runtime metadata from container {container_name}"
        ) from exc
    if not isinstance(metadata, dict):
        raise RuntimeError("container runtime metadata must be an object")
    return metadata


def _safe_health_metadata(health: dict[str, Any]) -> dict[str, Any]:
    allowed_fields = {
        "status",
        "ner",
        "ner_backend",
        "ner_failure_class",
        "ner_failure_phase",
        "ner_model",
        "ner_required",
        "ner_revision",
        "ner_state",
        "ner_warmed_up",
    }
    return {
        field: health[field]
        for field in sorted(allowed_fields.intersection(health))
    }


def _format_metric(value: Any) -> str:
    if value is None:
        return "—"
    if isinstance(value, float):
        return f"{value:.3f}"
    return str(value)


def evaluate_quality_gate(
    metrics: dict[str, Any],
    baseline_metrics: dict[str, Any],
) -> list[dict[str, Any]]:
    """Return bounded quality checks for the migration decision."""
    checks = []

    def add_check(name: str, actual: float | None, minimum: float) -> None:
        checks.append(
            {
                "name": name,
                "actual": actual,
                "minimum": round(minimum, 6),
                "passed": actual is not None and actual >= minimum,
            }
        )

    add_check(
        "critical_coverage_recall",
        metrics["aggregate"]["critical_coverage_recall"],
        1.0,
    )
    for entity_type in QUALITY_GATE_ENTITY_TYPES:
        row = metrics["per_entity"][entity_type]
        add_check(
            f"{entity_type}.precision",
            row["precision"],
            QUALITY_GATE_MINIMUM,
        )
        add_check(
            f"{entity_type}.recall",
            row["recall"],
            QUALITY_GATE_MINIMUM,
        )

    for entity_type in LEGACY_NER_ENTITY_TYPES:
        baseline_recall = baseline_metrics["per_entity"][entity_type]["recall"]
        if baseline_recall is None:
            raise ValueError(f"baseline recall is unavailable for {entity_type}")
        add_check(
            f"{entity_type}.recall_vs_baseline",
            metrics["per_entity"][entity_type]["recall"],
            max(0.0, baseline_recall - LEGACY_RECALL_TOLERANCE),
        )
    return checks


def render_markdown_report(report: dict[str, Any]) -> str:
    """Render a value-free human-readable evaluation summary."""
    metadata = report["metadata"]
    metrics = report["metrics"]
    aggregate = metrics["aggregate"]

    lines = [
        "# Отчёт оценки NER",
        "",
        "Отчёт фиксирует качество указанной версии NER и детерминированных "
        "распознавателей на обезличенном корпусе. Исходные тексты и найденные "
        "значения в отчёт не записываются.",
        "",
        "## Воспроизводимость",
        "",
        f"- Система: `{metadata['system']}`",
        f"- Ревизия кода: `{metadata['git_revision']}`",
        "- Рабочее дерево при запуске: "
        + (
            "с незакоммиченными изменениями"
            if metadata.get("git_worktree_dirty") is True
            else (
                "чистое"
                if metadata.get("git_worktree_dirty") is False
                else "состояние не определено"
            )
        ),
        f"- SHA-256 корпуса: `{metadata['corpus_sha256']}`",
        f"- Примеров: {metadata['case_count']}",
        f"- Состояние NER: `{metadata['analyzer_health'].get('ner', 'unknown')}`",
        f"- Готовность NER: `{metadata['analyzer_health'].get('ner_state', 'unknown')}`",
        "- Прогрев NER: "
        + (
            "выполнен"
            if metadata["analyzer_health"].get("ner_warmed_up") is True
            else "не подтверждён"
        ),
        f"- Время запуска в UTC: `{metadata['generated_at']}`",
    ]
    model_sha256 = metadata.get("model_artifact_sha256")
    if model_sha256:
        lines.append(f"- SHA-256 файла весов модели: `{model_sha256}`")
        lines.append(
            "- Проверка SHA-256 при сборке: "
            + ("включена" if metadata.get("model_checksum_enforced") else "не включена")
        )
    else:
        lines.append(
            "- Контрольная сумма файла весов не передана в отчёт; текущий "
            "health API её не сообщает."
        )

    runtime = metadata.get("runtime")
    if runtime:
        lines.extend(["", "### Среда контейнера", ""])
        lines.append(f"- Python: `{runtime.get('python', 'unknown')}`")
        for package, version in runtime.get("packages", {}).items():
            lines.append(f"- `{package}`: `{version}`")
        lines.append(
            f"- Сборка CUDA в Torch: `{runtime.get('torch_cuda_build') or 'нет'}`"
        )
        lines.append(
            "- CUDA доступна во время запуска: "
            + ("да" if runtime.get("torch_cuda_available") else "нет")
        )

    lines.extend(
        [
            "",
            "## Общий результат",
            "",
            "| Показатель | Значение |",
            "|---|---:|",
            f"| Ожидаемые сущности | {aggregate['expected']} |",
            f"| Предсказания целевых типов | {aggregate['predicted']} |",
            f"| Точные совпадения | {aggregate['true_positives']} |",
            f"| Точность точных совпадений | {_format_metric(aggregate['precision'])} |",
            f"| Полнота точных совпадений | {_format_metric(aggregate['recall'])} |",
            f"| F1 точных совпадений | {_format_metric(aggregate['f1'])} |",
            f"| Покрытие с правильным типом | {_format_metric(aggregate['typed_coverage_recall'])} |",
            f"| Покрытие для маскирования любым типом | {_format_metric(aggregate['masking_coverage_recall'])} |",
            f"| Покрытие критических значений | {_format_metric(aggregate['critical_coverage_recall'])} |",
            "",
            "## Результат по типам",
            "",
            "| Тип | Ожидалось | Предсказано | Точное P | Точное R | Покрытие типом | Покрытие маской |",
            "|---|---:|---:|---:|---:|---:|---:|",
        ]
    )
    for entity_type, row in metrics["per_entity"].items():
        lines.append(
            f"| `{entity_type}` | {row['expected']} | {row['predicted']} | "
            f"{_format_metric(row['precision'])} | {_format_metric(row['recall'])} | "
            f"{_format_metric(row['typed_coverage_recall'])} | "
            f"{_format_metric(row['masking_coverage_recall'])} |"
        )

    lines.extend(
        [
            "",
            "## Критические пропуски",
            "",
        ]
    )
    critical_misses = metrics["critical_misses"]
    if critical_misses:
        lines.extend(
            f"- `{miss['case_id']}`: `{miss['entity_type']}`"
            for miss in critical_misses
        )
    else:
        lines.append("Критических пропусков нет.")

    lines.extend(["", "## Ошибки обработки примеров", ""])
    case_errors = report.get("case_errors", [])
    if case_errors:
        lines.extend(
            f"- `{error['case_id']}`: `{error['error_type']}`"
            for error in case_errors
        )
    else:
        lines.append("Все примеры обработаны Analyzer без ошибок.")

    quality_gate = report.get("quality_gate", [])
    if quality_gate:
        lines.extend(
            [
                "",
                "## Порог качества",
                "",
                "| Проверка | Фактически | Минимум | Результат |",
                "|---|---:|---:|---|",
            ]
        )
        for check in quality_gate:
            lines.append(
                f"| `{check['name']}` | {_format_metric(check['actual'])} | "
                f"{_format_metric(check['minimum'])} | "
                f"{'пройдено' if check['passed'] else 'не пройдено'} |"
            )

    lines.extend(
        [
            "",
            "## Интерпретация",
            "",
            "Точное совпадение требует одинакового типа и одинаковых границ. "
            "Покрытие правильным типом допускает более широкий интервал. "
            "Покрытие для маскирования учитывает любой результат Analyzer, "
            "полностью закрывающий ожидаемое чувствительное значение.",
            "",
            "Для решения о миграции этот результат сравнивается с историческими "
            "исходными показателями на том же корпусе и с порогами качества "
            "из задачи #67.",
            "",
        ]
    )
    return "\n".join(lines)


def _write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def run(args: argparse.Namespace) -> dict[str, Any]:
    git_metadata = _validated_git_metadata(
        allow_dirty_worktree=args.allow_dirty_worktree,
    )
    corpus_path = Path(args.corpus).resolve()
    cases = load_corpus(corpus_path)
    client = AnalyzerClient(args.analyzer_url, timeout_seconds=args.timeout)
    health = client.health()
    if not args.allow_degraded and (
        health.get("status") != "ok"
        or health.get("ner") != "loaded"
        or health.get("ner_state") != "ready"
        or health.get("ner_warmed_up") is not True
    ):
        raise RuntimeError("Analyzer must be healthy with warmed-up NER for baseline run")

    predictions_by_case: dict[str, list[dict[str, Any]]] = {}
    case_errors: list[dict[str, str]] = []
    for index, case in enumerate(cases, start=1):
        print(f"[{index}/{len(cases)}] {case.case_id}", file=sys.stderr)
        try:
            predictions_by_case[case.case_id] = client.analyze(case)
        except RuntimeError as exc:
            predictions_by_case[case.case_id] = []
            case_errors.append(
                {
                    "case_id": case.case_id,
                    "error_type": (
                        exc.safe_code
                        if isinstance(exc, AnalyzerCaseError)
                        else type(exc).__name__
                    ),
                }
            )
            print(
                f"[{index}/{len(cases)}] {case.case_id}: analyzer error",
                file=sys.stderr,
            )

    metrics = evaluate_predictions(cases, predictions_by_case)
    baseline_report = json.loads(
        Path(args.baseline_report).read_text(encoding="utf-8")
    )
    report = {
        "metadata": {
            "system": args.system,
            **git_metadata,
            "corpus_sha256": _file_sha256(corpus_path),
            "case_count": len(cases),
            "generated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
            "analyzer_health": _safe_health_metadata(health),
            "model_artifact_sha256": args.model_artifact_sha256 or None,
            "model_checksum_enforced": args.model_checksum_enforced,
            "runtime": (
                _container_runtime_metadata(args.runtime_container)
                if args.runtime_container
                else None
            ),
        },
        "metrics": metrics,
        "quality_gate": evaluate_quality_gate(
            metrics,
            baseline_report["metrics"],
        ),
        "case_errors": case_errors,
    }

    json_output = Path(args.json_output)
    _write_text(
        json_output,
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
    )
    markdown = render_markdown_report(report)
    _write_text(Path(args.markdown_output), markdown)
    print(markdown)
    return report


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Evaluate a live Analyzer on the sanitized NER migration corpus.",
    )
    parser.add_argument("--corpus", default=str(DEFAULT_CORPUS_PATH))
    parser.add_argument(
        "--baseline-report",
        default=str(DEFAULT_BASELINE_REPORT),
    )
    parser.add_argument(
        "--analyzer-url",
        default=DEFAULT_ANALYZER_URL,
    )
    parser.add_argument("--timeout", type=float, default=60.0)
    parser.add_argument(
        "--system",
        default="fef2 secret-detection BERT + current recognizers",
    )
    parser.add_argument("--runtime-container", default="")
    parser.add_argument("--model-artifact-sha256", default="")
    parser.add_argument("--model-checksum-enforced", action="store_true")
    parser.add_argument("--json-output", default=str(DEFAULT_JSON_REPORT))
    parser.add_argument("--markdown-output", default=str(DEFAULT_MARKDOWN_REPORT))
    parser.add_argument(
        "--allow-degraded",
        action="store_true",
        help="Allow an explicit diagnostic run when required NER is unavailable.",
    )
    parser.add_argument(
        "--allow-case-errors",
        action="store_true",
        help="Write a diagnostic report and exit successfully when individual cases fail.",
    )
    parser.add_argument(
        "--allow-dirty-worktree",
        action="store_true",
        help="Allow a diagnostic report from a Git worktree with uncommitted changes.",
    )
    return parser


def main() -> None:
    args = build_parser().parse_args()
    try:
        report = run(args)
    except (OSError, ValueError, RuntimeError) as exc:
        print(f"evaluation failed: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
    if report["case_errors"] and not args.allow_case_errors:
        print(
            "evaluation failed: one or more corpus cases were not processed",
            file=sys.stderr,
        )
        raise SystemExit(2)
    failed_checks = [
        check for check in report.get("quality_gate", []) if not check["passed"]
    ]
    if failed_checks:
        names = ", ".join(check["name"] for check in failed_checks)
        print(f"evaluation failed quality gate: {names}", file=sys.stderr)
        raise SystemExit(3)


if __name__ == "__main__":
    main()
