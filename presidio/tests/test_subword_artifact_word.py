"""A lowercase mid-word PERSON fragment cut at its word end masks only that word.

Window geometry is real (``plan_token_windows`` + recovery ranges) over a
one-token-per-character tokenizer; only model predictions are injected.
Every case where the rule must not apply is compared with the same analysis
with the rule disabled: the outcome (masks or refusal) must be identical.
"""
import json
import threading

import pytest

import presidio.ner.triton_recognizer as triton_recognizer
from presidio.ner.boundary_values import BoundaryContext, BoundaryValues
from presidio.ner.triton_recognizer import (
    NER_BOUNDARY_CONTAINER_KEY, EntityPrediction, NERInferenceCancelled,
    NERProcessingError, NERWindowAnalysis, UnresolvedWindowEntityCluster,
    WindowEntityPrediction, _subword_artifact_word,
)
from presidio.tests.test_ner import FakeModel, FakeTokenizer, make_recognizer

WORD = "абвгдеж"  # filler: 7 lowercase letters, same length as "оверлей"


class CharTokenizer(FakeTokenizer):
    def __init__(self):
        super().__init__([])

    def __call__(self, text, **kwargs):
        self.call_kwargs = kwargs
        offsets = [(i, i + 1) for i, c in enumerate(text) if not c.isspace()]
        return {"input_ids": [1000 + i for i in range(len(offsets))], "offset_mapping": offsets}


def numbered_source(lines=40, words=8):
    """Read-file output: numbered records, a blank numbered record every 6 lines."""
    rows, n = ["tool output {meta}", "1: Заголовок", "2:"], 3
    for index in range(lines):
        rows.append(f"{n}: " + " ".join([WORD] * words))
        n += 1
        if index % 6 == 5:
            rows.append(f"{n}:")
            n += 1
    rows += [f"{n}:", "tail [meta]"]
    return "\n".join(rows)


def pred(start, end, window, *, left=False, right=False, score=.98, kind="PERSON"):
    return WindowEntityPrediction(EntityPrediction(kind, start, end, score), window, left, right)


def scenario(monkeypatch, *, word="оверлей", cut=4, kind="PERSON", base=None,
             extra=lambda window, edge, text: [], recovery=lambda window, edge, text: []):
    """Place ``word`` so that it ends exactly at a real internal window end."""
    base = base or numbered_source()
    instance = make_recognizer(tokenizer=CharTokenizer(), model=FakeModel(),
                               span_postprocessing="secrets-contracts")
    _, offsets, windows = instance._prepare_window_input(base)
    window = next(w for w in windows[1:-1]
                  if base[offsets[w.end_token - 1][1] - 7:offsets[w.end_token - 1][1]] == WORD
                  and not base[offsets[w.end_token - 1][1]].isalpha())
    edge = offsets[window.end_token - 1][1]
    text = base[:edge - 7] + word + base[edge:]
    initial = [pred(edge - cut, edge, window.index, right=True, kind=kind),
               *extra(window, edge, text)]
    recovered = list(recovery(window, edge, text))
    monkeypatch.setattr(instance, "_predict_window_entities",
                        lambda t, **kw: NERWindowAnalysis(initial, len(windows), len(t)))
    monkeypatch.setattr(instance, "_recover_window_boundaries",
                        lambda *a, **kw: (recovered, 1 if recovered else 0))
    return instance, text, edge


def outcome(instance, text):
    try:
        results = instance.analyze(text, boundary_context=BoundaryContext(BoundaryValues(text)))
    except NERProcessingError as error:
        return ("refused", error.failure_class)
    return [(r.entity_type, r.start, r.end, text[r.start:r.end]) for r in results]


def disabled(monkeypatch, instance, text):
    with monkeypatch.context() as patch:
        patch.setattr(triton_recognizer, "_subword_artifact_word", lambda *a, **kw: None)
        return outcome(instance, text)


def test_lowercase_midword_fragment_masks_only_its_word(monkeypatch):
    instance, text, edge = scenario(monkeypatch)
    results = instance.analyze(text, boundary_context=BoundaryContext(BoundaryValues(text)))
    assert [(r.entity_type, r.start, r.end) for r in results] == [("PERSON", edge - 7, edge)]
    assert text[edge - 7:edge] == "оверлей"
    assert results[0].score == pytest.approx(.98)
    assert results[0].recognition_metadata[NER_BOUNDARY_CONTAINER_KEY] is True
    # Without the rule the same input is masked far more widely (or refused).
    before = disabled(monkeypatch, instance, text)
    assert before[0] == "refused" or before[0][2] - before[0][1] > 7


def unchanged(monkeypatch, instance, text):
    after = outcome(instance, text)
    assert after == disabled(monkeypatch, instance, text)
    return after


@pytest.mark.parametrize("word", ["Иваново", "Петрова", "Оверлей", "Ёлкиной"])
def test_capitalized_word_keeps_previous_outcome(monkeypatch, word):
    instance, text, edge = scenario(monkeypatch, word=word)
    after = unchanged(monkeypatch, instance, text)
    assert after[0] == "refused" or (after[0][1] <= edge - 7 and after[0][2] > edge)


def test_fragment_at_word_start_is_a_name_start_not_an_artifact(monkeypatch):
    instance, text, edge = scenario(monkeypatch, word="иванова", cut=7)
    after = unchanged(monkeypatch, instance, text)
    assert after[0] == "refused" or after[0][2] > edge


@pytest.mark.parametrize("kind", ["ORGANIZATION", "LOCATION", "LOGIN", "PASSWORD",
                                  "AUTH_TOKEN", "SECRET_KEY", "CONTRACT_NUMBER"])
def test_other_entity_types_keep_previous_outcome(monkeypatch, kind):
    instance, text, _ = scenario(monkeypatch, kind=kind)
    unchanged(monkeypatch, instance, text)


def next_word_start(text, edge):
    return text.index(WORD, edge)


def test_person_on_next_word_from_another_window_keeps_previous_outcome(monkeypatch):
    # A real lowercase name may continue: "иваново петров". The other window sees it.
    def extra(window, edge, text):
        start = next_word_start(text, edge)
        return [pred(start, start + 7, window.index + 1)]
    instance, text, edge = scenario(monkeypatch, word="иваново", extra=extra)
    after = unchanged(monkeypatch, instance, text)
    start = next_word_start(text, edge)
    assert after[0] == "refused" or any(s[1] <= edge - 7 and s[2] >= start + 7 for s in after)


def test_person_from_recovery_keeps_previous_outcome(monkeypatch):
    def recovery(window, edge, text):
        return [pred(edge - 7, next_word_start(text, edge) + 7, 10_000)]
    instance, text, _ = scenario(monkeypatch, recovery=recovery)
    unchanged(monkeypatch, instance, text)


def test_fragment_crossing_a_non_letter_keeps_previous_outcome(monkeypatch):
    instance, text, _ = scenario(monkeypatch, word="ове-лей", cut=5)
    unchanged(monkeypatch, instance, text)


def test_short_text_without_recovery_window_keeps_previous_outcome(monkeypatch):
    text = "1: Заголовок\n2:\n3: код (оверлей\n4: Android: ссылки).\n5:\n"
    a = text.index("оверлей") + 3
    instance = make_recognizer(tokenizer=CharTokenizer(), model=FakeModel(),
                               span_postprocessing="secrets-contracts")
    monkeypatch.setattr(instance, "_predict_window_entities",
                        lambda t, **kw: NERWindowAnalysis([pred(a, a + 4, 0, right=True)], 2, len(t)))
    monkeypatch.setattr(instance, "_recover_window_boundaries", lambda *a, **kw: ([], 0))
    after = unchanged(monkeypatch, instance, text)
    assert after[0] == "refused" or after[0][3] != "оверлей"


def test_independent_login_inside_the_following_path_survives(monkeypatch):
    def extra(window, edge, text):
        start = text.index(WORD, edge + 20)
        return [pred(start, start + 7, window.index + 1, kind="LOGIN")]
    instance, text, edge = scenario(monkeypatch, extra=extra)
    start = text.index(WORD, edge + 20)
    text = text[:start] + "olgasmi" + text[start + 7:]
    after = outcome(instance, text)
    assert ("PERSON", edge - 7, edge, "оверлей") in after
    assert ("LOGIN", start, start + 7, "olgasmi") in after


def test_json_literal_container_from_the_old_resolver_is_unchanged(monkeypatch):
    base = json.dumps({"description": " ".join([WORD] * 120)}, ensure_ascii=False)
    instance = make_recognizer(tokenizer=CharTokenizer(), model=FakeModel(),
                               span_postprocessing="secrets-contracts")
    _, offsets, windows = instance._prepare_window_input(base)
    edge = offsets[windows[0].end_token - 1][1]
    text = base[:edge - 4] + "рлей" + base[edge:]
    initial = [pred(edge - 4, edge, 0, right=True)]
    monkeypatch.setattr(instance, "_predict_window_entities",
                        lambda t, **kw: NERWindowAnalysis(initial, len(windows), len(t)))
    monkeypatch.setattr(instance, "_recover_window_boundaries", lambda *a, **kw: ([], 0))
    values = BoundaryValues(text)
    assert values.is_original_container(edge - 4, edge)
    after = unchanged(monkeypatch, instance, text)
    assert [(s[1], s[2]) for s in after] == [values.resolve(edge - 4, edge)]
    json.loads(text)


def test_the_word_rule_does_not_bypass_cancellation(monkeypatch):
    instance, text, _ = scenario(monkeypatch)
    event = threading.Event()
    original = instance._recovery_covered_spans

    def covered(*args):
        event.set()
        return original(*args)
    monkeypatch.setattr(instance, "_recovery_covered_spans", covered)
    with pytest.raises(NERInferenceCancelled):
        instance.analyze(text, boundary_context=BoundaryContext(BoundaryValues(text)),
                         cancellation_event=event)
    assert instance.is_ready()


# Pure helper: condition matrix, including the left-cut mirror.
TEXT = "код (оверлей Android: ссылки) и Иван Петров"


def cluster(start, end, *, left=False, right=False, kind="PERSON"):
    return UnresolvedWindowEntityCluster(kind, (pred(start, end, 0, left=left, right=right, kind=kind),))


def helper(c, *, covered=((0, len(TEXT)),), combined=None, recovered=()):
    return _subword_artifact_word(TEXT, c, set(recovered),
                                  list(combined or c.predictions), lambda: list(covered))


def test_helper_right_cut_and_left_cut_mirror():
    w = TEXT.index("оверлей")
    assert helper(cluster(w + 3, w + 7, right=True)) == (w, w + 7)
    assert helper(cluster(w, w + 3, left=True)) == (w, w + 7)


@pytest.mark.parametrize("make", [
    lambda w: cluster(w + 3, w + 7, right=True, left=True),      # both sides cut
    lambda w: cluster(w + 3, w + 7),                             # nothing cut
    lambda w: cluster(w + 3, w + 6, right=True),                 # cut not at word end
    lambda w: cluster(w, w + 7, right=True),                     # starts at word start
    lambda w: cluster(w + 3, w + 7, right=True, kind="LOGIN"),
])
def test_helper_refuses_other_geometries(make):
    assert helper(make(TEXT.index("оверлей"))) is None


def test_helper_needs_recovery_coverage_of_the_continuation():
    w = TEXT.index("оверлей")
    c = cluster(w + 3, w + 7, right=True)
    assert helper(c, covered=[(0, w + 10)]) is None          # next words not seen
    assert helper(c, covered=[(w + 1, len(TEXT))]) is None   # word itself not fully seen
    assert helper(c, covered=[]) is None


def test_helper_capitalized_names_and_recovery_evidence():
    i = TEXT.index("Иван")
    assert helper(cluster(i + 1, i + 4, right=True)) is None
    w = TEXT.index("оверлей")
    c = cluster(w + 3, w + 7, right=True)
    assert helper(c, recovered=c.predictions) is None
    other = pred(TEXT.index("Android"), TEXT.index("Android") + 7, 1)
    assert helper(c, combined=[*c.predictions, other]) is None
    login = pred(TEXT.index("Android"), TEXT.index("Android") + 7, 1, kind="LOGIN")
    assert helper(c, combined=[*c.predictions, login]) == (w, w + 7)
