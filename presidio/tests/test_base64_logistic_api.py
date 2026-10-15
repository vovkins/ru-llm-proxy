"""HTTP, registry and outer chunks; transformer forward is explicitly stubbed."""
import base64
import json
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from presidio_analyzer import AnalyzerEngine, RecognizerRegistry

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from presidio import analyzer_server
from recognizers.base64_data import Base64DataRecognizer
from recognizers.json_strings import JsonStringRecognizer
from recognizers.credential_rules import AuthTokenRecognizer


@pytest.fixture
def api(monkeypatch):
    monkeypatch.setenv("PRESIDIO_ANALYZER_BASE64_POLICY", "logistic")
    registry = RecognizerRegistry(supported_languages=["ru"])
    for recognizer in (Base64DataRecognizer(), JsonStringRecognizer(), AuthTokenRecognizer()):
        registry.add_recognizer(recognizer)
    engine = AnalyzerEngine(registry=registry, nlp_engine=analyzer_server.nlp_engine, supported_languages=["ru"])
    monkeypatch.setattr(analyzer_server, "analyzer", engine)
    monkeypatch.setattr(analyzer_server.ner_recognizer, "require_ready", lambda: None)
    monkeypatch.setattr(analyzer_server.ner_recognizer, "analyze", lambda *_a, **_k: [])
    return TestClient(analyzer_server.app)


@pytest.mark.parametrize("declared", [False, True])
def test_large_container_is_not_truncated_at_outer_chunks(api, declared):
    value = base64.b64encode(("обычная проверка " * 3000).encode()).decode()
    text = f'base64="{value}"' if declared else value
    response = api.post("/api/v1/analyze", json={"text": text, "entities": ["BASE64_DATA"]})
    assert response.status_code == 200
    assert [(e["start"], e["end"]) for e in response.json()["entities"]] == [(text.index(value), text.index(value) + len(value))]


def test_projected_native_basic_token_retains_full_class_and_range(api):
    value = base64.b64encode(b"synthetic-user:synthetic-password").decode()
    text = json.dumps({"source": "Authorization: Basic " + value})
    response = api.post("/api/v1/analyze", json={"text": text, "entities": ["AUTH_TOKEN", "BASE64_DATA"]})
    assert response.status_code == 200
    assert [(e["entity_type"], e["text"]) for e in response.json()["entities"]] == [("AUTH_TOKEN", value)]


def test_unicode_escape_projection_and_filters(api):
    value = base64.b64encode("привет!".encode()).decode().replace("=", "\\u003d")
    text = '{"message":"' + value + '"}'
    response = api.post("/api/v1/analyze", json={"text": text, "entities": ["BASE64_DATA"]})
    assert response.status_code == 200
    assert [e["text"] for e in response.json()["entities"]] == [value]
    assert api.post("/api/v1/analyze", json={"text": text, "entities": ["BASE64_DATA"], "score_threshold": 0.96}).json()["entities"] == []
    assert api.post("/api/v1/analyze", json={"text": 'password_base64="SGVsbG8="', "entities": ["BASE64_DATA"]}).json()["entities"] == []
