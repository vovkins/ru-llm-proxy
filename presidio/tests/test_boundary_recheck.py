from types import SimpleNamespace
import threading

import pytest

from presidio.model_profiles import PROFILES
from presidio.ner.boundary_recheck import (
    confirmed_prompt_edges, eligible_prompt, resolve_recheck, prediction_key,
)
from presidio.ner.text_processing import normalize_for_ner
from presidio.ner.triton_recognizer import (
    EntityPrediction, WindowEntityPrediction, UnresolvedWindowEntityCluster,
    TokenPrediction, NERWindowAnalysis, NER_STATE_READY, NERProcessingError,
    TritonNERRecognizer, NERInferenceCancelled,
)


def setup_case(original=None):
    normalized = normalize_for_ner(original or "x" * 400 + "\nbash-$\n" + "y" * 600)
    start = normalized.text.index("-$")
    p = WindowEntityPrediction(EntityPrediction("PASSWORD", start, start + 2, .7), 0, False, True)
    cluster = UnresolvedWindowEntityCluster("PASSWORD", (p,))
    offsets = [(i, i + 1) for i in range(len(normalized.text))]
    return normalized, list(range(len(offsets))), offsets, (cluster,)


def inference(*, score=.98, label="O", malformed=False):
    def infer(_ids, batch_offsets):
        result = [[TokenPrediction(label, a, b, score) for a, b in offsets] for offsets in batch_offsets]
        return result[:1] if malformed else result
    return infer


def confirm(case, *, protected=(), infer=None, positive=None, cancelled=lambda: None):
    normalized, ids, offsets, clusters = case
    return confirmed_prompt_edges(normalized, ids, offsets, clusters, positive or [],
                                  protected, infer or inference(), cancelled)


def test_two_central_O_confirmations_can_withdraw_only_the_edge():
    case = setup_case()
    keys, processed = confirm(case)
    assert keys == {prediction_key(case[3][0].predictions[0])}
    assert processed == 2


@pytest.mark.parametrize("score,label,malformed", [
    (.89, "O", False), (.98, "B-PASSWORD", False),
    (float("nan"), "O", False), (float("inf"), "O", False), (1.1, "O", False),
    (.98, "O", True),
])
def test_incomplete_or_uncertain_evidence_keeps_detection(score, label, malformed):
    assert not confirm(setup_case(), infer=inference(score=score, label=label, malformed=malformed))[0]


def test_no_native_evidence_does_not_relax_refusal():
    assert confirm(setup_case(), protected=None) == (set(), 0)


def test_native_protection_uses_original_coordinates_after_NFC():
    case = setup_case("e\u0301" * 200 + "\nbash-$\n" + "x" * 600)
    a, b = case[0].to_original_span(case[3][0].predictions[0].entity.start,
                                  case[3][0].predictions[0].entity.end)
    assert a != case[3][0].predictions[0].entity.start
    assert confirm(case, protected=((a, b),)) == (set(), 0)


def test_positive_recovery_detection_is_never_removed():
    case = setup_case()
    assert confirm(case, positive=[case[3][0].predictions[0]]) == (set(), 0)


def test_forward_failure_keeps_original_detection():
    def fail(*_):raise RuntimeError("synthetic failure")
    assert not confirm(setup_case(), infer=fail)[0]


def test_missing_character_coverage_preserves_detection():
    case = setup_case()
    start = case[3][0].predictions[0].entity.start
    def incomplete(_ids, batch_offsets):
        return [[TokenPrediction("O", a, b, .99) for a, b in offsets if a != start + 1]
                for offsets in batch_offsets]
    assert not confirm(case, infer=incomplete)[0]


def test_work_budget_is_per_call_and_bounded():
    case = setup_case("x" * 400 + "\n" + "bash-$\n" * 5 + "y" * 600)
    normalized, ids, offsets, _ = case
    starts = [i for i in range(len(normalized.text)) if normalized.text.startswith("-$", i)]
    clusters = tuple(UnresolvedWindowEntityCluster("PASSWORD", (
        WindowEntityPrediction(EntityPrediction("PASSWORD", a, a + 2, .7), index, False, True),
    )) for index, a in enumerate(starts))
    confirmed, processed = confirm((normalized, ids, offsets, clusters))
    assert len(confirmed) == 4
    assert processed == 8


def test_parallel_calls_do_not_share_withdrawn_predictions():
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda native: confirm(setup_case(), protected=native)[0],
                                [(), None] * 8))
    assert [bool(r) for r in results] == [True, False] * 8


def test_cancellation_propagates_after_forward():
    cancelled = threading.Event()
    def infer(*args):
        cancelled.set()
        return inference()(*args)
    def check():
        if cancelled.is_set():raise NERInferenceCancelled()
    with pytest.raises(NERInferenceCancelled):confirm(setup_case(), infer=infer, cancelled=check)


@pytest.mark.parametrize("text", ["password=-$", "password=\nbash-$", "Пароль:\nbash-$",
                                  "-$", "hello-$", "fish>$"])
def test_credentials_and_unknown_punctuation_are_not_exceptions(text):
    start = text.index("$")
    assert not eligible_prompt(text, start, start + 1)


@pytest.mark.parametrize("line,edge", [
    ("bash-$", "-$"), ("  zsh> tool --flag", ">"),
    ("\tfish $ printf hello", "$"), ("sh#", "#"),
])
def test_only_prompt_punctuation_is_eligible_with_original_offsets(line, edge):
    text = "Контекст🙂\n" + line + "\nследующая строка"
    start = text.index(edge)
    assert eligible_prompt(text, start, start + len(edge))


@pytest.mark.parametrize("line,value", [
    ("bash-$ tool '-$'", "-$"),
    ("zsh> tool '>'", ">"),
    ("fish$ tool '$'", "$"),
    ("sh# tool '#'", "#"),
    ("bash-$ tool --option=!?", "!?"),
    ("bash-$ tool -$", "-$"),
])
def test_command_argument_punctuation_is_never_negative_prompt_evidence(line, value):
    start = line.rindex(value)
    assert not eligible_prompt(line, start, start + len(value))


def test_confirm_does_not_withdraw_a_punctuation_secret_inside_command_arguments():
    case = setup_case("x" * 400 + "\nbash-$ tool '-$'\n" + "y" * 600)
    normalized, ids, offsets, _ = case
    start = normalized.text.index("-$'\n")
    prediction = WindowEntityPrediction(EntityPrediction("PASSWORD", start, start + 2, .7), 0, False, True)
    cluster = UnresolvedWindowEntityCluster("PASSWORD", (prediction,))
    assert confirm((normalized, ids, offsets, (cluster,))) == (set(), 0)


def test_disagreeing_centers_do_not_withdraw_a_prompt_detection():
    def disagree(_ids, batch_offsets):
        result = inference()(_ids, batch_offsets)
        return [result[0], [TokenPrediction("B-PASSWORD", p.start, p.end, .98) for p in result[1]]]
    assert not confirm(setup_case(), infer=disagree)[0]


def test_cancellation_before_work_is_not_negative_evidence():
    def cancelled():
        raise NERInferenceCancelled()
    with pytest.raises(NERInferenceCancelled):
        confirm(setup_case(), cancelled=cancelled)


def test_config_keeps_the_e6_default_without_a_public_switch():
    assert resolve_recheck("tiny2") == "off"
    with pytest.raises(ValueError):
        resolve_recheck("bert")


@pytest.mark.parametrize("enabled,protected,success", [(True, (), True), (True, None, False), (False, (), False)])
def test_real_recognizer_merge_is_opt_in_and_requires_native_evidence(monkeypatch, enabled, protected, success):
    monkeypatch.setattr("presidio.ner.triton_recognizer.resolve_recheck",
                        lambda _: "shell-prompt" if enabled else "off")
    normalized, ids, offsets, clusters = setup_case()
    r = TritonNERRecognizer(profile=PROFILES["tiny2"])
    def load():
        r._state = NER_STATE_READY
        r._warmed_up = True
        from types import SimpleNamespace
        r._model = SimpleNamespace(ready=lambda: True)
        r._tokenizer = object()
    monkeypatch.setattr(r, "load_model", load)
    monkeypatch.setattr(r, "_predict_window_entities", lambda *_a, **_k: NERWindowAnalysis(list(clusters[0].predictions), 1, len(ids)))
    monkeypatch.setattr(r, "_recover_window_boundaries", lambda *_a, **_k: ([], 0))
    monkeypatch.setattr(r, "_tokenize_content", lambda _: (ids, offsets))
    monkeypatch.setattr(r, "_infer_window_batch", inference())
    if success:
        assert r.analyze(normalized.original, protected_spans=protected) == []
    else:
        with pytest.raises(NERProcessingError) as exc:r.analyze(normalized.original, protected_spans=protected)
        assert exc.value.failure_class == "window_boundary_unresolved"
