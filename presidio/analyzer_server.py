"""Presidio Analyzer REST server for ru-llm-proxy."""

import asyncio
import json
import logging
import os
import time
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from presidio_analyzer import AnalyzerEngine
from presidio_analyzer.nlp_engine import NlpEngineProvider

from capacity import CapacityRejected, build_limiter_from_env
from recognizers import ALL_RECOGNIZERS
from result_merging import MergeDecision, merge_results
from ner import (
    MODEL_ID,
    MODEL_REVISION,
    HuggingFaceNERRecognizer,
    NERBackendError,
    NERInferenceTelemetry,
    NERProcessingError,
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

try:
    from prometheus_client import (
        CONTENT_TYPE_LATEST,
        Counter,
        Histogram,
        generate_latest,
    )
except Exception:  # pragma: no cover - dependency is present in the Docker image.
    CONTENT_TYPE_LATEST = "text/plain; version=0.0.4"
    Counter = None
    Histogram = None
    generate_latest = None


class _NoopMetric:
    """Fallback metric used when prometheus_client is unavailable."""

    def labels(self, *args, **kwargs):
        return self

    def inc(self, amount: float = 1):
        return None

    def observe(self, amount: float):
        return None


def _build_metric(factory, *args, **kwargs):
    """Create a Prometheus metric or a no-op replacement."""
    if factory is None:
        return _NoopMetric()
    try:
        return factory(*args, **kwargs)
    except ValueError:
        logger.warning(
            "Prometheus metric already registered, using no-op for %s",
            args[0],
        )
        return _NoopMetric()


ANALYZER_REQUESTS = _build_metric(
    Counter,
    "ru_presidio_analyzer_requests",
    "Presidio Analyzer requests by safe outcome.",
    ["outcome"],
)
ANALYZER_LATENCY = _build_metric(
    Histogram,
    "ru_presidio_analyzer_latency_seconds",
    "Presidio Analyzer request latency by safe outcome.",
    ["outcome"],
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30),
)
ANALYZER_ENTITIES_DETECTED = _build_metric(
    Counter,
    "ru_presidio_analyzer_entities_detected",
    "Presidio Analyzer detected entities by entity type.",
    ["entity_type"],
)
ANALYZER_CAPACITY_REJECTIONS = _build_metric(
    Counter,
    "ru_presidio_analyzer_capacity_rejections",
    "Presidio Analyzer capacity rejections by bounded reason.",
    ["reason"],
)
ANALYZER_FAILURES = _build_metric(
    Counter,
    "ru_presidio_analyzer_failures",
    "Presidio Analyzer failures by bounded reason.",
    ["reason"],
)
ANALYZER_NER_FAILURES = _build_metric(
    Counter,
    "ru_presidio_analyzer_ner_failures",
    "Required NER backend failures by bounded phase and class.",
    ["phase", "failure_class"],
)
ANALYZER_NER_INFERENCE = _build_metric(
    Counter,
    "ru_presidio_analyzer_ner_inference",
    "Required NER inference attempts by bounded outcome.",
    ["outcome"],
)
ANALYZER_NER_INFERENCE_LATENCY = _build_metric(
    Histogram,
    "ru_presidio_analyzer_ner_inference_duration_seconds",
    "Required NER inference duration by bounded outcome.",
    ["outcome"],
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30),
)
ANALYZER_NER_WINDOWS = _build_metric(
    Histogram,
    "ru_presidio_analyzer_ner_windows_processed",
    "Fully processed NER token windows by bounded outcome.",
    ["outcome"],
    buckets=(0, 1, 2, 4, 8, 16, 32, 64, 128),
)
ANALYZER_MERGE_DECISIONS = _build_metric(
    Counter,
    "ru_presidio_analyzer_merge_decisions",
    "Presidio Analyzer overlap decisions by bounded reason and source.",
    ["reason", "winner_source", "loser_source"],
)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    """Load NER model on startup."""
    _safe_log(
        logging.INFO,
        "presidio_ner_startup_begin",
        model=MODEL_ID,
        revision=MODEL_REVISION,
    )
    try:
        ner_recognizer.load_model()
        _safe_log(
            logging.INFO,
            "presidio_ner_startup_ready",
            model=MODEL_ID,
            revision=MODEL_REVISION,
            state=ner_recognizer.state(),
            warmed_up=ner_recognizer.is_warmed_up(),
        )
    except Exception:
        phase = ner_recognizer.failure_phase() or "startup"
        failure_class = ner_recognizer.failure_class() or "startup_failed"
        ANALYZER_FAILURES.labels(reason="ner_startup_failed").inc()
        ANALYZER_NER_FAILURES.labels(
            phase=phase,
            failure_class=failure_class,
        ).inc()
        _safe_log(
            logging.CRITICAL,
            "presidio_ner_startup_failed",
            phase=phase,
            failure_class=failure_class,
            model=MODEL_ID,
            revision=MODEL_REVISION,
        )
        raise RuntimeError(
            "Hugging Face NER is required but failed to load. "
            "Refusing to start Presidio Analyzer."
        ) from None
    yield


app = FastAPI(title="Presidio Analyzer (ru-llm-proxy)", lifespan=lifespan)
# Configure NLP engine with Russian spaCy model
nlp_engine_provider = NlpEngineProvider(
    nlp_configuration={
        "nlp_engine_name": "spacy",
        "models": [{"lang_code": "ru", "model_name": "ru_core_news_sm"}],
    }
)
nlp_engine = nlp_engine_provider.create_engine()
analyzer = AnalyzerEngine(nlp_engine=nlp_engine)

# Register custom Russian regex recognizers
for recognizer_cls in ALL_RECOGNIZERS:
    recognizer = recognizer_cls()
    analyzer.registry.add_recognizer(recognizer)
    logger.info(f"Registered recognizer: {recognizer.name}")

# Initialize the one fixed Hugging Face NER backend.
ner_recognizer = HuggingFaceNERRecognizer()
capacity_limiter = build_limiter_from_env()


class AnalyzeRequest(BaseModel):
    text: str
    language: str = "ru"
    entities: list[str] | None = None
    score_threshold: float = 0.35


class AnalyzeResponse(BaseModel):
    text: str
    entities: list[dict]


@app.get("/api/v1/health")
async def health():
    ner_status = "loaded" if ner_recognizer.is_loaded() else "not_loaded"
    ner_warmed_up = ner_recognizer.is_warmed_up()
    ner_state = ner_recognizer.state()
    payload = {
        "status": "ok" if ner_state == "ready" else "unhealthy",
        "ner": ner_status,
        "ner_state": ner_state,
        "ner_warmed_up": ner_warmed_up,
        "ner_required": True,
        "ner_backend": "huggingface_transformers",
        "ner_model": MODEL_ID,
        "ner_revision": MODEL_REVISION,
        "capacity": capacity_limiter.snapshot(),
    }
    if ner_recognizer.failure_phase() is not None:
        payload["ner_failure_phase"] = ner_recognizer.failure_phase()
    if ner_recognizer.failure_class() is not None:
        payload["ner_failure_class"] = ner_recognizer.failure_class()
    if ner_state != "ready":
        return JSONResponse(status_code=503, content=payload)
    return payload


@app.get("/metrics")
async def metrics():
    if generate_latest is None:
        return Response(status_code=503, content="prometheus_client unavailable\n")
    return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)


@app.post("/api/v1/analyze", response_model=AnalyzeResponse)
async def analyze(request: AnalyzeRequest):
    started_at = time.perf_counter()
    try:
        slot = await capacity_limiter.acquire()
    except CapacityRejected as e:
        _emit_analyzer_telemetry(
            request=request,
            started_at=started_at,
            outcome="overload",
            failure_reason=e.reason,
        )
        raise HTTPException(
            status_code=e.status_code,
            detail={
                "code": "analyzer_overloaded",
                "reason": e.reason,
                "message": str(e),
            },
        ) from e

    try:
        async with slot:
            response = await _run_blocking_analyze(request)
    except asyncio.CancelledError:
        _emit_analyzer_telemetry(
            request=request,
            started_at=started_at,
            outcome="timeout_or_cancelled",
            failure_reason="cancelled",
        )
        raise
    except NERBackendError as e:
        _emit_analyzer_telemetry(
            request=request,
            started_at=started_at,
            outcome="analyzer_error",
            failure_reason="required_ner_unavailable",
            ner_failure_phase=e.phase,
            ner_failure_class=e.failure_class,
        )
        raise HTTPException(
            status_code=503,
            detail={
                "code": "required_ner_unavailable",
                "phase": e.phase,
                "failure_class": e.failure_class,
            },
        ) from None
    except Exception as e:
        _emit_analyzer_telemetry(
            request=request,
            started_at=started_at,
            outcome="analyzer_error",
            failure_reason="internal_error",
        )
        raise

    entity_counts = _entity_counts_from_response(response)
    _emit_analyzer_telemetry(
        request=request,
        started_at=started_at,
        outcome="success" if entity_counts else "no_entities",
        entity_counts=entity_counts,
    )
    return response


async def _run_blocking_analyze(request: AnalyzeRequest) -> AnalyzeResponse:
    """Run blocking analyzer work without releasing capacity on cancellation."""
    task = asyncio.create_task(asyncio.to_thread(_analyze_sync, request))
    cancelled = False

    while True:
        try:
            response = await asyncio.shield(task)
        except asyncio.CancelledError:
            cancelled = True
            if task.done():
                break
            continue
        except Exception as e:
            if cancelled:
                logger.error(
                    "Analyzer work failed after request cancellation: error_type=%s",
                    type(e).__name__,
                )
                raise asyncio.CancelledError from None
            raise

        if cancelled:
            raise asyncio.CancelledError
        return response

    if task.done():
        try:
            task.result()
        except Exception as e:
            logger.error(
                "Analyzer work failed after request cancellation: error_type=%s",
                type(e).__name__,
            )
    raise asyncio.CancelledError


def _analyze_sync(request: AnalyzeRequest) -> AnalyzeResponse:
    ner_telemetry: NERInferenceTelemetry | None = None
    readiness_started_at = time.perf_counter()

    def capture_ner_telemetry(telemetry: NERInferenceTelemetry) -> None:
        nonlocal ner_telemetry
        ner_telemetry = telemetry

    # The required model is a service-wide readiness dependency. Never serve a
    # deterministic-only degraded mode after NER has failed.
    try:
        ner_recognizer.require_ready()

        # 1. Run Presidio with deterministic recognizers.
        results = analyzer.analyze(
            text=request.text,
            language=request.language,
            entities=request.entities,
            score_threshold=request.score_threshold,
        )

        # 2. Run or explicitly skip the required Hugging Face NER.
        ner_results = ner_recognizer.analyze(
            request.text,
            score_threshold=request.score_threshold,
            entities=request.entities,
            telemetry_callback=capture_ner_telemetry,
        )
        results.extend(ner_results)

        # A concurrent inference may have latched the shared backend as failed.
        ner_recognizer.require_ready()

        # 3. Merge overlaps using source-specific confidence contracts.
        merge_outcome = merge_results(
            request.text,
            results,
            requested_entities=request.entities,
            score_threshold=request.score_threshold,
        )
        _record_merge_metrics(merge_outcome.decisions)
        results = merge_outcome.results

        entities = [
            {
                "entity_type": r.entity_type,
                "start": r.start,
                "end": r.end,
                "score": r.score,
                "text": request.text[r.start : r.end],
            }
            for r in results
        ]
        return AnalyzeResponse(text=request.text, entities=entities)
    except NERBackendError as error:
        if ner_telemetry is None or ner_telemetry.outcome in {"success", "skipped"}:
            ner_telemetry = NERInferenceTelemetry(
                outcome=(
                    "failure"
                    if isinstance(error, NERProcessingError)
                    else "unavailable"
                ),
                duration_seconds=(
                    ner_telemetry.duration_seconds
                    if ner_telemetry is not None
                    else max(0.0, time.perf_counter() - readiness_started_at)
                ),
                windows_processed=max(
                    error.windows_processed,
                    ner_telemetry.windows_processed
                    if ner_telemetry is not None
                    else 0,
                ),
                failure_phase=error.phase,
                failure_class=error.failure_class,
            )
        raise
    finally:
        if ner_telemetry is not None:
            _record_ner_inference(ner_telemetry)


def _record_merge_metrics(decisions: tuple[MergeDecision, ...]) -> None:
    """Record bounded merge diagnostics without entity values or offsets."""
    for decision in decisions:
        ANALYZER_MERGE_DECISIONS.labels(
            reason=decision.reason,
            winner_source=decision.winner_source,
            loser_source=decision.loser_source,
        ).inc()


def _record_ner_inference(telemetry: NERInferenceTelemetry) -> None:
    """Record bounded NER telemetry without request-derived fields."""
    try:
        ANALYZER_NER_INFERENCE.labels(outcome=telemetry.outcome).inc()
        ANALYZER_NER_INFERENCE_LATENCY.labels(
            outcome=telemetry.outcome,
        ).observe(telemetry.duration_seconds)
        ANALYZER_NER_WINDOWS.labels(outcome=telemetry.outcome).observe(
            telemetry.windows_processed
        )
        if telemetry.outcome in {"failure", "unavailable"}:
            ANALYZER_NER_FAILURES.labels(
                phase=telemetry.failure_phase or "readiness",
                failure_class=telemetry.failure_class or "unexpected_failure",
            ).inc()
    except Exception as exc:
        logger.warning(
            "NER metric recording failed: error_type=%s",
            type(exc).__name__,
        )

    fields = {
        "outcome": telemetry.outcome,
        "duration_ms": round(telemetry.duration_seconds * 1000, 3),
        "windows_processed": telemetry.windows_processed,
    }
    if telemetry.failure_phase is not None:
        fields["failure_phase"] = telemetry.failure_phase
    if telemetry.failure_class is not None:
        fields["failure_class"] = telemetry.failure_class
    _safe_log(logging.INFO, "presidio_ner_inference", **fields)


def _safe_log(level: int, event: str, **fields) -> None:
    """Write structured Analyzer logs without request text or raw entity values."""
    try:
        logger.log(
            level,
            json.dumps(
                {"event": event, **fields},
                ensure_ascii=False,
                sort_keys=True,
            ),
        )
    except Exception as exc:
        logger.warning(
            "Structured log emission failed: event=%s error_type=%s",
            event,
            type(exc).__name__,
        )


def _entity_counts_from_response(response: AnalyzeResponse) -> dict[str, int]:
    """Return safe entity type counts without values or offsets."""
    counts: dict[str, int] = {}
    for entity in response.entities:
        entity_type = str(entity.get("entity_type") or "UNKNOWN")
        counts[entity_type] = counts.get(entity_type, 0) + 1
    return counts


def _record_analyzer_metrics(
    *,
    outcome: str,
    latency_seconds: float,
    entity_counts: dict[str, int],
    failure_reason: str | None = None,
) -> None:
    """Update low-cardinality Analyzer metrics."""
    ANALYZER_REQUESTS.labels(outcome=outcome).inc()
    ANALYZER_LATENCY.labels(outcome=outcome).observe(latency_seconds)
    for entity_type, count in entity_counts.items():
        ANALYZER_ENTITIES_DETECTED.labels(entity_type=entity_type).inc(count)
    if outcome == "overload":
        ANALYZER_CAPACITY_REJECTIONS.labels(
            reason=failure_reason or "unknown",
        ).inc()
    if outcome in {"timeout_or_cancelled", "analyzer_error"}:
        ANALYZER_FAILURES.labels(reason=failure_reason or "unknown").inc()


def _emit_analyzer_telemetry(
    *,
    request: AnalyzeRequest,
    started_at: float,
    outcome: str,
    entity_counts: dict[str, int] | None = None,
    failure_reason: str | None = None,
    ner_failure_phase: str | None = None,
    ner_failure_class: str | None = None,
) -> None:
    """Emit one safe telemetry event for an Analyzer request."""
    entity_counts = entity_counts or {}
    latency_seconds = time.perf_counter() - started_at
    _record_analyzer_metrics(
        outcome=outcome,
        latency_seconds=latency_seconds,
        entity_counts=entity_counts,
        failure_reason=failure_reason,
    )

    fields = {
        "event_id": str(uuid.uuid4()),
        "outcome": outcome,
        "latency_ms": round(latency_seconds * 1000, 3),
        "entity_count": sum(entity_counts.values()),
        "entity_counts": entity_counts,
        "language": request.language,
        "score_threshold": request.score_threshold,
        "ner": "loaded" if ner_recognizer.is_loaded() else "not_loaded",
        "ner_state": ner_recognizer.state(),
        "capacity": capacity_limiter.snapshot(),
    }
    if failure_reason is not None:
        fields["failure_reason"] = failure_reason
    if ner_failure_phase is not None:
        fields["ner_failure_phase"] = ner_failure_phase
    if ner_failure_class is not None:
        fields["ner_failure_class"] = ner_failure_class
    _safe_log(logging.INFO, "presidio_analyzer_request", **fields)


if __name__ == "__main__":
    import uvicorn

    port = int(os.getenv("PRESIDIO_ANALYZER_PORT", "5001"))
    workers = int(os.getenv("PRESIDIO_ANALYZER_WORKERS", "1"))
    uvicorn.run("analyzer_server:app", host="0.0.0.0", port=port, workers=workers)
