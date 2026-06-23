"""Presidio Analyzer REST server for ru-llm-proxy."""

import asyncio
import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from presidio_analyzer import AnalyzerEngine
from presidio_analyzer.nlp_engine import NlpEngineProvider

from capacity import CapacityRejected, build_limiter_from_env
from recognizers import ALL_RECOGNIZERS
from ner import DeepPavlovRecognizer, should_run_ner

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    """Load NER model on startup."""
    logger.info("Loading DeepPavlov NER model...")
    try:
        dp_recognizer.load_model()
        logger.info("DeepPavlov NER model loaded")
    except Exception as e:
        logger.error(f"Failed to load DeepPavlov model: {e}")
        logger.info("Server will start without NER. Regex recognizers still available.")
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
    return {
        "status": "ok",
        "ner": ner_status,
        "capacity": capacity_limiter.snapshot(),
    }


@app.post("/api/v1/analyze", response_model=AnalyzeResponse)
async def analyze(request: AnalyzeRequest):
    try:
        async with await capacity_limiter.acquire():
            return await _run_blocking_analyze(request)
    except CapacityRejected as e:
        raise HTTPException(
            status_code=e.status_code,
            detail={
                "code": "analyzer_overloaded",
                "reason": e.reason,
                "message": str(e),
            },
        ) from e


async def _run_blocking_analyze(request: AnalyzeRequest) -> AnalyzeResponse:
    """Run blocking analyzer work without releasing capacity on cancellation."""
    task = asyncio.create_task(asyncio.to_thread(_analyze_sync, request))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        try:
            await task
        except Exception as e:
            logger.error("Analyzer work failed after request cancellation: %s", e)
        raise


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
            logger.error(f"NER analysis error: {e}")

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


if __name__ == "__main__":
    import uvicorn

    port = int(os.getenv("PRESIDIO_ANALYZER_PORT", "5001"))
    workers = int(os.getenv("PRESIDIO_ANALYZER_WORKERS", "1"))
    uvicorn.run("analyzer_server:app", host="0.0.0.0", port=port, workers=workers)
