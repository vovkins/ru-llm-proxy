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
from ner import DeepPavlovRecognizer, should_run_ner

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


def _env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


DEEPPAVLOV_NER_REQUIRED = _env_bool("DEEPPAVLOV_NER_REQUIRED", True)
ner_startup_error: str | None = None


@asynccontextmanager
async def lifespan(_app: FastAPI):
    """Load NER model on startup."""
    global ner_startup_error
    logger.info("Loading DeepPavlov NER model...")
    try:
        dp_recognizer.load_model()
        ner_startup_error = None
        logger.info("DeepPavlov NER model loaded")
    except Exception as e:
        ner_startup_error = type(e).__name__
        ANALYZER_FAILURES.labels(reason="ner_startup_failed").inc()
        logger.exception(
            "CRITICAL: Failed to load required DeepPavlov NER model; "
            "structured entities PERSON/LOCATION/ORGANIZATION would be degraded."
        )
        if DEEPPAVLOV_NER_REQUIRED:
            raise RuntimeError(
                "DeepPavlov NER is required but failed to load. "
                "Refusing to start Presidio Analyzer in degraded mode."
            ) from e
        logger.error(
            "DeepPavlov NER is not loaded; starting in explicit degraded mode "
            "because DEEPPAVLOV_NER_REQUIRED=false. Regex recognizers remain available."
        )
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

# Initialize DeepPavlov NER
dp_recognizer = DeepPavlovRecognizer()
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
    ner_status = "loaded" if dp_recognizer.is_loaded() else "not_loaded"
    status = "ok" if ner_status == "loaded" else "degraded"
    payload = {
        "status": status,
        "ner": ner_status,
        "ner_required": DEEPPAVLOV_NER_REQUIRED,
        "capacity": capacity_limiter.snapshot(),
    }
    if ner_startup_error:
        payload["ner_error"] = ner_startup_error
    if DEEPPAVLOV_NER_REQUIRED and ner_status != "loaded":
        payload["status"] = "unhealthy"
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
    except Exception as e:
        _emit_analyzer_telemetry(
            request=request,
            started_at=started_at,
            outcome="analyzer_error",
            failure_reason=type(e).__name__,
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
    # 1. Run Presidio with regex recognizers
    results = analyzer.analyze(
        text=request.text,
        language=request.language,
        entities=request.entities,
        score_threshold=request.score_threshold,
    )

    # 2. Run DeepPavlov NER and merge results when requested.
    if dp_recognizer.is_loaded() and should_run_ner(
        request.entities,
        request.score_threshold,
    ):
        try:
            ner_results = dp_recognizer.analyze(
                request.text,
                score_threshold=request.score_threshold,
                entities=request.entities,
            )
            results.extend(ner_results)
        except Exception as e:
            logger.error("NER analysis error: error_type=%s", type(e).__name__)

    # 3. Deduplicate overlapping entities (keep higher score)
    results = _deduplicate(results)

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


def _deduplicate(results):
    """Remove overlapping entities, keeping higher-score ones."""
    if not results:
        return results

    # Sort by score descending
    results.sort(key=lambda r: r.score, reverse=True)

    kept = []
    for result in results:
        overlaps = False
        for existing in kept:
            if (result.start >= existing.start and result.start < existing.end) or \
               (result.end > existing.start and result.end <= existing.end) or \
               (result.start <= existing.start and result.end >= existing.end):
                overlaps = True
                break
        if not overlaps:
            kept.append(result)

    return sorted(kept, key=lambda r: r.start)


def _safe_log(level: int, event: str, **fields) -> None:
    """Write structured Analyzer logs without request text or raw entity values."""
    logger.log(
        level,
        json.dumps(
            {"event": event, **fields},
            ensure_ascii=False,
            sort_keys=True,
        ),
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
        "ner": "loaded" if dp_recognizer.is_loaded() else "not_loaded",
        "capacity": capacity_limiter.snapshot(),
    }
    if failure_reason is not None:
        fields["failure_reason"] = failure_reason
    _safe_log(logging.INFO, "presidio_analyzer_request", **fields)


if __name__ == "__main__":
    import uvicorn

    port = int(os.getenv("PRESIDIO_ANALYZER_PORT", "5001"))
    workers = int(os.getenv("PRESIDIO_ANALYZER_WORKERS", "1"))
    uvicorn.run("analyzer_server:app", host="0.0.0.0", port=port, workers=workers)
