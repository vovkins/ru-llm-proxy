"""JSON argument analysis must return source coordinates, not rewritten JSON."""

import json
import sys
from pathlib import Path

import pytest

pytest.importorskip("presidio_analyzer")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from presidio_analyzer import RecognizerResult

from presidio import analyzer_server as server
from recognizers.json_strings import JsonArgumentView, JsonStringRecognizer
from result_merging import DETECTION_SOURCE_METADATA_KEY, SOURCE_NER


def test_lexical_projection_keeps_duplicates_nested_scalars_and_atomic_escapes():
    raw = r' {"\u006b": [1.2300e-02, "a\ud83d\ude00\n\"\\b", {"k":"\\u0418"}], "k":null}'
    view = JsonArgumentView.parse(raw, lambda: None)
    assert view.text == ' {"k": [1.2300e-02, "a😀\\n\\\"\\\\b", {"k":"\\\\u0418"}], "k":null}'
    assert json.loads(view.text) == json.loads(raw)
    value = next(s for s in view.scalars if s.decoded == 'a😀\n"\\b')
    assert [raw[left:right] for left, right in zip(value.source_offsets, value.source_offsets[1:])] == [
        'a', r'\ud83d\ude00', r'\n', r'\"', r'\\', 'b',
    ]
    newline = view.text.index(r'\n')
    assert [raw[a:b] for a, b in view.project(newline + 1, newline + 2)] == [r'\n']
    number = view.text.index('1.2300')
    assert [raw[a:b] for a, b in view.project(number + 1, number + 2)] == ['1.2300e-02']


@pytest.mark.parametrize("source", ['"top-level scalar"', '{"x":NaN}', '["bad\\q"]',
                                   '["\\ud800"]', '{"x":1} trailing', '{"x":1,}'])
def test_non_container_invalid_and_unpaired_json_stay_on_legacy_path(source):
    assert JsonArgumentView.parse(source, lambda: None) is None


def test_crossing_finding_preserves_touched_values_without_masking_json_syntax():
    raw = r'["left\ud83d\ude00","\u0418van"]'
    view = JsonArgumentView.parse(raw, lambda: None)
    spans = view.project(view.text.index("😀"), view.text.index("И") + 1)
    assert [raw[a:b] for a, b in spans] == [r'\ud83d\ude00', r'\u0418']


def test_large_escape_projection_checks_cancellation_during_mapping():
    class Cancelled(Exception):
        pass

    calls = 0

    def check():
        nonlocal calls
        calls += 1
        if calls == 5:
            raise Cancelled

    with pytest.raises(Cancelled):
        JsonArgumentView.parse('["' + r'\u0418' * 2000 + '"]', check)


@pytest.mark.parametrize("words", [8000, 32000])
def test_large_argument_reaches_ner_as_same_logical_value_and_returns_source_offsets(monkeypatch, words):
    monkeypatch.setattr(server.ner_recognizer, "require_ready", lambda: None)
    monkeypatch.setattr(server.analyzer, "analyze", lambda **_: [])
    monkeypatch.setattr(server, "_analyze_document_base64", lambda _: [])
    monkeypatch.setattr(server.nlp_engine, "get_supported_languages", lambda: [])
    analyzed = []
    name = "Иван Петров"

    def inference(text, **_):
        analyzed.append(text)
        if name not in text:
            return []
        start = text.index(name)
        return [RecognizerResult("PERSON", start, start + len(name), 0.99,
                                 recognition_metadata={DETECTION_SOURCE_METADATA_KEY: SOURCE_NER})]

    monkeypatch.setattr(server.ner_recognizer, "analyze", inference)
    # Leave enough overlap to exercise chunking in the 32k-word case.
    document = {"report": "запись " * words + name, "other": ["not joined", "other value"]}
    visits = []
    for ascii_only in (False, True):
        source = json.dumps(document, ensure_ascii=ascii_only)
        result = server._analyze_sync(server.AnalyzeRequest(text=source, input_kind="json_arguments"))
        assert result.text == source
        assert len(result.entities) == 1
        entity = result.entities[0]
        assert entity["entity_type"] == "PERSON"
        assert json.loads('"' + source[entity['start']:entity['end']] + '"') == name
        assert "not joined" in analyzed and "other value" in analyzed
        visits.append(analyzed[:])
        analyzed.clear()
    assert visits[0] == visits[1]


def test_long_json_credential_members_keep_context_beyond_legacy_cap():
    source = json.dumps({"filler": " " * 66_000, "password": "!?$", "comment": "!?$"})
    view = JsonArgumentView.parse(source, lambda: None)
    findings = JsonStringRecognizer().analyze_arguments(view, ["PASSWORD"], lambda: None)
    assert [(r.entity_type, source[r.start:r.end]) for r in findings] == [("PASSWORD", "!?$")]
    assert findings[0].start == source.index("!?$")


def test_confirmed_value_may_resolve_to_whole_value_without_allowing_whole_document():
    text = "an ambiguous single line"
    assert server.BoundaryValues(text).resolve(3, 10) is None
    assert server.BoundaryValues(text, confirmed_value=True).resolve(3, 10) == (0, len(text))
