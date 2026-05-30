"""NER module for ru-llm-proxy."""

from .deeppavlov_recognizer import DeepPavlovRecognizer, should_run_ner

__all__ = ["DeepPavlovRecognizer", "should_run_ner"]
