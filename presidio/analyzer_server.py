"""Presidio Analyzer REST server for ru-llm-proxy."""

import logging
from fastapi import FastAPI
from pydantic import BaseModel
from presidio_analyzer import AnalyzerEngine, RecognizerRegistry
from presidio_analyzer.nlp_engine import NlpEngineProvider

from recognizers import ALL_RECOGNIZERS
from ner import DeepPavlovRecognizer

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="Presidio Analyzer (ru-llm-proxy)")
# Configure NLP engine with Russian spaCy model
nlp_engine_provider = NlpEngineProvider()
nlp_engine = nlp_engine_provider.create_engine({
    "nlp_engine_name": "spacy",
    "models": [{"lang_code": "ru", "model_name": "ru_core_news_sm"}],
})
analyzer = AnalyzerEngine(
    registry=RecognizerRegistry(),
    nlp_engine=nlp_engine,
    supported_languages=["ru"],
)

# Register custom Russian regex recognizers
for recognizer_cls in ALL_RECOGNIZERS:
    recognizer = recognizer_cls()
    analyzer.registry.add_recognizer(recognizer)
    logger.info(f"Registered recognizer: {recognizer.name}")

# Initialize DeepPavlov NER
dp_recognizer = DeepPavlovRecognizer()


@app.on_event("startup")
async def startup():
    """Load NER model on startup."""
    logger.info("Loading DeepPavlov NER model...")
    try:
        dp_recognizer.load_model()
        logger.info("DeepPavlov NER model loaded")
    except Exception as e:
        logger.error(f"Failed to load DeepPavlov model: {e}")
        logger.info("Server will start without NER. Regex recognizers still available.")


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
    return {"status": "ok", "ner": ner_status}


@app.post("/api/v1/analyze", response_model=AnalyzeResponse)
async def analyze(request: AnalyzeRequest):
    # 1. Run Presidio with regex recognizers
    results = analyzer.analyze(
        text=request.text,
        language=request.language,
        entities=request.entities,
        score_threshold=request.score_threshold,
    )

    # 2. Run DeepPavlov NER and merge results
    if dp_recognizer.is_loaded():
        try:
            ner_results = dp_recognizer.analyze(request.text, score_threshold=0.7)
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

    uvicorn.run(app, host="0.0.0.0", port=5001)
