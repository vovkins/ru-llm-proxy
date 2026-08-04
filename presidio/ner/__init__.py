"""Lazy public exports for the Analyzer NER backend."""

from __future__ import annotations

from typing import Any

__all__ = [
    "MODEL_ID",
    "MODEL_REVISION",
    "HuggingFaceNERRecognizer",
    "NERBackendError",
    "NERConfigurationError",
    "NERInferenceTelemetry",
    "NERProcessingError",
    "NERUnavailableError",
    "should_run_ner",
]


def __getattr__(name: str) -> Any:
    if name not in __all__:
        raise AttributeError(name)
    from . import huggingface_recognizer

    return getattr(huggingface_recognizer, name)
