"""The isolated experiment cannot alter production input or expand offsets."""

import ast
from pathlib import Path

import pytest

from presidio.evaluation.name_recheck_experiment import (
    CONTEXT_CHARS,
    MAX_CANDIDATE_CHARS,
    prepare_window,
)


def test_experiment_keeps_original_text_and_character_offsets():
    text = "Клиент 👩‍💻 ИВАН ПЕТРОВ подтвердил заявку."
    start = text.index("ИВАН")
    end = start + len("ИВАН ПЕТРОВ")
    original = text
    window = prepare_window(text, start, end, recase=True)
    assert window.text[window.candidate_start : window.candidate_end] == "Иван Петров"
    assert (
        window.offset + window.candidate_start,
        window.offset + window.candidate_end,
    ) == (start, end)
    assert text == original


def test_window_is_bounded_even_in_large_context():
    text = "слово " * 10000 + "ИВАН ПЕТРОВ" + " слово" * 10000
    start = text.index("ИВАН")
    for recase in (False, True):
        window = prepare_window(text, start, start + 11, recase=recase)
        assert len(window.text) <= MAX_CANDIDATE_CHARS + 2 * CONTEXT_CHARS


@pytest.mark.parametrize(
    "text,start,end",
    [
        ("ИВАН ПЕТРОВ", 0, 2),
        ("ИВАН ПЕТРОВ", 2, 11),
        ("ИВАН ПЕТРОВ", -1, 11),
        ("ИВАН ПЕТРОВ", 0, 12),
        ("Иван ПЕТРОВ", 0, 11),
        ("ООО 1234", 0, 8),
        ("ИВАН ПЕТРОВ", 4, 4),
    ],
)
def test_non_candidates_are_not_rechecked(text, start, end):
    assert prepare_window(text, start, end, recase=True) is None


def test_production_does_not_import_experiment():
    root = Path(__file__).resolve().parents[1]
    paths = [
        root / "analyzer_server.py",
        root / "result_merging.py",
        *(root / "ner").glob("*.py"),
    ]
    for path in paths:
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                assert "name_recheck_experiment" not in (node.module or "")
            if isinstance(node, ast.Import):
                assert all(
                    "name_recheck_experiment" not in alias.name for alias in node.names
                )
