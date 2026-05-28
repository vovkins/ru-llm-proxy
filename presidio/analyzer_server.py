"""Presidio Analyzer REST server for ru-llm-proxy."""

import logging
from fastapi import FastAPI
from pydantic import BaseModel
from presidio_analyzer import AnalyzerEngine

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="Presidio Analyzer (ru-llm-proxy)")
analyzer = AnalyzerEngine()


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
    return {"status": "ok"}


@app.post("/api/v1/analyze", response_model=AnalyzeResponse)
async def analyze(request: AnalyzeRequest):
    results = analyzer.analyze(
        text=request.text,
        language=request.language,
        entities=request.entities,
        score_threshold=request.score_threshold,
    )
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


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=5001)
