"""Exercise entity filters against the real Presidio registry and NLP engine."""

from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient
from presidio_analyzer import RecognizerResult

from presidio import analyzer_server
from result_merging import DETECTION_SOURCE_METADATA_KEY, SOURCE_NER

NER_VALUES = {
    "PERSON": "Иван Петров",
    "ORGANIZATION": "ООО Вектор",
    "CONTRACT_NUMBER": "AG-2026/44",
    "LOCATION": "Москве",
}
FILTER_TEXT = (
    "Клиент Иван Петров из ООО Вектор в Москве подписал договор № AG-2026/44. "
    "КПП: 770801001."
)
BERT_ONLY_TYPES = {"PERSON", "ORGANIZATION", "CONTRACT_NUMBER"}


def _ner_result(text, entity_type, value):
    start = text.index(value)
    return RecognizerResult(
        entity_type=entity_type,
        start=start,
        end=start + len(value),
        score=0.8,
        recognition_metadata={DETECTION_SOURCE_METADATA_KEY: SOURCE_NER},
    )


@pytest.fixture
def sources(monkeypatch):
    presidio = Mock(wraps=analyzer_server.analyzer.analyze)

    def analyze_ner(text, *, entities, score_threshold, **_kwargs):
        return [
            _ner_result(text, entity_type, value)
            for entity_type, value in NER_VALUES.items()
            if value in text
            and (not entities or entity_type in entities)
            and score_threshold <= 0.8
        ]

    ner = Mock(side_effect=analyze_ner)
    monkeypatch.setattr(analyzer_server.analyzer, "analyze", presidio)
    monkeypatch.setattr(analyzer_server.ner_recognizer, "require_ready", lambda: None)
    monkeypatch.setattr(analyzer_server.ner_recognizer, "analyze", ner)
    return presidio, ner


@pytest.mark.parametrize(
    "entities",
    [
        ["PERSON"],
        ["ORGANIZATION"],
        ["CONTRACT_NUMBER"],
        ["PERSON", "ORGANIZATION"],
        ["PERSON", "CONTRACT_NUMBER"],
        ["ORGANIZATION", "CONTRACT_NUMBER"],
        ["PERSON", "ORGANIZATION", "CONTRACT_NUMBER"],
        ["PERSON", "RU_KPP"],
        ["ORGANIZATION", "RU_KPP"],
        ["CONTRACT_NUMBER", "RU_KPP"],
        ["PERSON", "ORGANIZATION", "CONTRACT_NUMBER", "RU_KPP"],
        ["RU_KPP"],
        ["LOCATION"],
        ["PERSON", "LOCATION"],
        None,
        [],
    ],
)
def test_filters_route_to_available_sources(sources, entities):
    presidio, ner = sources
    response = TestClient(analyzer_server.app).post(
        "/api/v1/analyze",
        json={"text": FILTER_TEXT, "entities": entities, "score_threshold": 0.35},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["text"] == FILTER_TEXT
    expected_types = set(entities or (*NER_VALUES, "RU_KPP"))
    assert {entity["entity_type"] for entity in payload["entities"]} == expected_types
    for entity in payload["entities"]:
        expected_value = NER_VALUES.get(entity["entity_type"], "770801001")
        assert entity["text"] == expected_value
        assert FILTER_TEXT[entity["start"] : entity["end"]] == expected_value

    expected_presidio_filter = (
        [entity for entity in entities if entity not in BERT_ONLY_TYPES]
        if entities
        else entities
    )
    if entities and not expected_presidio_filter:
        presidio.assert_not_called()
    else:
        presidio.assert_called_once()
        assert presidio.call_args.kwargs["entities"] == expected_presidio_filter
    ner.assert_called_once()
    expected_ner_filter = entities
    if entities and {"PERSON", "ORGANIZATION"}.intersection(entities):
        expected_ner_filter = list(dict.fromkeys([*entities, "PERSON", "ORGANIZATION"]))
    assert ner.call_args.kwargs["entities"] == expected_ner_filter
    assert ner.call_args.kwargs["score_threshold"] == 0.35


def test_category_shared_with_bert_keeps_its_regex_recognizer(sources):
    presidio, _ner = sources
    response = TestClient(analyzer_server.app).post(
        "/api/v1/analyze", json={"text": "PGUSER=analytics", "entities": ["LOGIN"]}
    )
    assert response.status_code == 200
    assert [entity["text"] for entity in response.json()["entities"]] == ["analytics"]
    assert presidio.call_args.kwargs["entities"] == ["LOGIN"]


@pytest.mark.parametrize("entities", [None, ["PERSON"], ["PERSON", "RU_KPP"]])
def test_name_context_reuses_one_nlp_pass(sources, monkeypatch, entities):
    presidio, _ner = sources
    artifacts = analyzer_server.nlp_engine.process_text(FILTER_TEXT, "ru")
    process = Mock(return_value=artifacts)
    monkeypatch.setattr(analyzer_server.nlp_engine, "process_text", process)
    response = TestClient(analyzer_server.app).post(
        "/api/v1/analyze", json={"text": FILTER_TEXT, "entities": entities}
    )
    assert response.status_code == 200
    process.assert_called_once_with(FILTER_TEXT, "ru")
    if presidio.called:
        assert presidio.call_args.kwargs["nlp_artifacts"] is artifacts


def test_api_filters_after_legal_name_refinement(sources):
    _presidio, ner = sources
    text = "Клиент ООО Иван Петров подтвердил заявку."
    ner.side_effect = lambda *_args, **_kwargs: [
        _ner_result(text, "ORGANIZATION", "ООО"),
        _ner_result(text, "PERSON", "Иван Петров"),
    ]
    for entity_type in ("PERSON", "ORGANIZATION"):
        response = TestClient(analyzer_server.app).post(
            "/api/v1/analyze", json={"text": text, "entities": [entity_type]}
        )
        assert response.status_code == 200
        assert [r["text"] for r in response.json()["entities"]] == (
            ["ООО Иван Петров"] if entity_type == "ORGANIZATION" else []
        )


@pytest.mark.parametrize("entity_type", sorted(BERT_ONLY_TYPES))
def test_no_matching_value_is_a_successful_empty_result(sources, entity_type):
    presidio, ner = sources
    response = TestClient(analyzer_server.app).post(
        "/api/v1/analyze",
        json={"text": "Верни ровно MODEL_OK.", "entities": [entity_type]},
    )
    assert response.status_code == 200
    assert response.json()["entities"] == []
    presidio.assert_not_called()
    ner.assert_called_once()


def test_contract_filter_preserves_context_fallback(sources):
    presidio, ner = sources
    ner.side_effect = lambda *_args, **_kwargs: []
    response = TestClient(analyzer_server.app).post(
        "/api/v1/analyze",
        json={
            "text": "Договор № AG-2026/44 подписан.",
            "entities": ["CONTRACT_NUMBER"],
        },
    )
    assert response.status_code == 200
    assert [(r["entity_type"], r["text"]) for r in response.json()["entities"]] == [
        ("CONTRACT_NUMBER", "AG-2026/44")
    ]
    presidio.assert_not_called()
    ner.assert_called_once()


@pytest.mark.parametrize("score_threshold", [0.35, 0.9])
def test_bert_only_filter_preserves_score_threshold(sources, score_threshold):
    _presidio, ner = sources
    response = TestClient(analyzer_server.app).post(
        "/api/v1/analyze",
        json={
            "text": FILTER_TEXT,
            "entities": ["PERSON"],
            "score_threshold": score_threshold,
        },
    )
    assert response.status_code == 200
    assert bool(response.json()["entities"]) is (score_threshold <= 0.8)
    assert ner.call_args.kwargs["score_threshold"] == score_threshold


@pytest.mark.parametrize("entities", [["UNKNOWN_TYPE"], ["PERSON", "UNKNOWN_TYPE"]])
def test_unknown_only_presidio_filter_does_not_become_success(sources, entities):
    _presidio, ner = sources
    with pytest.raises(ValueError, match="No matching recognizers"):
        TestClient(analyzer_server.app).post(
            "/api/v1/analyze", json={"text": FILTER_TEXT, "entities": entities}
        )
    ner.assert_not_called()


@pytest.mark.parametrize("language", ["en", "unknown"])
def test_unsupported_nlp_language_does_not_use_bert_only_shortcut(sources, language):
    _presidio, ner = sources
    with pytest.raises(ValueError, match="No matching recognizers"):
        TestClient(analyzer_server.app).post(
            "/api/v1/analyze",
            json={"text": FILTER_TEXT, "language": language, "entities": ["PERSON"]},
        )
    ner.assert_not_called()


@pytest.mark.parametrize("error_type", [RuntimeError, ValueError])
def test_real_presidio_errors_are_not_suppressed(sources, error_type):
    presidio, ner = sources
    presidio.side_effect = error_type("analysis failed")
    with pytest.raises(error_type, match="analysis failed"):
        TestClient(analyzer_server.app).post(
            "/api/v1/analyze",
            json={"text": FILTER_TEXT, "entities": ["PERSON", "RU_KPP"]},
        )
    ner.assert_not_called()


@pytest.mark.parametrize("failure_stage", ["readiness", "inference"])
def test_bert_only_filter_keeps_required_ner_failure(
    sources, monkeypatch, failure_stage
):
    presidio, ner = sources
    error = analyzer_server.NERBackendError(
        phase=failure_stage, failure_class="unexpected_failure"
    )
    if failure_stage == "readiness":
        monkeypatch.setattr(
            analyzer_server.ner_recognizer, "require_ready", Mock(side_effect=error)
        )
    else:
        ner.side_effect = error
    response = TestClient(analyzer_server.app).post(
        "/api/v1/analyze", json={"text": FILTER_TEXT, "entities": ["PERSON"]}
    )
    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "required_ner_unavailable"
    presidio.assert_not_called()


def test_bert_only_filter_preserves_offsets_across_outer_chunks(sources, monkeypatch):
    presidio, ner = sources
    parts = ("Клиент Иван Петров.\n", "Клиент Олег Волков.")
    text = "".join(parts)
    chunks = (
        analyzer_server.TextChunk(index=0, start=0, end=len(parts[0])),
        analyzer_server.TextChunk(index=1, start=len(parts[0]), end=len(text)),
    )
    monkeypatch.setattr(analyzer_server, "plan_text_chunks", lambda _text: chunks)

    def analyze_ner(chunk_text, **_kwargs):
        value = "Иван Петров" if "Иван Петров" in chunk_text else "Олег Волков"
        return [_ner_result(chunk_text, "PERSON", value)]

    ner.side_effect = analyze_ner
    response = TestClient(analyzer_server.app).post(
        "/api/v1/analyze", json={"text": text, "entities": ["PERSON"]}
    )
    assert response.status_code == 200
    assert [r["text"] for r in response.json()["entities"]] == [
        "Иван Петров",
        "Олег Волков",
    ]
    for result in response.json()["entities"]:
        assert text[result["start"] : result["end"]] == result["text"]
    presidio.assert_not_called()
    assert [call.args[0] for call in ner.call_args_list] == list(parts)
