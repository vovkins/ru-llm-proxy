"""Tests for DeepPavlov NER integration."""

import pytest
from unittest.mock import MagicMock, patch

from presidio.ner.deeppavlov_recognizer import (
    DEFAULT_NER_SCORE,
    DeepPavlovRecognizer,
    _merge_bio_tags,
    should_run_ner,
    DEEPPAVLOV_ENTITY_MAP,
)


class TestMergeBIOTags:
    def test_single_person(self):
        tokens = ["Иван", "Иванов", "пошёл", "домой"]
        tags = ["B-PER", "I-PER", "O", "O"]
        entities = _merge_bio_tags(tokens, tags)
        assert len(entities) == 1
        assert entities[0]["entity_tag"] == "PER"
        assert entities[0]["text_parts"] == ["Иван", "Иванов"]

    def test_multiple_entities(self):
        tokens = ["Иван", "из", "Москвы", "работает", "в", "Газпроме"]
        tags = ["B-PER", "O", "B-LOC", "O", "O", "B-ORG"]
        entities = _merge_bio_tags(tokens, tags)
        assert len(entities) == 3
        assert entities[0]["entity_tag"] == "PER"
        assert entities[1]["entity_tag"] == "LOC"
        assert entities[2]["entity_tag"] == "ORG"

    def test_no_entities(self):
        tokens = ["просто", "текст", "без", "сущностей"]
        tags = ["O", "O", "O", "O"]
        entities = _merge_bio_tags(tokens, tags)
        assert len(entities) == 0

    def test_adjacent_entities(self):
        tokens = ["Мария", "Петрова", "Сбербанк"]
        tags = ["B-PER", "I-PER", "B-ORG"]
        entities = _merge_bio_tags(tokens, tags)
        assert len(entities) == 2
        assert entities[0]["text_parts"] == ["Мария", "Петрова"]
        assert entities[1]["text_parts"] == ["Сбербанк"]


class TestDeepPavlovRecognizer:
    def test_entity_mapping(self):
        """Verify entity type mappings are correct."""
        assert DEEPPAVLOV_ENTITY_MAP["PER"] == "PERSON"
        assert DEEPPAVLOV_ENTITY_MAP["LOC"] == "LOCATION"
        assert DEEPPAVLOV_ENTITY_MAP["ORG"] == "ORGANIZATION"

    @patch("presidio.ner.deeppavlov_recognizer.DeepPavlovRecognizer.load_model")
    def test_analyze_with_mock(self, mock_load):
        """Test analyze with mocked DeepPavlov model."""
        recognizer = DeepPavlovRecognizer()
        recognizer._model = MagicMock()

        # Simulate DeepPavlov output for "Иван Иванов из Москвы"
        recognizer._model.return_value = [
            [["Иван", "Иванов", "из", "Москвы"]],
            [["B-PER", "I-PER", "O", "B-LOC"]],
        ]

        results = recognizer.analyze("Иван Иванов из Москвы")
        assert len(results) == 2

        person_results = [r for r in results if r.entity_type == "PERSON"]
        loc_results = [r for r in results if r.entity_type == "LOCATION"]
        assert len(person_results) == 1
        assert len(loc_results) == 1

    @patch("presidio.ner.deeppavlov_recognizer.DeepPavlovRecognizer.load_model")
    def test_analyze_repeated_entities_uses_sequential_offsets(self, mock_load):
        """Repeated equal entity text should map to sequential spans."""
        recognizer = DeepPavlovRecognizer()
        recognizer._model = MagicMock()
        recognizer._model.return_value = [
            [["Иван", "Иванов", "встретил", "Иван", "Иванов"]],
            [["B-PER", "I-PER", "O", "B-PER", "I-PER"]],
        ]

        text = "Иван Иванов встретил Иван Иванов"
        results = recognizer.analyze(text)

        assert len(results) == 2
        assert [text[r.start : r.end] for r in results] == [
            "Иван Иванов",
            "Иван Иванов",
        ]
        assert results[0].start == 0
        assert results[1].start == text.rindex("Иван Иванов")

    @patch("presidio.ner.deeppavlov_recognizer.DeepPavlovRecognizer.load_model")
    def test_analyze_filters_requested_entities(self, mock_load):
        """NER should only emit requested Presidio entity types."""
        recognizer = DeepPavlovRecognizer()
        recognizer._model = MagicMock()
        recognizer._model.return_value = [
            [["Иван", "из", "Москвы", "работает", "в", "Газпроме"]],
            [["B-PER", "O", "B-LOC", "O", "O", "B-ORG"]],
        ]

        results = recognizer.analyze(
            "Иван из Москвы работает в Газпроме",
            entities=["LOCATION"],
        )

        assert [r.entity_type for r in results] == ["LOCATION"]

    @patch("presidio.ner.deeppavlov_recognizer.DeepPavlovRecognizer.load_model")
    def test_filtering_preserves_offsets_after_skipped_entities(self, mock_load):
        """Skipped NER types should still advance span alignment."""
        recognizer = DeepPavlovRecognizer()
        recognizer._model = MagicMock()
        recognizer._model.return_value = [
            [["Москва", "и", "Москва"]],
            [["B-ORG", "O", "B-LOC"]],
        ]

        text = "Москва и Москва"
        results = recognizer.analyze(text, entities=["LOCATION"])

        assert len(results) == 1
        assert results[0].entity_type == "LOCATION"
        assert results[0].start == text.rindex("Москва")

    @patch("presidio.ner.deeppavlov_recognizer.DeepPavlovRecognizer.load_model")
    def test_score_threshold_above_default_skips_model(self, mock_load):
        """DeepPavlov has no per-entity score, so high thresholds skip NER."""
        recognizer = DeepPavlovRecognizer()

        results = recognizer.analyze("Иван Иванов", score_threshold=DEFAULT_NER_SCORE + 0.01)

        assert results == []
        mock_load.assert_not_called()

    def test_is_loaded_false(self):
        recognizer = DeepPavlovRecognizer()
        assert recognizer.is_loaded() is False

    def test_is_loaded_true(self):
        recognizer = DeepPavlovRecognizer()
        recognizer._model = MagicMock()
        assert recognizer.is_loaded() is True

    @patch("presidio.ner.deeppavlov_recognizer.DeepPavlovRecognizer.load_model")
    def test_analyze_empty_result(self, mock_load):
        """Test with model returning no entities."""
        recognizer = DeepPavlovRecognizer()
        recognizer._model = MagicMock()
        recognizer._model.return_value = [[[]], [[]]]

        results = recognizer.analyze("просто текст")
        assert len(results) == 0


class TestShouldRunNER:
    def test_runs_when_entities_are_not_limited(self):
        assert should_run_ner(None, score_threshold=0.35) is True

    def test_runs_when_requested_entities_include_ner_type(self):
        assert should_run_ner(["PERSON"], score_threshold=0.35) is True

    def test_skips_when_requested_entities_exclude_ner_types(self):
        assert should_run_ner(["RU_INN", "PHONE_NUMBER"], score_threshold=0.35) is False

    def test_skips_when_score_threshold_is_too_high(self):
        assert should_run_ner(["PERSON"], score_threshold=DEFAULT_NER_SCORE + 0.01) is False
