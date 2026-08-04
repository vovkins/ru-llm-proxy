#!/usr/bin/env python3
"""Load the pinned model and run offline inference without printing input."""

from __future__ import annotations

import json

from ner import MODEL_ID, MODEL_REVISION, HuggingFaceNERRecognizer


def main() -> None:
    recognizer = HuggingFaceNERRecognizer()
    short_text = "Клиент Иван Петров подписал договор номер AB-12345."
    short_results = recognizer.analyze(
        short_text,
        score_threshold=0.0,
    )
    long_text = (
        "Служебная запись без чувствительных значений. " * 90
        + "Ответственный: Анна Соколова."
    )
    long_results = recognizer.analyze(
        long_text,
        score_threshold=0.35,
    )
    unicode_text = "Клиент Сергеи\u0306 Петров согласовал документ."
    unicode_results = recognizer.analyze(
        unicode_text,
        score_threshold=0.0,
    )

    result_sets = {
        "short": (short_text, short_results),
        "long": (long_text, long_results),
        "unicode": (unicode_text, unicode_results),
    }
    for scenario, (text, results) in result_sets.items():
        if any(not 0 <= result.start < result.end <= len(text) for result in results):
            raise RuntimeError(f"{scenario} NER result has invalid original offsets")

    expected_long_start = long_text.index("Анна Соколова")
    if not any(
        result.entity_type == "PERSON"
        and result.start == expected_long_start
        and result.end == expected_long_start + len("Анна Соколова")
        for result in long_results
    ):
        raise RuntimeError("long-window NER did not detect the expected tail entity")

    normalized_name = "Сергей Петров"
    if not any(
        result.entity_type == "PERSON"
        and unicode_text[result.start : result.end] == "Сергеи\u0306 Петров"
        for result in unicode_results
    ):
        raise RuntimeError(
            f"Unicode NER did not map {len(normalized_name)} normalized characters"
        )

    if not recognizer.is_ready():
        raise RuntimeError("pinned NER model did not reach ready state")

    entity_types = {
        scenario: sorted({result.entity_type for result in results})
        for scenario, (_text, results) in result_sets.items()
    }
    result_counts = {
        scenario: len(results)
        for scenario, (_text, results) in result_sets.items()
    }

    print(
        json.dumps(
            {
                "status": "ok",
                "model": MODEL_ID,
                "revision": MODEL_REVISION,
                "state": recognizer.state(),
                "warmed_up": recognizer.is_warmed_up(),
                "result_counts": result_counts,
                "entity_types": entity_types,
            },
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
