"""Tests for the pinned Hugging Face NER adapter."""

from __future__ import annotations

import logging
import threading
import unicodedata
from types import SimpleNamespace
from unittest.mock import patch

import pytest

pytest.importorskip("presidio_analyzer")
import numpy as np

from presidio.model_artifact import ModelManifest
from presidio.entity_types import NER_ENTITY_TYPES
from presidio.ner.triton_recognizer import (
    EXPECTED_ID2LABEL,
    MAX_CONTENT_TOKENS,
    MODEL_ID,
    MODEL_REVISION,
    EntityPrediction,
    TritonNERRecognizer,
    NERConfigurationError,
    NERInferenceTelemetry,
    NERInferenceCancelled,
    NERProcessingError,
    NERUnavailableError,
    NERWindowBoundaryError,
    TokenPrediction,
    WindowEntityPrediction,
    decode_bio_predictions,
    merge_window_predictions,
    refine_organization_quotes,
    should_run_ner,
    _filter_window_predictions,
)
from presidio.result_merging import (
    DETECTION_SOURCE_METADATA_KEY,
    NER_MODEL_METADATA_KEY,
    SOURCE_NER,
)


def test_name_support_filter_retains_only_complete_login_within_explicit_value():
    predictions = [
        WindowEntityPrediction(EntityPrediction("LOGIN", 5, 9, 0.8), 0, False, False),
        WindowEntityPrediction(EntityPrediction("LOGIN", 10, 16, 0.8), 0, False, True),
        WindowEntityPrediction(EntityPrediction("LOGIN", 19, 24, 0.8), 0, False, False),
        WindowEntityPrediction(EntityPrediction("LOGIN", 4, 9, 0.8), 0, False, False),
        WindowEntityPrediction(EntityPrediction("LOGIN", 11, 15, 0.2), 0, False, False),
        WindowEntityPrediction(EntityPrediction("PASSWORD", 5, 9, 0.8), 0, False, False),
    ]
    assert _filter_window_predictions(
        predictions, score_threshold=0.35, requested_entities={"PERSON", "ORGANIZATION"},
        supporting_name_spans=((5, 16),),
    ) == predictions[:1]


def test_name_support_does_not_change_requested_boundary_or_empty_filter_semantics():
    prediction = WindowEntityPrediction(EntityPrediction("LOGIN", 5, 9, 0.8), 0, False, True)
    for requested in (None, {"LOGIN"}):
        assert _filter_window_predictions(
            [prediction], score_threshold=0.35, requested_entities=requested,
            supporting_name_spans=((5, 16),),
        ) == [prediction]


def test_adapter_retains_local_support_without_second_forward_pass(monkeypatch):
    text = "ФИО: ИВАН ПЕТРОВ. login=account"
    recognizer = make_recognizer(tokenizer=FakeTokenizer([(0, 1)]), model=FakeModel())
    predictions = [
        WindowEntityPrediction(EntityPrediction("PERSON", 5, 9, 0.8), 0, False, False),
        WindowEntityPrediction(EntityPrediction("LOGIN", 10, 16, 0.8), 0, False, False),
        WindowEntityPrediction(EntityPrediction("LOGIN", 24, 31, 0.8), 0, False, False),
    ]
    calls = []

    def predict(text, **kwargs):
        calls.append(text)
        return predictions, 1

    monkeypatch.setattr(recognizer, "_predict_window_entities", predict)
    results = recognizer.analyze(text, entities=["PERSON", "ORGANIZATION"])
    assert [(r.entity_type, r.start, r.end) for r in results] == [
        ("PERSON", 5, 9), ("LOGIN", 10, 16),
    ]
    assert len(calls) == 1


def test_truncated_unrequested_login_does_not_trigger_boundary_recovery(monkeypatch):
    text = "ФИО: ИВАН ПЕТРОВ."
    recognizer = make_recognizer(tokenizer=FakeTokenizer([(0, 1)]), model=FakeModel())
    predictions = [
        WindowEntityPrediction(EntityPrediction("PERSON", 5, 9, 0.8), 0, False, False),
        WindowEntityPrediction(EntityPrediction("LOGIN", 10, 16, 0.8), 0, False, True),
    ]
    monkeypatch.setattr(recognizer, "_predict_window_entities", lambda *_a, **_kw: (predictions, 1))
    monkeypatch.setattr(recognizer, "_recover_window_boundaries", lambda *_a, **_kw: pytest.fail("unrequested boundary recovery"))
    results = recognizer.analyze(text, entities=["PERSON", "ORGANIZATION"])
    assert [(r.entity_type, r.start, r.end) for r in results] == [("PERSON", 5, 9)]
    assert recognizer.is_ready()


def make_recognizer(**kwargs):
    if "profile" not in kwargs:
        kwargs.setdefault("o_logit_bias", 0.0)
        kwargs.setdefault("span_postprocessing", "none")
    return TritonNERRecognizer(**kwargs)


class FakeTokenizer:
    is_fast = True

    def __init__(self, offsets):
        self.offsets = offsets
        self.call_kwargs = None
        self.prepared_input_ids = []

    def __len__(self):
        return 3

    def __call__(self, _text, **kwargs):
        self.call_kwargs = kwargs
        return {
            "input_ids": [1000 + index for index in range(len(self.offsets))],
            "offset_mapping": list(self.offsets),
        }

    def build_inputs_with_special_tokens(self, input_ids):
        self.prepared_input_ids.append(list(input_ids))
        return [101, *input_ids, 102]

    def create_token_type_ids_from_sequences(self, input_ids):
        return [0] * (len(input_ids) + 2)


def test_model_labels_match_public_entity_contract():
    model_entity_types = {
        label.partition("-")[2]
        for label in EXPECTED_ID2LABEL.values()
        if label != "O"
    }

    assert model_entity_types == NER_ENTITY_TYPES


class FakeModel:
    def __init__(self, label_ids=None, *, error=None):
        self.config = SimpleNamespace(
            architectures=["BertForTokenClassification"],
            id2label=dict(EXPECTED_ID2LABEL),
        )
        self.label_ids = label_ids or [0]
        self.error = error
        self.call_count = 0
        self.input_lengths = []
        self.max_batch_size = 32
        self.graph_digest = "c" * 64
        self.client = SimpleNamespace(close=lambda: None)

    def ready(self):
        return True

    def close(self):
        self.client.close()

    def __call__(self, **kwargs):
        self.call_count += 1
        self.input_lengths.append(len(kwargs["input_ids"][0]))
        if self.error is not None:
            raise self.error
        logits = np.full((1, len(self.label_ids), len(EXPECTED_ID2LABEL)), -10.0, dtype=np.float32)
        for index, label_id in enumerate(self.label_ids):
            logits[0, index, label_id] = 10.0
        return SimpleNamespace(logits=logits)


@pytest.mark.parametrize("size", [0, -1, 33])
def test_runtime_configuration_rejects_invalid_batch_size(size):
    with pytest.raises(ValueError, match="between 1 and 32"):
        make_recognizer(inference_batch_size=size)


def test_only_runtime_is_cpu_triton():
    r = TritonNERRecognizer()
    info = r.runtime_info()
    assert info["backend"] == "triton" and info["device"] == "cpu" and info["precision"] == "fp32"
    assert info["model_profile"] == "tiny2" and info["o_logit_bias"] == 1.0
    assert not any("cuda" in k or "gpu" in k for k in info)
    for option in ("device_profile", "gpu_precision"):
        with pytest.raises(TypeError):
            TritonNERRecognizer(**{option: "gpu"})


class GlobalLabelModel(FakeModel):
    """Return labels based on the global content-token ids from FakeTokenizer."""

    def __init__(self, labels_by_token=None):
        super().__init__()
        self.labels_by_token = labels_by_token or {}

    def __call__(self, **kwargs):
        self.call_count += 1
        input_batches = kwargs["input_ids"].tolist()
        self.input_lengths.append(len(input_batches[0]))
        logits = np.full(
            (
                len(input_batches),
                len(input_batches[0]),
                len(EXPECTED_ID2LABEL),
            ),
            -10.0, dtype=np.float32,
        )
        for batch_index, input_ids in enumerate(input_batches):
            label_ids = [
                self.labels_by_token.get(token_id - 1000, 0)
                if token_id >= 1000
                else 0
                for token_id in input_ids
            ]
            for index, label_id in enumerate(label_ids):
                logits[batch_index, index, label_id] = 10.0
        return SimpleNamespace(logits=logits)


class FailOnCallModel(GlobalLabelModel):
    def __init__(self, fail_on_call, labels_by_token=None):
        super().__init__(labels_by_token)
        self.fail_on_call = fail_on_call

    def __call__(self, **kwargs):
        if self.call_count + 1 == self.fail_on_call:
            self.call_count += 1
            raise RuntimeError("inference failed")
        return super().__call__(**kwargs)


def _recognizer_for(labels, token_offsets):
    tokenizer = FakeTokenizer(token_offsets)
    model = FakeModel([0, *labels, 0])
    return make_recognizer(tokenizer=tokenizer, model=model), tokenizer, model


def test_window_batching_uses_one_forward_pass_for_multiple_windows():
    token_count = MAX_CONTENT_TOKENS * 2
    text = "x" * token_count
    offsets = [(index, index + 1) for index in range(token_count)]
    tokenizer = FakeTokenizer(offsets)
    model = GlobalLabelModel()
    recognizer = make_recognizer(
        tokenizer=tokenizer,
        model=model,
        inference_batch_size=4,
    )
    telemetry = []

    assert recognizer.analyze(text, telemetry_callback=telemetry.append) == []

    assert model.call_count == 1
    assert model.input_lengths == [MAX_CONTENT_TOKENS + 2]
    assert telemetry[0].windows_processed == 3
    assert telemetry[0].input_tokens == token_count


def test_cancellation_between_batches_does_not_poison_backend():
    token_count = MAX_CONTENT_TOKENS * 2
    text = "x" * token_count
    offsets = [(index, index + 1) for index in range(token_count)]
    cancellation_event = threading.Event()
    tokenizer = FakeTokenizer(offsets)

    class CancelAfterFirstBatch(GlobalLabelModel):
        def __call__(self, **kwargs):
            result = super().__call__(**kwargs)
            cancellation_event.set()
            return result

    model = CancelAfterFirstBatch()
    recognizer = make_recognizer(
        tokenizer=tokenizer,
        model=model,
        inference_batch_size=1,
    )

    with pytest.raises(NERInferenceCancelled):
        recognizer.analyze(text, cancellation_event=cancellation_event)

    assert model.call_count == 1
    assert recognizer.is_ready() is True
    assert recognizer.failure_phase() is None
    assert recognizer.failure_class() is None


def _manifest():
    return ModelManifest(
        model_id=MODEL_ID,
        revision=MODEL_REVISION,
        architecture="BertForTokenClassification",
        license="apache-2.0",
        base_model="cointegrated/rubert-tiny2",
        files=(),
    )


def _character_offsets(count):
    return [(index, index + 1) for index in range(count)]


@pytest.mark.parametrize(
    "entity_type",
    [
        "PERSON",
        "LOCATION",
        "ORGANIZATION",
        "LOGIN",
        "PASSWORD",
        "AUTH_TOKEN",
        "SECRET_KEY",
        "CONTRACT_NUMBER",
    ],
)
def test_decode_supports_all_model_entity_types(entity_type):
    entities = decode_bio_predictions(
        [
            TokenPrediction(f"B-{entity_type}", 2, 5, 0.98),
            TokenPrediction(f"I-{entity_type}", 5, 8, 0.91),
        ]
    )

    assert entities == [EntityPrediction(entity_type, 2, 8, 0.91)]


def test_decode_treats_malformed_i_transition_as_new_entity():
    entities = decode_bio_predictions(
        [
            TokenPrediction("I-PERSON", 0, 4, 0.9),
            TokenPrediction("I-LOCATION", 5, 11, 0.8),
        ]
    )

    assert entities == [
        EntityPrediction("PERSON", 0, 4, 0.9),
        EntityPrediction("LOCATION", 5, 11, 0.8),
    ]


def test_analyze_uses_offset_mapping_and_real_confidence():
    text = "Иван из Москвы"
    recognizer, tokenizer, model = _recognizer_for(
        [1, 0, 3, 4],
        [(0, 4), (5, 7), (8, 12), (12, 14)],
    )

    results = recognizer.analyze(text)

    assert [(result.entity_type, result.start, result.end) for result in results] == [
        ("PERSON", 0, 4),
        ("LOCATION", 8, 14),
    ]
    assert all(result.score > 0.99 for result in results)
    assert all(
        result.recognition_metadata[DETECTION_SOURCE_METADATA_KEY] == SOURCE_NER
        for result in results
    )
    assert all(
        result.recognition_metadata[NER_MODEL_METADATA_KEY] == MODEL_ID
        for result in results
    )
    assert tokenizer.call_kwargs["add_special_tokens"] is False
    assert tokenizer.call_kwargs["padding"] is False
    assert tokenizer.call_kwargs["return_offsets_mapping"] is True
    assert tokenizer.call_kwargs["truncation"] is False
    assert model.call_count == 1


def test_inference_ignores_predictions_for_bert_special_tokens():
    tokenizer = FakeTokenizer([(0, 4)])
    model = FakeModel([1, 0, 3])
    recognizer = make_recognizer(tokenizer=tokenizer, model=model)

    assert recognizer.analyze("Иван") == []


def test_wordpiece_offsets_and_punctuation_form_one_exact_login_span():
    text = "login=ops-admin_2."
    recognizer, _, _ = _recognizer_for(
        [0, 0, 7, 8, 8, 8, 8, 0],
        [
            (0, 5),
            (5, 6),
            (6, 9),
            (9, 10),
            (10, 15),
            (15, 16),
            (16, 17),
            (17, 18),
        ],
    )

    results = recognizer.analyze(text)

    assert [(result.entity_type, result.start, result.end) for result in results] == [
        ("LOGIN", 6, 17)
    ]
    assert text[results[0].start : results[0].end] == "ops-admin_2"


def test_successful_analysis_emits_bounded_inference_telemetry():
    recognizer, _, _ = _recognizer_for([1], [(0, 4)])
    events = []

    recognizer.analyze("Иван", telemetry_callback=events.append)

    assert len(events) == 1
    assert events[0].outcome == "success"
    assert events[0].duration_seconds >= 0
    assert events[0].windows_processed == 1
    assert events[0].input_tokens == 1
    assert events[0].failure_phase is None
    assert events[0].failure_class is None


def test_invalid_inference_telemetry_is_rejected():
    with pytest.raises(ValueError, match="unsupported"):
        NERInferenceTelemetry("unknown", 0.1, 1)
    with pytest.raises(ValueError, match="duration"):
        NERInferenceTelemetry("success", -0.1, 1)
    with pytest.raises(ValueError, match="window"):
        NERInferenceTelemetry("success", 0.1, -1)
    with pytest.raises(ValueError, match="input-token"):
        NERInferenceTelemetry("success", 0.1, 1, input_tokens=-1)


def test_repeated_values_keep_tokenizer_offsets():
    text = "Иван встретил Иван"
    recognizer, _, _ = _recognizer_for(
        [1, 0, 1],
        [(0, 4), (5, 13), (14, 18)],
    )

    results = recognizer.analyze(text)

    assert [(result.start, result.end) for result in results] == [(0, 4), (14, 18)]


@pytest.mark.parametrize("quotes", [("«", "»"), ('"', '"'), ("„", "“"), ("“", "”")])
def test_organization_quotes_join_only_already_detected_name(quotes):
    opening, closing = quotes
    text = f"Компания {opening}Северный Контур{closing} открыла счёт."
    start = text.index(opening)
    end = text.index(closing, start + 1) + 1
    predictions = [
        EntityPrediction("ORGANIZATION", start, start + 1, 0.6),
        EntityPrediction("ORGANIZATION", start + 1, end - 1, 0.8),
    ]

    assert refine_organization_quotes(text, predictions) == [
        EntityPrediction("ORGANIZATION", start, end, 0.6)
    ]


@pytest.mark.parametrize(
    ("text", "values", "types"),
    [
        ("Компания «Северный Контур».", ["Северный"], ["ORGANIZATION"]),
        ("Компания «Северный Контур».", ["Контур"], ["ORGANIZATION"]),
        ("Компания «Иван Петров».", ["Иван", "Петров"], ["ORGANIZATION", "PERSON"]),
        ("Компания «LOGIN=service.user».", ["service.user"], ["LOGIN"]),
        ("Компания «MODEL_OK».", [], []),
        ("Компания «Северный Контур\".", ["Северный Контур"], ["ORGANIZATION"]),
        ("Компания «Северный\nКонтур».", ["Северный\nКонтур"], ["ORGANIZATION"]),
        ("Компания «12345678».", ["12345678"], ["ORGANIZATION"]),
        ("Компания «Северный» и «Контур».", ["Северный» и «Контур"], ["ORGANIZATION"]),
    ],
)
def test_organization_quotes_do_not_invent_words_or_merge_other_types(text, values, types):
    predictions = [
        EntityPrediction(entity_type, text.index(value), text.index(value) + len(value), 0.8)
        for value, entity_type in zip(values, types)
    ]
    assert refine_organization_quotes(text, predictions) == predictions


def test_organization_quotes_keep_legal_form_and_separate_names():
    text = 'ООО «Вектор» и компания «Северный Контур».'
    first_end = text.index('»') + 1
    second_start = text.index('«', first_end)
    second_end = text.index('»', second_start) + 1
    predictions = [
        EntityPrediction("ORGANIZATION", 0, first_end - 1, 0.9),
        EntityPrediction("ORGANIZATION", second_start + 1, second_end - 1, 0.8),
    ]
    assert refine_organization_quotes(text, predictions) == [
        EntityPrediction("ORGANIZATION", 0, first_end, 0.9),
        EntityPrediction("ORGANIZATION", second_start, second_end, 0.8),
    ]


def test_organization_quotes_join_word_spans_but_not_unmasked_separator():
    text = 'Компания «Северный Контур».'
    start = text.index('«')
    end = text.index('»') + 1
    predictions = [
        EntityPrediction("ORGANIZATION", start + 1, start + 9, 0.8),
        EntityPrediction("ORGANIZATION", start + 10, end - 1, 0.7),
    ]
    assert refine_organization_quotes(text, predictions) == [
        EntityPrediction("ORGANIZATION", start, end, 0.7)
    ]
    text = text.replace("Северный Контур", "Северный;Контур")
    assert refine_organization_quotes(text, predictions) == predictions


@pytest.mark.parametrize("entities", [None, ["ORGANIZATION"], ["PERSON"]])
def test_quote_refinement_preserves_filters_threshold_and_one_forward_pass(entities):
    text = "Компания «Вектор»."
    start = text.index("Вектор")
    recognizer, _, model = _recognizer_for([5], [(start, start + 6)])
    results = recognizer.analyze(text, entities=entities, score_threshold=0.35)
    assert [(result.entity_type, result.start, result.end) for result in results] == (
        [] if entities == ["PERSON"] else [("ORGANIZATION", start - 1, start + 7)]
    )
    assert model.call_count == 1


def test_quote_refinement_does_not_restore_below_threshold_words(monkeypatch):
    text = "Компания «Северный Контур»."
    recognizer, _, _ = _recognizer_for([0], [(0, 1)])
    predictions = [
        WindowEntityPrediction(EntityPrediction("ORGANIZATION", 10, 18, 0.9), 0, False, False),
        WindowEntityPrediction(EntityPrediction("ORGANIZATION", 19, 25, 0.349999), 0, False, False),
    ]
    monkeypatch.setattr(recognizer, "_predict_window_entities", lambda _text, **_kw: (predictions, 1))
    results = recognizer.analyze(text, score_threshold=0.35)
    assert [(result.start, result.end) for result in results] == [(10, 18)]


@pytest.mark.parametrize("entities", [None, ["ORGANIZATION"]])
def test_quote_refinement_keeps_conflicts_even_when_other_type_is_not_requested(monkeypatch, entities):
    text = 'Компания «Вектор».'
    recognizer, _, _ = _recognizer_for([0], [(0, 1)])
    predictions = [
        WindowEntityPrediction(EntityPrediction("ORGANIZATION", 10, 16, 0.9), 0, False, False),
        WindowEntityPrediction(EntityPrediction("LOGIN", 10, 16, 0.9), 0, False, False),
    ]
    monkeypatch.setattr(recognizer, "_predict_window_entities", lambda _text, **_kw: (predictions, 1))
    results = recognizer.analyze(text, entities=entities)
    assert [(result.start, result.end) for result in results if result.entity_type == "ORGANIZATION"] == [(10, 16)]


def test_quote_refinement_failure_is_request_scoped_and_explicit():
    text = 'Компания «Вектор».'
    recognizer, _, _ = _recognizer_for([5], [(10, 16)])
    with patch(
        "presidio.ner.triton_recognizer.refine_organization_quotes",
        side_effect=ValueError("synthetic processing failure"),
    ):
        with pytest.raises(NERProcessingError) as error:
            recognizer.analyze(text)
    assert error.value.phase == "decoding"
    assert error.value.failure_class == "bio_decoding_failed"
    assert recognizer.is_ready()
    assert [(result.start, result.end) for result in recognizer.analyze(text)] == [(9, 17)]


def test_score_threshold_uses_unrounded_span_minimum(monkeypatch):
    recognizer, _, _ = _recognizer_for([13], [(0, 6)])
    below = WindowEntityPrediction(
        EntityPrediction("SECRET_KEY", 0, 6, 0.3499999), 0, False, False
    )
    at_threshold = WindowEntityPrediction(
        EntityPrediction("SECRET_KEY", 0, 6, 0.35), 0, False, False
    )

    monkeypatch.setattr(
        recognizer,
        "_predict_window_entities",
        lambda _text, **_kwargs: ([below], 1),
    )
    assert recognizer.analyze("secret", score_threshold=0.35) == []

    monkeypatch.setattr(
        recognizer,
        "_predict_window_entities",
        lambda _text, **_kwargs: ([at_threshold], 1),
    )
    assert len(recognizer.analyze("secret", score_threshold=0.35)) == 1


def test_requested_entities_filter_results():
    text = "Иван Москва"
    recognizer, _, _ = _recognizer_for(
        [1, 3],
        [(0, 4), (5, 11)],
    )

    results = recognizer.analyze(text, entities=["LOCATION"])

    assert [result.entity_type for result in results] == ["LOCATION"]


def test_requested_entities_and_score_threshold_are_applied_together(monkeypatch):
    recognizer, _, _ = _recognizer_for([0], [(0, 6)])
    predictions = [
        WindowEntityPrediction(
            EntityPrediction("LOCATION", 0, 6, 0.3499999), 0, False, False
        ),
        WindowEntityPrediction(
            EntityPrediction("SECRET_KEY", 0, 6, 0.99), 0, False, False
        ),
    ]
    monkeypatch.setattr(
        recognizer,
        "_predict_window_entities",
        lambda _text, **_kwargs: (predictions, 1),
    )

    assert recognizer.analyze(
        "Москва",
        entities=["LOCATION"],
        score_threshold=0.35,
    ) == []


def test_irrelevant_requested_entities_skip_model():
    recognizer, _, model = _recognizer_for([1], [(0, 9)])
    events = []

    results = recognizer.analyze(
        "770801001",
        entities=["RU_KPP"],
        telemetry_callback=events.append,
    )

    assert results == []
    assert model.call_count == 0
    assert should_run_ner(["RU_KPP"], 0.35) is False
    assert should_run_ner(["CONTRACT_NUMBER"], 0.99) is True
    assert len(events) == 1
    assert events[0].outcome == "skipped"
    assert events[0].windows_processed == 0
    assert events[0].input_tokens == 0


def test_long_input_is_processed_in_overlapping_windows():
    token_count = MAX_CONTENT_TOKENS + 1
    tokenizer = FakeTokenizer(_character_offsets(token_count))
    model = GlobalLabelModel({token_count - 1: 1})
    recognizer = make_recognizer(tokenizer=tokenizer, model=model)

    results = recognizer.analyze("x" * token_count)

    assert [(result.entity_type, result.start, result.end) for result in results] == [
        ("PERSON", token_count - 1, token_count)
    ]
    assert model.call_count == 1
    assert model.input_lengths == [MAX_CONTENT_TOKENS + 2]
    assert [len(ids) for ids in tokenizer.prepared_input_ids] == [
        MAX_CONTENT_TOKENS,
        65,
    ]


def test_sensitive_value_at_end_of_third_window_is_not_lost():
    token_count = (MAX_CONTENT_TOKENS * 2) + 1
    tokenizer = FakeTokenizer(_character_offsets(token_count))
    model = GlobalLabelModel({token_count - 1: 13})
    recognizer = make_recognizer(tokenizer=tokenizer, model=model)

    results = recognizer.analyze("x" * token_count)

    assert [(result.entity_type, result.start, result.end) for result in results] == [
        ("SECRET_KEY", token_count - 1, token_count)
    ]
    assert model.call_count == 1


def test_entity_crossing_window_boundary_is_emitted_once():
    token_count = MAX_CONTENT_TOKENS + 3
    labels = {382: 1, 383: 2, 384: 2, 385: 2}
    tokenizer = FakeTokenizer(_character_offsets(token_count))
    model = GlobalLabelModel(labels)
    recognizer = make_recognizer(tokenizer=tokenizer, model=model)

    results = recognizer.analyze("x" * token_count)

    assert [(result.entity_type, result.start, result.end) for result in results] == [
        ("PERSON", 382, 386)
    ]


def test_value_repair_after_window_merge_preserves_unicode_source_offsets():
    prefix = "Сергеи\u0306 🙂 "
    normalized_prefix = unicodedata.normalize("NFC", prefix)
    padding = "x " * ((370 - len(normalized_prefix)) // 2)
    text = prefix + padding + " PASSWORD=abc/def+ghi!? next=visible"
    normalized = unicodedata.normalize("NFC", text)
    start = normalized.index("def")
    tokenizer = FakeTokenizer(_character_offsets(len(normalized)))
    model = GlobalLabelModel({start: 9, start + 1: 10, start + 2: 10})
    recognizer = make_recognizer(
        tokenizer=tokenizer, model=model, span_postprocessing="secrets-contracts",
    )

    results = recognizer.analyze(text, entities=["PASSWORD"])

    assert len(tokenizer.prepared_input_ids) > 1
    assert [(r.entity_type, text[r.start:r.end]) for r in results] == [
        ("PASSWORD", "abc/def+ghi!?"),
    ]
    assert text[results[0].end:] == " next=visible"
    assert text[:results[0].start].startswith(prefix)


def test_shifted_window_recovers_entity_longer_than_overlap():
    token_count = 500
    labels = {300: 1, **{index: 2 for index in range(301, 451)}}
    tokenizer = FakeTokenizer(_character_offsets(token_count))
    model = GlobalLabelModel(labels)
    recognizer = make_recognizer(tokenizer=tokenizer, model=model)
    events = []

    results = recognizer.analyze(
        "x" * token_count,
        telemetry_callback=events.append,
    )

    assert [(result.entity_type, result.start, result.end) for result in results] == [
        ("PERSON", 300, 451)
    ]
    assert model.call_count == 2
    assert model.input_lengths == [386, 386]
    assert [len(ids) for ids in tokenizer.prepared_input_ids] == [384, 180, 384]
    assert len(events) == 1
    assert events[0].outcome == "success"
    assert events[0].windows_processed == 3
    assert events[0].input_tokens == token_count
    assert recognizer.is_ready() is True


def test_unresolved_boundary_is_request_scoped_and_next_request_succeeds():
    token_count = 500
    labels = {0: 1, **{index: 2 for index in range(1, token_count)}}
    tokenizer = FakeTokenizer(_character_offsets(token_count))
    model = GlobalLabelModel(labels)
    recognizer = make_recognizer(tokenizer=tokenizer, model=model)
    events = []

    with pytest.raises(NERProcessingError) as exc_info:
        recognizer.analyze(
            "x" * token_count,
            telemetry_callback=events.append,
        )

    assert exc_info.value.phase == "windowing"
    assert exc_info.value.failure_class == "window_boundary_unresolved"
    assert exc_info.value.windows_processed == 3
    assert recognizer.state() == "ready"
    assert recognizer.is_loaded() is True
    assert recognizer.is_warmed_up() is True
    assert recognizer.failure_phase() is None
    assert recognizer.failure_class() is None
    assert len(events) == 1
    assert events[0].outcome == "failure"
    assert events[0].windows_processed == 3
    assert events[0].failure_phase == "windowing"
    assert events[0].failure_class == "window_boundary_unresolved"

    tokenizer.offsets = _character_offsets(2)
    model.labels_by_token = {}
    assert recognizer.analyze("ok") == []
    assert model.call_count == 3
    assert recognizer.is_ready() is True


def test_recovery_forward_failure_marks_backend_failed():
    token_count = 500
    labels = {300: 1, **{index: 2 for index in range(301, 451)}}
    tokenizer = FakeTokenizer(_character_offsets(token_count))
    model = FailOnCallModel(fail_on_call=3, labels_by_token=labels)
    recognizer = make_recognizer(
        tokenizer=tokenizer,
        model=model,
        inference_batch_size=1,
    )

    with pytest.raises(NERProcessingError) as exc_info:
        recognizer.analyze("x" * token_count)

    assert exc_info.value.phase == "inference"
    assert exc_info.value.failure_class == "forward_pass_failed"
    assert exc_info.value.windows_processed == 2
    assert model.call_count == 3
    assert recognizer.state() == "failed"
    assert recognizer.is_loaded() is True
    assert recognizer.is_warmed_up() is False
    assert recognizer.is_ready() is False


def test_normalization_failure_does_not_poison_backend(monkeypatch):
    recognizer, _, _ = _recognizer_for([0], [(0, 2)])

    def fail_normalization(_text):
        raise ValueError("request cannot be normalized")

    with monkeypatch.context() as request_failure:
        request_failure.setattr(
            "presidio.ner.triton_recognizer.normalize_for_ner",
            fail_normalization,
        )
        with pytest.raises(NERProcessingError) as exc_info:
            recognizer.analyze("ok")

    assert exc_info.value.phase == "normalization"
    assert exc_info.value.failure_class == "normalization_failed"
    assert recognizer.is_ready() is True
    assert recognizer.failure_phase() is None
    assert recognizer.failure_class() is None
    assert recognizer.analyze("ok") == []


@pytest.mark.parametrize(
    ("phase", "failure_class"),
    [
        ("tokenization", "tokenizer_failed"),
        ("windowing", "window_planning_failed"),
        ("decoding", "bio_decoding_failed"),
    ],
)
def test_input_processing_failure_does_not_poison_backend(
    monkeypatch,
    phase,
    failure_class,
):
    recognizer, _, _ = _recognizer_for([0], [(0, 2)])
    original_predict = recognizer._predict_window_entities

    def fail_request(_text, **_kwargs):
        raise NERProcessingError(
            phase=phase,
            failure_class=failure_class,
        )

    monkeypatch.setattr(recognizer, "_predict_window_entities", fail_request)
    with pytest.raises(NERProcessingError) as exc_info:
        recognizer.analyze("ok")

    assert exc_info.value.failure_class == failure_class
    assert recognizer.is_ready() is True
    assert recognizer.failure_phase() is None
    assert recognizer.failure_class() is None

    monkeypatch.setattr(recognizer, "_predict_window_entities", original_predict)
    assert recognizer.analyze("ok") == []


def test_offset_mapping_failure_does_not_poison_backend(monkeypatch):
    recognizer, _, _ = _recognizer_for([0], [(0, 2)])
    original_predict = recognizer._predict_window_entities
    outside_text = WindowEntityPrediction(
        EntityPrediction("PERSON", 0, 99, 0.99),
        0,
        False,
        False,
    )
    monkeypatch.setattr(
        recognizer,
        "_predict_window_entities",
        lambda _text, **_kwargs: ([outside_text], 1),
    )

    with pytest.raises(NERProcessingError) as exc_info:
        recognizer.analyze("ok")

    assert exc_info.value.phase == "offset_mapping"
    assert exc_info.value.failure_class == "offset_mapping_failed"
    assert recognizer.is_ready() is True

    monkeypatch.setattr(recognizer, "_predict_window_entities", original_predict)
    assert recognizer.analyze("ok") == []


def test_full_prediction_wins_over_truncated_overlap_fragment():
    predictions = [
        WindowEntityPrediction(
            EntityPrediction("PERSON", 10, 14, 0.99), 0, False, True
        ),
        WindowEntityPrediction(
            EntityPrediction("PERSON", 10, 18, 0.90), 1, False, False
        ),
    ]

    assert merge_window_predictions(predictions) == [
        EntityPrediction("PERSON", 10, 18, 0.90)
    ]


def test_duplicate_full_predictions_use_highest_confidence():
    predictions = [
        WindowEntityPrediction(
            EntityPrediction("PERSON", 10, 18, 0.90), 0, False, False
        ),
        WindowEntityPrediction(
            EntityPrediction("PERSON", 10, 18, 0.95), 1, False, False
        ),
    ]

    assert merge_window_predictions(predictions) == [
        EntityPrediction("PERSON", 10, 18, 0.95)
    ]


def test_boundary_only_prediction_fails_closed():
    predictions = [
        WindowEntityPrediction(
            EntityPrediction("SECRET_KEY", 10, 18, 0.99), 0, False, True
        )
    ]

    with pytest.raises(NERWindowBoundaryError, match="truncated SECRET_KEY"):
        merge_window_predictions(predictions)


@pytest.mark.parametrize(
    "prefix",
    ["", "👩‍💻 ", "PDF\u00a0"],
)
def test_nfc_normalization_maps_entity_back_to_original_text(prefix):
    original_name = "Сергеи\u0306 Петров"
    text = prefix + original_name
    normalized = unicodedata.normalize("NFC", text)
    normalized_start = normalized.index("Сергей")
    normalized_end = normalized_start + len("Сергей Петров")
    recognizer, _, _ = _recognizer_for(
        [1, 2],
        [
            (normalized_start, normalized_start + 6),
            (normalized_start + 7, normalized_end),
        ],
    )

    result = recognizer.analyze(text)[0]

    assert text[result.start : result.end] == original_name


def test_repeated_decomposed_entities_map_to_distinct_original_spans():
    original_name = "Сергеи\u0306"
    text = f"{original_name} встретил {original_name}"
    normalized = unicodedata.normalize("NFC", text)
    first_start = normalized.index("Сергей")
    second_start = normalized.index("Сергей", first_start + 1)
    recognizer, _, _ = _recognizer_for(
        [1, 0, 1],
        [
            (first_start, first_start + 6),
            (first_start + 7, second_start - 1),
            (second_start, second_start + 6),
        ],
    )

    results = recognizer.analyze(text)

    assert [text[result.start : result.end] for result in results] == [
        original_name,
        original_name,
    ]
    assert results[0].start != results[1].start


def test_pdf_soft_hyphen_and_emoji_keep_original_contract_span():
    contract = "ДГ-2026\u00ad/117"
    text = f"📄 Номер\u00a0договора из PDF: {contract}."
    normalized = unicodedata.normalize("NFC", text)
    start = normalized.index(contract)
    recognizer, _, _ = _recognizer_for(
        [15],
        [(start, start + len(contract))],
    )

    result = recognizer.analyze(text)[0]

    assert result.entity_type == "CONTRACT_NUMBER"
    assert text[result.start : result.end] == contract


def test_inference_failure_is_not_suppressed():
    tokenizer = FakeTokenizer([(0, 4)])
    model = FakeModel([0, 1, 0], error=RuntimeError("inference failed"))
    recognizer = make_recognizer(
        tokenizer=tokenizer,
        model=model,
        inference_batch_size=1,
    )

    with pytest.raises(NERProcessingError) as exc_info:
        recognizer.analyze("Иван")

    assert exc_info.value.phase == "inference"
    assert exc_info.value.failure_class == "forward_pass_failed"
    assert "inference failed" not in str(exc_info.value)
    assert recognizer.state() == "failed"
    assert recognizer.is_loaded() is True
    assert recognizer.is_warmed_up() is False
    assert recognizer.is_ready() is False

    with pytest.raises(NERUnavailableError) as unavailable:
        recognizer.analyze("Петр")

    assert unavailable.value.phase == "inference"
    assert unavailable.value.failure_class == "forward_pass_failed"
    assert model.call_count == 1


def test_memory_failure_is_classified_and_latches_unhealthy_state():
    tokenizer = FakeTokenizer([(0, 4)])
    model = FakeModel(
        [0, 1, 0],
        error=MemoryError("synthetic allocation failure"),
    )
    recognizer = make_recognizer(tokenizer=tokenizer, model=model)

    with pytest.raises(NERProcessingError) as exc_info:
        recognizer.analyze("Иван")

    assert exc_info.value.phase == "inference"
    assert exc_info.value.failure_class == "forward_pass_failed"
    assert "synthetic" not in str(exc_info.value)
    assert recognizer.state() == "failed"
    assert recognizer.failure_class() == "forward_pass_failed"


def test_inference_failure_reports_completed_windows():
    token_count = MAX_CONTENT_TOKENS + 1
    tokenizer = FakeTokenizer(_character_offsets(token_count))
    model = FailOnCallModel(fail_on_call=2)
    recognizer = make_recognizer(
        tokenizer=tokenizer,
        model=model,
        inference_batch_size=1,
    )
    events = []

    with pytest.raises(NERProcessingError) as exc_info:
        recognizer.analyze(
            "x" * token_count,
            telemetry_callback=events.append,
        )

    assert exc_info.value.windows_processed == 1
    assert len(events) == 1
    assert events[0].outcome == "failure"
    assert events[0].windows_processed == 1
    assert events[0].failure_phase == "inference"
    assert events[0].failure_class == "forward_pass_failed"


def test_telemetry_callback_failure_does_not_break_analysis(caplog):
    recognizer, _, _ = _recognizer_for([1], [(0, 4)])

    def fail_callback(_telemetry):
        raise RuntimeError("telemetry unavailable")

    with caplog.at_level(logging.WARNING):
        results = recognizer.analyze("Иван", telemetry_callback=fail_callback)

    assert len(results) == 1
    assert "error_type=RuntimeError" in caplog.text
    assert "Иван" not in caplog.text


def test_backend_error_normalizes_untrusted_categories():
    error = NERProcessingError(
        phase="prompt-derived-phase",
        failure_class="prompt-derived-class",
    )

    assert error.phase == "readiness"
    assert error.failure_class == "unexpected_failure"


def _mock_runtime(monkeypatch, tmp_path, tokenizer=None, model=None):
    tokenizer = tokenizer or FakeTokenizer([(0, 1)])
    model = model or FakeModel([0, 0, 0])
    monkeypatch.setattr("presidio.ner.triton_recognizer.load_and_verify_runtime_artifact", lambda _p: _manifest())
    monkeypatch.setattr("presidio.ner.tokenization.ContentTokenizer", lambda _p: tokenizer)
    monkeypatch.setattr("presidio.ner.triton_model.TritonModel", lambda *_a: model)
    (tmp_path / "config.json").write_text('{"vocab_size":3}')
    return model


def test_load_model_uses_tokenizer_and_remote_service(monkeypatch, tmp_path):
    model = _mock_runtime(monkeypatch, tmp_path)
    recognizer = make_recognizer(model_directory=tmp_path)
    recognizer.load_model()
    assert model.call_count == 1
    assert recognizer.is_loaded() and recognizer.is_warmed_up() and recognizer.is_ready()
    assert recognizer.state() == "ready" and recognizer.failure_class() is None


def test_artifact_verification_failure_marks_backend_failed(monkeypatch, tmp_path):
    def fail_verification(_path):
        raise ValueError("raw artifact path must not become status metadata")

    monkeypatch.setattr(
        "presidio.ner.triton_recognizer.load_and_verify_runtime_artifact",
        fail_verification,
    )
    recognizer = make_recognizer(model_directory=tmp_path)

    with pytest.raises(ValueError):
        recognizer.load_model()

    assert recognizer.state() == "failed"
    assert recognizer.failure_phase() == "artifact_verification"
    assert recognizer.failure_class() == "artifact_invalid"
    assert recognizer.is_loaded() is False

    with pytest.raises(NERUnavailableError) as unavailable:
        recognizer.require_ready()

    assert unavailable.value.phase == "artifact_verification"
    assert unavailable.value.failure_class == "artifact_invalid"


@pytest.mark.parametrize("component", ["ContentTokenizer", "TritonModel"])
def test_component_load_failure_marks_backend_failed(monkeypatch, tmp_path, component):
    _mock_runtime(monkeypatch, tmp_path)
    path = "presidio.ner." + ("tokenization." if component == "ContentTokenizer" else "triton_model.") + component
    def fail(*_a):
        raise RuntimeError("private component detail")
    monkeypatch.setattr(path, fail)
    r = make_recognizer(model_directory=tmp_path)
    with pytest.raises(NERUnavailableError) as exc:
        r.load_model()
    assert "private" not in str(exc.value)
    assert r.failure_class() == "triton_load_failed" and not r.is_loaded()


def test_runtime_contract_failure_marks_backend_failed(monkeypatch, tmp_path):
    model = FakeModel()
    model.config.id2label[1] = "B-UNKNOWN"
    _mock_runtime(monkeypatch, tmp_path, model=model)
    r = make_recognizer(model_directory=tmp_path)
    with pytest.raises(NERUnavailableError):
        r.load_model()
    assert r.state() == "failed" and not r.is_loaded()


def test_warmup_failure_clears_loaded_components(monkeypatch, tmp_path):
    model = _mock_runtime(monkeypatch, tmp_path, model=FakeModel(error=RuntimeError("warmup failed")))
    r = make_recognizer(model_directory=tmp_path)
    with pytest.raises(NERUnavailableError):
        r.load_model()
    assert not r.is_loaded() and not r.is_warmed_up() and not r.is_ready()
    assert r.state() == "failed" and r.failure_class() == "triton_load_failed"
    with pytest.raises(NERUnavailableError):
        r.load_model()
    assert model.call_count == 1


def test_adapter_rejects_slow_tokenizer():
    tokenizer = FakeTokenizer([(0, 1)])
    tokenizer.is_fast = False

    with pytest.raises(NERConfigurationError, match="fast tokenizer"):
        make_recognizer(tokenizer=tokenizer, model=FakeModel([0, 0, 0]))


def test_adapter_rejects_unexpected_label_map():
    tokenizer = FakeTokenizer([(0, 1)])
    model = FakeModel([0, 0, 0])
    model.config.id2label[1] = "B-UNKNOWN"

    with pytest.raises(NERConfigurationError, match="labels"):
        make_recognizer(tokenizer=tokenizer, model=model)


class LogitRowsModel(FakeModel):
    """Returns explicit per-token logits; positions include [CLS] and [SEP]."""

    def __init__(self, rows):
        super().__init__([0] * len(rows))
        self.rows = rows

    def __call__(self, **kwargs):
        self.call_count += 1
        logits = np.full((1, len(self.rows), len(EXPECTED_ID2LABEL)), -10.0, dtype=np.float32)
        for index, row in enumerate(self.rows):
            for label_id, value in row.items():
                logits[0, index, label_id] = value
        return SimpleNamespace(logits=logits)


def _weak_login_strong_person_model():
    # "dev" is barely a LOGIN (2.6 vs O 2.1); "Иван" is clearly a PERSON.
    return LogitRowsModel([{0: 10.0}, {0: 2.1, 7: 2.6}, {0: 0.0, 1: 8.0}, {0: 10.0}])


def test_o_logit_bias_drops_weak_entities_and_keeps_model_probability():
    text = "dev Иван"
    offsets = [(0, 3), (4, 8)]
    plain = make_recognizer(
        tokenizer=FakeTokenizer(offsets),
        model=_weak_login_strong_person_model(),
    )
    biased = make_recognizer(
        tokenizer=FakeTokenizer(offsets),
        model=_weak_login_strong_person_model(),
        o_logit_bias=1.0,
    )

    plain_results = plain.analyze(text, score_threshold=0.0)
    biased_results = biased.analyze(text, score_threshold=0.0)

    assert [r.entity_type for r in plain_results] == ["LOGIN", "PERSON"]
    assert [r.entity_type for r in biased_results] == ["PERSON"]
    person_probability = float(
        np.exp(8.0) / np.exp(np.array([0.0, 8.0, *([-10.0] * 15)])).sum()
    )
    assert biased_results[0].score == pytest.approx(person_probability)


def _fragmented_secret_recognizer(**kwargs):
    # "key=abc+def": the model marks "abc" and "def" but not "+".
    offsets = [(0, 3), (3, 4), (4, 7), (7, 8), (8, 11)]
    labels = [0, 0, 13, 0, 13]
    tokenizer = FakeTokenizer(offsets)
    model = FakeModel([0, *labels, 0])
    return make_recognizer(tokenizer=tokenizer, model=model, **kwargs)


def test_default_profile_keeps_model_fragments_unchanged():
    results = _fragmented_secret_recognizer().analyze("key=abc+def")

    assert [(r.entity_type, r.start, r.end) for r in results] == [
        ("SECRET_KEY", 4, 7),
        ("SECRET_KEY", 8, 11),
    ]


def test_span_postprocessing_masks_the_whole_value():
    recognizer = _fragmented_secret_recognizer(span_postprocessing="secrets-contracts")

    results = recognizer.analyze("key=abc+def")

    assert [(r.entity_type, r.start, r.end) for r in results] == [
        ("SECRET_KEY", 4, 11)
    ]
    assert recognizer.runtime_info()["span_postprocessing"] == "secrets-contracts"


def test_span_postprocessing_failure_is_request_scoped(monkeypatch):
    recognizer = _fragmented_secret_recognizer(span_postprocessing="secrets")

    def broken(*_args, **_kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(
        "presidio.ner.triton_recognizer.postprocess_spans",
        broken,
    )
    with pytest.raises(NERProcessingError) as error:
        recognizer.analyze("key=abc+def")

    assert error.value.phase == "postprocessing"
    assert error.value.failure_class == "span_postprocessing_failed"
    assert recognizer.is_ready()


def test_tiny2_profile_validates_its_own_manifest_identity():
    from presidio.model_profiles import PROFILES

    tiny2 = PROFILES["tiny2"]
    recognizer = make_recognizer(
        tokenizer=FakeTokenizer([(0, 1)]),
        model=FakeModel([0, 0, 0]),
        profile=tiny2,
    )
    manifest = ModelManifest(
        model_id=tiny2.model_id,
        revision=tiny2.revision,
        architecture="BertForTokenClassification",
        license="mit",
        base_model="cointegrated/rubert-tiny2",
        files=(),
    )

    recognizer._validate_manifest(manifest)
    with pytest.raises(NERConfigurationError):
        from dataclasses import replace
        recognizer._validate_manifest(replace(manifest, revision="0" * 40))
    assert recognizer.model_directory == tiny2.directory
    assert recognizer.runtime_info()["model_profile"] == "tiny2"
    assert recognizer.runtime_info()["o_logit_bias"] == 1.0
