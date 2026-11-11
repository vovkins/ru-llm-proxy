"""Closed numbered prose is evidence of a boundary; parentheses alone are not."""
import pytest

from presidio.ner.boundary_values import (
    _MAX_PROSE_CONTAINER_CHARS, BoundaryContext, BoundaryValues,
)
from presidio.ner.triton_recognizer import NERProcessingError
from presidio.tests.test_ner_window_value_repair import prediction, recognizer


@pytest.mark.parametrize("intro", ["Note:", "See `docs/setup.md`:", "See `docs/setup.md` -"])
@pytest.mark.parametrize("eol", ["\n", "\r\n", "\r", "\u2028"])
def test_masks_whole_insertion_after_a_confirmed_intro(intro, eol):
    body = eol.join(["owner Ivan Petrov (reviewer)", "9: Examples: `demo.md`, `report.txt`"])
    text = eol.join(["Header {meta}", f"8: {intro} ({body})"])
    start = text.index("Petrov")
    bounds = BoundaryValues(text).resolve(start, start + len("Petrov"), entity_type="PERSON")
    assert text[slice(*bounds)] == body
    assert text[bounds[0] - 1] == "("
    assert text[bounds[1]] == ")"


@pytest.mark.parametrize("text", [
    "8: Note: (Ivan Petrov",
    "8: Note: (Ivan Petrov) continuation",
    "8: Note: (Ivan Petrov)\n9: possible continuation",
    "8: Note: (Ivan Petrov\n10: continuation)",
    "8: Note: (Ivan Petrov\n continuation)",
    "8: Note: (Ivan Petrov\n9: - next list item)",
    "8: Note: (Ivan Petrov\n\n9: next paragraph)",
    "8: Note: (Ivan Petrov\n9: url https://example.test/details)",
    "8: Note: (Ivan Petrov\n9: options={value})",
    "8: Note: (foo(Ivan Petrov))",
    "8: Note: (Ivan Petrov);",
    "8: Note: [Ivan Petrov](https://example.test/guide)",
    "8: Note: `call(Ivan Petrov)`",
    "8: Note: (Ivan Petrov `unclosed)",
    "8: Note `unclosed: (Ivan Petrov)",
    "8: Note - (Ivan Petrov \\n)",
    '8: Name = "unclosed\n9: Note: (Ivan Petrov)',
    "7: Company \u00abNorth\n8: Note: (Ivan Petrov)",
    "8: Company \u00abNorth (Ivan Petrov) West\u00bb",
    "8: Note (Ivan Petrov)",
    "(Ivan Petrov)",
])
def test_does_not_guess_new_bounds(text):
    source = "Header {meta}\n" + text
    start = source.index("Ivan")
    assert BoundaryValues(source).resolve(start, start + 4, entity_type="PERSON") is None


@pytest.mark.parametrize("tail", ["\n", "\n9:\nNext section", "\n\nNext section"])
def test_confirmed_trailing_record(tail):
    text = "Header {meta}\n8: Note: (Ivan Petrov)" + tail
    start = text.index("Ivan")
    assert BoundaryValues(text).resolve(start, start + 4) == (start, start + len("Ivan Petrov"))


def test_does_not_join_independent_json_values():
    text = 'Header {meta}\n8: Note: ({"left":"Ivan","right":"Petrov"})'
    start, end = text.index("Ivan"), text.index("Petrov") + len("Petrov")
    assert BoundaryValues(text).resolve(start, end, entity_type="PERSON") is None


def test_candidate_must_contain_initial_and_shifted_evidence():
    text = "Header {meta}\n8: Note: (Ivan Petrov)"
    start, end = text.index("Ivan"), len(text)
    assert BoundaryValues(text).resolve(start, end, entity_type="PERSON") is None


@pytest.mark.parametrize("size", [_MAX_PROSE_CONTAINER_CHARS, _MAX_PROSE_CONTAINER_CHARS + 1])
def test_keeps_the_existing_parenthetical_size_limit(size):
    body = "Ivan " + "a" * (size - len("Ivan "))
    text = f"Header {{meta}}\n8: Note: ({body})"
    start = text.index("Ivan")
    bounds = BoundaryValues(text).resolve(start, start + 4)
    assert bounds == ((start, start + size) if size == _MAX_PROSE_CONTAINER_CHARS else None)


def test_uses_original_pair_across_analyzer_chunks(monkeypatch):
    text = "Header {meta}\n8: Note: (Ivan Petrov\n9: continued description)"
    start, end = text.index("Ivan"), text.rindex(")")
    local = text[:start + len("Ivan")]
    instance, _, _ = recognizer(monkeypatch, [prediction("PERSON", start, len(local), right=True)], [])
    result, = instance.analyze(local, boundary_context=BoundaryContext(BoundaryValues(text)))
    assert (result.start, result.end) == (start, end)
    with pytest.raises(NERProcessingError) as error:
        instance.analyze(local)
    assert error.value.failure_class == "window_boundary_unresolved"
