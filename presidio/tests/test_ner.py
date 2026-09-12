"""Tests for the pinned Hugging Face NER adapter."""

from __future__ import annotations

import logging
import threading
import unicodedata
from types import SimpleNamespace
from unittest.mock import patch

import pytest

pytest.importorskip("presidio_analyzer")
torch = pytest.importorskip("torch")

from presidio.model_artifact import ModelManifest
from presidio.entity_types import NER_ENTITY_TYPES
from presidio.ner.huggingface_recognizer import (
    EXPECTED_ID2LABEL,
    MAX_CONTENT_TOKENS,
    MODEL_ID,
    MODEL_REVISION,
    EntityPrediction,
    HuggingFaceNERRecognizer,
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
    should_run_ner,
)
from presidio.result_merging import (
    DETECTION_SOURCE_METADATA_KEY,
    NER_MODEL_METADATA_KEY,
    SOURCE_NER,
)


class FakeTokenizer:
    is_fast = True

    def __init__(self, offsets):
        self.offsets = offsets
        self.call_kwargs = None
        self.prepared_input_ids = []

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
        self.eval_called = False
        self.call_count = 0
        self.input_lengths = []

    def eval(self):
        self.eval_called = True
        return self

    def __call__(self, **kwargs):
        self.call_count += 1
        self.input_lengths.append(len(kwargs["input_ids"][0]))
        if self.error is not None:
            raise self.error
        logits = torch.full((1, len(self.label_ids), len(EXPECTED_ID2LABEL)), -10.0)
        for index, label_id in enumerate(self.label_ids):
            logits[0, index, label_id] = 10.0
        return SimpleNamespace(logits=logits)


class GlobalLabelModel(FakeModel):
    """Return labels based on the global content-token ids from FakeTokenizer."""

    def __init__(self, labels_by_token=None):
        super().__init__()
        self.labels_by_token = labels_by_token or {}

    def __call__(self, **kwargs):
        self.call_count += 1
        input_batches = kwargs["input_ids"].tolist()
        self.input_lengths.append(len(input_batches[0]))
        logits = torch.full(
            (
                len(input_batches),
                len(input_batches[0]),
                len(EXPECTED_ID2LABEL),
            ),
            -10.0,
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
    return HuggingFaceNERRecognizer(tokenizer=tokenizer, model=model), tokenizer, model


def test_window_batching_uses_one_forward_pass_for_multiple_windows():
    token_count = MAX_CONTENT_TOKENS * 2
    text = "x" * token_count
    offsets = [(index, index + 1) for index in range(token_count)]
    tokenizer = FakeTokenizer(offsets)
    model = GlobalLabelModel()
    recognizer = HuggingFaceNERRecognizer(
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
    recognizer = HuggingFaceNERRecognizer(
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
        base_model="DeepPavlov/rubert-base-cased",
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
    recognizer = HuggingFaceNERRecognizer(tokenizer=tokenizer, model=model)

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
    recognizer = HuggingFaceNERRecognizer(tokenizer=tokenizer, model=model)

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
    recognizer = HuggingFaceNERRecognizer(tokenizer=tokenizer, model=model)

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
    recognizer = HuggingFaceNERRecognizer(tokenizer=tokenizer, model=model)

    results = recognizer.analyze("x" * token_count)

    assert [(result.entity_type, result.start, result.end) for result in results] == [
        ("PERSON", 382, 386)
    ]


def test_shifted_window_recovers_entity_longer_than_overlap():
    token_count = 500
    labels = {300: 1, **{index: 2 for index in range(301, 451)}}
    tokenizer = FakeTokenizer(_character_offsets(token_count))
    model = GlobalLabelModel(labels)
    recognizer = HuggingFaceNERRecognizer(tokenizer=tokenizer, model=model)
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
    recognizer = HuggingFaceNERRecognizer(tokenizer=tokenizer, model=model)
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
    recognizer = HuggingFaceNERRecognizer(
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
            "presidio.ner.huggingface_recognizer.normalize_for_ner",
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
    recognizer = HuggingFaceNERRecognizer(
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


def test_inference_failure_reports_completed_windows():
    token_count = MAX_CONTENT_TOKENS + 1
    tokenizer = FakeTokenizer(_character_offsets(token_count))
    model = FailOnCallModel(fail_on_call=2)
    recognizer = HuggingFaceNERRecognizer(
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


def test_load_model_is_local_only_and_uses_safetensors(monkeypatch, tmp_path):
    tokenizer = FakeTokenizer([(0, 1)])
    model = FakeModel([0, 0, 0])
    monkeypatch.setattr(
        "presidio.ner.huggingface_recognizer.load_and_verify_embedded_manifest",
        lambda _path: _manifest(),
    )

    with patch(
        "transformers.AutoTokenizer.from_pretrained",
        return_value=tokenizer,
    ) as tokenizer_loader, patch(
        "transformers.AutoModelForTokenClassification.from_pretrained",
        return_value=model,
    ) as model_loader:
        recognizer = HuggingFaceNERRecognizer(model_directory=tmp_path)
        recognizer.load_model()

    tokenizer_loader.assert_called_once_with(
        str(tmp_path),
        local_files_only=True,
        use_fast=True,
        trust_remote_code=False,
        fix_mistral_regex=False,
    )
    model_loader.assert_called_once_with(
        str(tmp_path),
        local_files_only=True,
        trust_remote_code=False,
        use_safetensors=True,
    )
    assert model.eval_called is True
    assert model.call_count == 1
    assert recognizer.is_loaded() is True
    assert recognizer.is_warmed_up() is True
    assert recognizer.is_ready() is True
    assert recognizer.state() == "ready"
    assert recognizer.failure_phase() is None
    assert recognizer.failure_class() is None


def test_artifact_verification_failure_marks_backend_failed(monkeypatch, tmp_path):
    def fail_verification(_path):
        raise ValueError("raw artifact path must not become status metadata")

    monkeypatch.setattr(
        "presidio.ner.huggingface_recognizer.load_and_verify_embedded_manifest",
        fail_verification,
    )
    recognizer = HuggingFaceNERRecognizer(model_directory=tmp_path)

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


@pytest.mark.parametrize(
    ("failing_component", "failure_class"),
    [
        ("tokenizer", "tokenizer_load_failed"),
        ("model", "model_load_failed"),
    ],
)
def test_component_load_failure_marks_backend_failed(
    monkeypatch,
    tmp_path,
    failing_component,
    failure_class,
):
    tokenizer = FakeTokenizer([(0, 1)])
    model = FakeModel([0, 0, 0])
    monkeypatch.setattr(
        "presidio.ner.huggingface_recognizer.load_and_verify_embedded_manifest",
        lambda _path: _manifest(),
    )
    tokenizer_result = (
        RuntimeError("tokenizer load details")
        if failing_component == "tokenizer"
        else tokenizer
    )
    model_result = (
        RuntimeError("model load details")
        if failing_component == "model"
        else model
    )

    with patch(
        "transformers.AutoTokenizer.from_pretrained",
        side_effect=tokenizer_result
        if isinstance(tokenizer_result, Exception)
        else None,
        return_value=None
        if isinstance(tokenizer_result, Exception)
        else tokenizer_result,
    ), patch(
        "transformers.AutoModelForTokenClassification.from_pretrained",
        side_effect=model_result if isinstance(model_result, Exception) else None,
        return_value=None if isinstance(model_result, Exception) else model_result,
    ):
        recognizer = HuggingFaceNERRecognizer(model_directory=tmp_path)
        with pytest.raises(RuntimeError):
            recognizer.load_model()

    assert recognizer.state() == "failed"
    assert recognizer.failure_phase() == "model_loading"
    assert recognizer.failure_class() == failure_class
    assert recognizer.is_loaded() is False


def test_runtime_contract_failure_marks_backend_failed(monkeypatch, tmp_path):
    tokenizer = FakeTokenizer([(0, 1)])
    model = FakeModel([0, 0, 0])
    model.config.id2label[1] = "B-UNKNOWN"
    monkeypatch.setattr(
        "presidio.ner.huggingface_recognizer.load_and_verify_embedded_manifest",
        lambda _path: _manifest(),
    )

    with patch(
        "transformers.AutoTokenizer.from_pretrained",
        return_value=tokenizer,
    ), patch(
        "transformers.AutoModelForTokenClassification.from_pretrained",
        return_value=model,
    ):
        recognizer = HuggingFaceNERRecognizer(model_directory=tmp_path)
        with pytest.raises(NERConfigurationError):
            recognizer.load_model()

    assert recognizer.state() == "failed"
    assert recognizer.failure_phase() == "model_validation"
    assert recognizer.failure_class() == "runtime_contract_invalid"
    assert recognizer.is_loaded() is False


def test_warmup_failure_clears_loaded_components(monkeypatch, tmp_path):
    tokenizer = FakeTokenizer([(0, 1)])
    model = FakeModel([0, 0, 0], error=RuntimeError("warmup failed"))
    monkeypatch.setattr(
        "presidio.ner.huggingface_recognizer.load_and_verify_embedded_manifest",
        lambda _path: _manifest(),
    )

    with patch(
        "transformers.AutoTokenizer.from_pretrained",
        return_value=tokenizer,
    ), patch(
        "transformers.AutoModelForTokenClassification.from_pretrained",
        return_value=model,
    ):
        recognizer = HuggingFaceNERRecognizer(model_directory=tmp_path)
        with pytest.raises(NERProcessingError) as exc_info:
            recognizer.load_model()

    assert exc_info.value.phase == "inference"
    assert exc_info.value.failure_class == "forward_pass_failed"
    assert recognizer.is_loaded() is False
    assert recognizer.is_warmed_up() is False
    assert recognizer.is_ready() is False
    assert recognizer.state() == "failed"
    assert recognizer.failure_phase() == "warmup"
    assert recognizer.failure_class() == "forward_pass_failed"

    with pytest.raises(NERUnavailableError) as unavailable:
        recognizer.load_model()

    assert unavailable.value.phase == "warmup"
    assert unavailable.value.failure_class == "forward_pass_failed"
    assert model.call_count == 1


def test_adapter_rejects_slow_tokenizer():
    tokenizer = FakeTokenizer([(0, 1)])
    tokenizer.is_fast = False

    with pytest.raises(NERConfigurationError, match="fast tokenizer"):
        HuggingFaceNERRecognizer(tokenizer=tokenizer, model=FakeModel([0, 0, 0]))


def test_adapter_rejects_unexpected_label_map():
    tokenizer = FakeTokenizer([(0, 1)])
    model = FakeModel([0, 0, 0])
    model.config.id2label[1] = "B-UNKNOWN"

    with pytest.raises(NERConfigurationError, match="labels"):
        HuggingFaceNERRecognizer(tokenizer=tokenizer, model=model)
