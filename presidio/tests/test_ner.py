"""Tests for DeepPavlov NER integration."""

import pytest
from unittest.mock import MagicMock, patch

from presidio.ner.deeppavlov_recognizer import (
    DeepPavlovRecognizer,
    _merge_bio_tags,
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
