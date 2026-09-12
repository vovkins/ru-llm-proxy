"""Presidio Analyzer REST server for ru-llm-proxy."""

import asyncio
import hashlib
import importlib.metadata
import json
import logging
import os
import threading
import time
import uuid
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager, suppress
from contextvars import ContextVar
from pathlib import Path

from fastapi import FastAPI, Header, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from presidio_analyzer import AnalyzerEngine, RecognizerResult
from presidio_analyzer.nlp_engine import NlpEngineProvider

from capacity import CapacityRejected, build_limiter_from_env
from recognizers import ALL_RECOGNIZERS
from result_merging import MergeDecision, merge_results
from text_chunking import TextChunk, plan_text_chunks
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
    buckets=(0, 1, 2, 4, 8, 16, 32, 64, 128, 256, 512, 1024, 2048, 4096),
)
ANALYZER_NER_INPUT_TOKENS = _build_metric(
    Histogram,
    "ru_presidio_analyzer_ner_input_tokens",
    "NER input tokens by bounded outcome.",
    ["outcome"],
    buckets=(0, 128, 384, 1024, 4096, 8192, 50000, 128000, 256000, 512000, 1000000),
)
ANALYZER_QUEUE_WAIT = _build_metric(
    Histogram,
    "ru_presidio_analyzer_queue_wait_seconds",
    "Time waiting for local Analyzer capacity by bounded outcome.",
    ["outcome"],
    buckets=(0, 0.001, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30),
)
ANALYZER_PHASE_LATENCY = _build_metric(
    Histogram,
    "ru_presidio_analyzer_phase_duration_seconds",
    "Analyzer computation phase duration by bounded phase and outcome.",
    ["phase", "outcome"],
    buckets=(0.001, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 60),
)
ANALYZER_INPUT_CHARACTERS = _build_metric(
    Histogram,
    "ru_presidio_analyzer_input_characters",
    "Analyzer input size in characters.",
    buckets=(0, 1024, 8192, 50000, 128000, 256000, 512000, 1000000, 2000000, 4000000),
)
ANALYZER_MERGE_DECISIONS = _build_metric(
    Counter,
    "ru_presidio_analyzer_merge_decisions",
    "Presidio Analyzer overlap decisions by bounded reason and source.",
    ["reason", "winner_source", "loser_source"],
)
ANALYZER_TEXT_CHUNKS = _build_metric(
    Histogram,
    "ru_presidio_analyzer_text_chunks",
    "Outer text chunks planned for one Analyzer request.",
    buckets=(1, 2, 4, 8, 16, 32, 64, 128),
)
ANALYZER_TEXT_CHUNK_CHARACTERS = _build_metric(
    Histogram,
    "ru_presidio_analyzer_text_chunk_characters",
    "Characters in one bounded outer Analyzer text chunk.",
    buckets=(1024, 8192, 32000, 64000, 96000, 128000),
)

ANALYZER_REQUEST_ID_HEADER = "X-Ru-LLM-Request-ID"
ANALYZER_TEXT_FIELD_INDEX_HEADER = "X-Ru-LLM-Text-Field-Index"
ANALYSIS_SIGNATURE_SCHEMA = "ru-llm-proxy-analyzer-v1"
ANALYSIS_SIGNATURE_ENV_NAMES = (
    "PRESIDIO_ANALYZER_DETECT_BARE_INN_BY_CHECKSUM",
    "PRESIDIO_ANALYZER_DETECT_PUBLIC_IPS",
    "PRESIDIO_ANALYZER_INTERNAL_DOMAIN_SUFFIXES",
)
ANALYSIS_SIGNATURE_DISTRIBUTIONS = (
    "presidio-analyzer",
    "ru-core-news-sm",
    "spacy",
    "torch",
    "transformers",
)
_ANALYZER_CORRELATION_CONTEXT: ContextVar[dict[str, str | int]] = ContextVar(
    "ru_llm_proxy_analyzer_correlation",
    default={},
)
_ANALYZER_CANCELLATION_CONTEXT: ContextVar[threading.Event | None] = ContextVar(
    "ru_llm_proxy_analyzer_cancellation",
    default=None,
)


class AnalyzerWorkCancelled(RuntimeError):
    """Raised inside the worker after cooperative request cancellation."""


class AnalyzerClientDisconnected(RuntimeError):
    """Raised after an abandoned HTTP request has stopped its worker."""


def _analysis_source_files() -> tuple[Path, ...]:
    """Return source files whose behavior affects Analyzer findings."""
    root = Path(__file__).resolve().parent
    files = [
        root / "analyzer_server.py",
        root / "entity_types.py",
        root / "result_merging.py",
        root / "text_chunking.py",
    ]
    for directory in (root / "ner", root / "recognizers"):
        files.extend(sorted(directory.glob("*.py")))
    return tuple(path for path in files if path.is_file())


def _distribution_version(name: str) -> str:
    """Return a stable installed distribution version for the signature."""
    try:
        return importlib.metadata.version(name)
    except importlib.metadata.PackageNotFoundError:
        return "not-installed"


def _build_analysis_signature() -> str:
    """Hash the complete non-secret Analyzer behavior contract."""
    root = Path(__file__).resolve().parent
    sources = {}
    for path in _analysis_source_files():
        relative_path = path.relative_to(root).as_posix()
        sources[relative_path] = hashlib.sha256(path.read_bytes()).hexdigest()

    payload = {
        "schema": ANALYSIS_SIGNATURE_SCHEMA,
        "model": {"id": MODEL_ID, "revision": MODEL_REVISION},
        "distributions": {
            name: _distribution_version(name)
            for name in ANALYSIS_SIGNATURE_DISTRIBUTIONS
        },
        "environment": {
            name: os.getenv(name, "") for name in ANALYSIS_SIGNATURE_ENV_NAMES
        },
        "sources": sources,
    }
    serialized = json.dumps(
        payload,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(serialized).hexdigest()


ANALYSIS_SIGNATURE = _build_analysis_signature()


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


def _validate_analyzer_correlation(
    request_id: object,
    text_field_index: object,
) -> dict[str, str | int]:
    """Return only canonical bounded internal correlation values."""
    if not isinstance(request_id, str) or not isinstance(text_field_index, str):
        return {}
    try:
        canonical_request_id = str(uuid.UUID(request_id))
    except (ValueError, AttributeError, TypeError):
        return {}
    if request_id.lower() != canonical_request_id:
        return {}
    if not text_field_index.isascii() or not text_field_index.isdecimal():
        return {}
    field_index = int(text_field_index)
    if not 1 <= field_index <= 1_000_000:
        return {}
    return {
        "request_id": canonical_request_id,
        "text_field_index": field_index,
    }


def _correlation_log_fields() -> dict[str, str | int]:
    """Return a copy of validated request correlation for safe logs."""
    return dict(_ANALYZER_CORRELATION_CONTEXT.get())


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
        "analysis_signature": ANALYSIS_SIGNATURE,
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
async def analyze(
    request: AnalyzeRequest,
    request_id_header: str | None = Header(
        default=None,
        alias=ANALYZER_REQUEST_ID_HEADER,
    ),
    text_field_index_header: str | None = Header(
        default=None,
        alias=ANALYZER_TEXT_FIELD_INDEX_HEADER,
    ),
    http_request: Request = None,
):
    """Analyze one text field with optional validated internal correlation."""
    correlation = _validate_analyzer_correlation(
        request_id_header,
        text_field_index_header,
    )
    context_token = _ANALYZER_CORRELATION_CONTEXT.set(correlation)
    try:
        if http_request is None:
            return await _analyze_with_capacity(request)
        try:
            return await _analyze_until_disconnect(
                request,
                http_request.is_disconnected,
            )
        except AnalyzerClientDisconnected:
            return Response(status_code=499)
    finally:
        _ANALYZER_CORRELATION_CONTEXT.reset(context_token)


async def _analyze_until_disconnect(
    request: AnalyzeRequest,
    is_disconnected: Callable[[], Awaitable[bool]],
) -> AnalyzeResponse:
    """Stop bounded Analyzer work after its HTTP client disconnects."""
    analysis_task = asyncio.create_task(_analyze_with_capacity(request))
    try:
        while True:
            try:
                return await asyncio.wait_for(
                    asyncio.shield(analysis_task),
                    timeout=0.25,
                )
            except TimeoutError:
                if not await is_disconnected():
                    continue
                analysis_task.cancel()
                with suppress(asyncio.CancelledError):
                    await analysis_task
                raise AnalyzerClientDisconnected from None
    except asyncio.CancelledError:
        analysis_task.cancel()
        with suppress(asyncio.CancelledError):
            await analysis_task
        raise


async def _analyze_with_capacity(request: AnalyzeRequest) -> AnalyzeResponse:
    """Apply bounded queueing and execute one Analyzer request."""
    started_at = time.perf_counter()
    queue_started_at = time.perf_counter()
    try:
        slot = await capacity_limiter.acquire()
    except CapacityRejected as e:
        queue_wait_seconds = time.perf_counter() - queue_started_at
        _record_queue_wait(e.reason, queue_wait_seconds)
        _emit_analyzer_telemetry(
            request=request,
            started_at=started_at,
            outcome="overload",
            failure_reason=e.reason,
            queue_wait_seconds=queue_wait_seconds,
        )
        raise HTTPException(
            status_code=e.status_code,
            detail={
                "code": "analyzer_overloaded",
                "reason": e.reason,
                "message": str(e),
            },
        ) from e

    queue_wait_seconds = time.perf_counter() - queue_started_at
    _record_queue_wait("acquired", queue_wait_seconds)
    try:
        async with slot:
            response = await _run_blocking_analyze(request)
    except asyncio.CancelledError:
        _emit_analyzer_telemetry(
            request=request,
            started_at=started_at,
            outcome="timeout_or_cancelled",
            failure_reason="cancelled",
            queue_wait_seconds=queue_wait_seconds,
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
            queue_wait_seconds=queue_wait_seconds,
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
            queue_wait_seconds=queue_wait_seconds,
        )
        raise

    entity_counts = _entity_counts_from_response(response)
    _emit_analyzer_telemetry(
        request=request,
        started_at=started_at,
        outcome="success" if entity_counts else "no_entities",
        entity_counts=entity_counts,
        queue_wait_seconds=queue_wait_seconds,
    )
    return response


async def _run_blocking_analyze(request: AnalyzeRequest) -> AnalyzeResponse:
    """Run blocking analyzer work without releasing capacity on cancellation."""
    cancellation_event = threading.Event()
    context_token = _ANALYZER_CANCELLATION_CONTEXT.set(cancellation_event)
    try:
        task = asyncio.create_task(asyncio.to_thread(_analyze_sync, request))
    finally:
        _ANALYZER_CANCELLATION_CONTEXT.reset(context_token)
    cancelled = False

    while True:
        try:
            response = await asyncio.shield(task)
        except asyncio.CancelledError:
            cancelled = True
            cancellation_event.set()
            if task.done():
                break
            continue
        except AnalyzerWorkCancelled:
            if cancelled:
                raise asyncio.CancelledError from None
            raise
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
        except AnalyzerWorkCancelled:
            pass
        except Exception as e:
            logger.error(
                "Analyzer work failed after request cancellation: error_type=%s",
                type(e).__name__,
            )
    raise asyncio.CancelledError


def _analyze_sync(request: AnalyzeRequest) -> AnalyzeResponse:
    ner_telemetries: list[NERInferenceTelemetry] = []
    ner_telemetry: NERInferenceTelemetry | None = None
    ner_telemetry_recorded = False
    readiness_started_at = time.perf_counter()
    cancellation_event = _ANALYZER_CANCELLATION_CONTEXT.get()

    def capture_ner_telemetry(telemetry: NERInferenceTelemetry) -> None:
        ner_telemetries.append(telemetry)

    # The required model is a service-wide readiness dependency. Never serve a
    # deterministic-only degraded mode after NER has failed.
    try:
        ner_recognizer.require_ready()

        chunks = plan_text_chunks(request.text)
        _record_text_chunk_metrics(chunks)
        results: list[RecognizerResult] = []
        presidio_duration = 0.0
        for chunk in chunks:
            _raise_if_cancelled(cancellation_event)
            chunk_text = request.text[chunk.start : chunk.end]
            presidio_started_at = time.perf_counter()
            try:
                chunk_results = analyzer.analyze(
                    text=chunk_text,
                    language=request.language,
                    entities=request.entities,
                    score_threshold=request.score_threshold,
                )
            except Exception:
                presidio_duration += time.perf_counter() - presidio_started_at
                _record_analyzer_phase("presidio", "failure", presidio_duration)
                raise
            presidio_duration += time.perf_counter() - presidio_started_at
            results.extend(
                _shift_recognizer_result(result, chunk.start)
                for result in chunk_results
                if _is_complete_chunk_result(result, chunk, len(request.text))
            )

            _raise_if_cancelled(cancellation_event)
            ner_results = ner_recognizer.analyze(
                chunk_text,
                score_threshold=request.score_threshold,
                entities=request.entities,
                telemetry_callback=capture_ner_telemetry,
                cancellation_event=cancellation_event,
            )
            results.extend(
                _shift_recognizer_result(result, chunk.start)
                for result in ner_results
                if _is_complete_chunk_result(result, chunk, len(request.text))
            )
            _raise_if_cancelled(cancellation_event)

        _record_analyzer_phase(
            "presidio",
            "success",
            presidio_duration,
        )

        # Record one aggregate NER event even when large input used many chunks.
        ner_telemetry = _aggregate_ner_telemetry(ner_telemetries)
        if ner_telemetry is not None:
            _record_ner_inference(ner_telemetry)
            ner_telemetry_recorded = True

        # A concurrent inference may have latched the shared backend as failed.
        ner_recognizer.require_ready()

        # 3. Merge overlaps using source-specific confidence contracts.
        merge_started_at = time.perf_counter()
        try:
            merge_outcome = merge_results(
                request.text,
                results,
                requested_entities=request.entities,
                score_threshold=request.score_threshold,
            )
        except Exception:
            _record_analyzer_phase(
                "merge",
                "failure",
                time.perf_counter() - merge_started_at,
            )
            raise
        _record_analyzer_phase(
            "merge",
            "success",
            time.perf_counter() - merge_started_at,
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
        ner_telemetry = _aggregate_ner_telemetry(ner_telemetries)
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
                input_tokens=(
                    ner_telemetry.input_tokens
                    if ner_telemetry is not None
                    else 0
                ),
                failure_phase=error.phase,
                failure_class=error.failure_class,
            )
        raise
    finally:
        if ner_telemetry is not None and not ner_telemetry_recorded:
            _record_ner_inference(ner_telemetry)


def _raise_if_cancelled(cancellation_event: threading.Event | None) -> None:
    """Stop bounded worker work at the next safe chunk or batch boundary."""
    if cancellation_event is not None and cancellation_event.is_set():
        raise AnalyzerWorkCancelled("Analyzer request was cancelled")


def _shift_recognizer_result(
    result: RecognizerResult,
    offset: int,
) -> RecognizerResult:
    """Translate one chunk-local Presidio result to source-text offsets."""
    if offset == 0:
        return result
    return RecognizerResult(
        entity_type=result.entity_type,
        start=result.start + offset,
        end=result.end + offset,
        score=result.score,
        analysis_explanation=result.analysis_explanation,
        recognition_metadata=dict(result.recognition_metadata or {}),
    )


def _is_complete_chunk_result(
    result: RecognizerResult,
    chunk: TextChunk,
    source_length: int,
) -> bool:
    """Reject candidates truncated by an artificial outer chunk boundary."""
    if chunk.start > 0 and result.start <= 0:
        return False
    if chunk.end < source_length and result.end >= chunk.length:
        return False
    return True


def _aggregate_ner_telemetry(
    telemetry_items: list[NERInferenceTelemetry],
) -> NERInferenceTelemetry | None:
    """Combine per-chunk NER measurements into one request-scoped event."""
    if not telemetry_items:
        return None
    failure = next(
        (
            item
            for item in reversed(telemetry_items)
            if item.outcome in {"failure", "unavailable"}
        ),
        None,
    )
    outcomes = {item.outcome for item in telemetry_items}
    outcome = failure.outcome if failure is not None else (
        "skipped" if outcomes == {"skipped"} else "success"
    )
    return NERInferenceTelemetry(
        outcome=outcome,
        duration_seconds=sum(item.duration_seconds for item in telemetry_items),
        windows_processed=sum(item.windows_processed for item in telemetry_items),
        input_tokens=sum(item.input_tokens for item in telemetry_items),
        failure_phase=failure.failure_phase if failure is not None else None,
        failure_class=failure.failure_class if failure is not None else None,
    )


def _record_text_chunk_metrics(chunks: tuple[TextChunk, ...]) -> None:
    """Record bounded outer chunk dimensions without request-derived labels."""
    try:
        ANALYZER_TEXT_CHUNKS.observe(len(chunks))
        for chunk in chunks:
            ANALYZER_TEXT_CHUNK_CHARACTERS.observe(chunk.length)
    except Exception as exc:
        logger.warning(
            "Analyzer text chunk metric recording failed: error_type=%s",
            type(exc).__name__,
        )


def _record_merge_metrics(decisions: tuple[MergeDecision, ...]) -> None:
    """Record bounded merge diagnostics without entity values or offsets."""
    for decision in decisions:
        ANALYZER_MERGE_DECISIONS.labels(
            reason=decision.reason,
            winner_source=decision.winner_source,
            loser_source=decision.loser_source,
        ).inc()


def _record_queue_wait(outcome: str, duration_seconds: float) -> None:
    """Record bounded queue timing without request-derived labels."""
    try:
        ANALYZER_QUEUE_WAIT.labels(outcome=outcome).observe(duration_seconds)
    except Exception as exc:
        logger.warning(
            "Analyzer queue metric recording failed: error_type=%s",
            type(exc).__name__,
        )


def _record_analyzer_phase(
    phase: str,
    outcome: str,
    duration_seconds: float,
) -> None:
    """Record and log one bounded Analyzer computation phase."""
    try:
        ANALYZER_PHASE_LATENCY.labels(
            phase=phase,
            outcome=outcome,
        ).observe(duration_seconds)
    except Exception as exc:
        logger.warning(
            "Analyzer phase metric recording failed: error_type=%s",
            type(exc).__name__,
        )
    _safe_log(
        logging.INFO,
        "presidio_analyzer_phase",
        phase=phase,
        outcome=outcome,
        duration_ms=round(duration_seconds * 1000, 3),
        **_correlation_log_fields(),
    )


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
        ANALYZER_NER_INPUT_TOKENS.labels(outcome=telemetry.outcome).observe(
            telemetry.input_tokens
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

    _record_analyzer_phase(
        "ner",
        telemetry.outcome,
        telemetry.duration_seconds,
    )
    fields = {
        "outcome": telemetry.outcome,
        "duration_ms": round(telemetry.duration_seconds * 1000, 3),
        "windows_processed": telemetry.windows_processed,
        "input_tokens": telemetry.input_tokens,
        **_correlation_log_fields(),
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
    input_characters: int,
    entity_counts: dict[str, int],
    failure_reason: str | None = None,
) -> None:
    """Update low-cardinality Analyzer metrics."""
    ANALYZER_REQUESTS.labels(outcome=outcome).inc()
    ANALYZER_LATENCY.labels(outcome=outcome).observe(latency_seconds)
    ANALYZER_INPUT_CHARACTERS.observe(input_characters)
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
    queue_wait_seconds: float = 0.0,
) -> None:
    """Emit one safe telemetry event for an Analyzer request."""
    entity_counts = entity_counts or {}
    latency_seconds = time.perf_counter() - started_at
    _record_analyzer_metrics(
        outcome=outcome,
        latency_seconds=latency_seconds,
        input_characters=len(request.text),
        entity_counts=entity_counts,
        failure_reason=failure_reason,
    )

    fields = {
        "event_id": str(uuid.uuid4()),
        "outcome": outcome,
        "latency_ms": round(latency_seconds * 1000, 3),
        "queue_wait_ms": round(queue_wait_seconds * 1000, 3),
        "input_character_count": len(request.text),
        "text_chunk_count": len(plan_text_chunks(request.text)),
        "entity_count": sum(entity_counts.values()),
        "entity_counts": entity_counts,
        "language": request.language,
        "score_threshold": request.score_threshold,
        "ner": "loaded" if ner_recognizer.is_loaded() else "not_loaded",
        "ner_state": ner_recognizer.state(),
        "capacity": capacity_limiter.snapshot(),
        **_correlation_log_fields(),
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
