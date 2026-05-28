"""Presidio Anonymizer REST server for ru-llm-proxy."""

import logging
from fastapi import FastAPI
from pydantic import BaseModel
from presidio_anonymizer import AnonymizerEngine
from presidio_anonymizer.entities import OperatorConfig

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="Presidio Anonymizer (ru-llm-proxy)")
anonymizer = AnonymizerEngine()


class AnonymizeRequest(BaseModel):
    text: str
    entities: list[dict]
    operators: dict[str, str] | None = None


class AnonymizeResponse(BaseModel):
    text: str
    items: list[dict]


@app.get("/api/v1/health")
async def health():
    return {"status": "ok"}


@app.post("/api/v1/anonymize", response_model=AnonymizeResponse)
async def anonymize(request: AnonymizeRequest):
    from presidio_anonymizer.entities import RecognizerResult

    analyzer_results = [
        RecognizerResult(
            entity_type=e["entity_type"],
            start=e["start"],
            end=e["end"],
            score=e.get("score", 0.85),
        )
        for e in request.entities
    ]

    operators = {}
    if request.operators:
        operators = {
            k: OperatorConfig(v) for k, v in request.operators.items()
        }

    result = anonymizer.anonymize(
        text=request.text,
        analyzer_results=analyzer_results,
        operators=operators,
    )

    items = [
        {
            "entity_type": item.entity_type,
            "start": item.start,
            "end": item.end,
            "text": item.text,
            "operator": item.operator,
        }
        for item in result.items
    ]

    return AnonymizeResponse(text=result.text, items=items)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=5002)
