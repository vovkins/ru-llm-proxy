"""Pinned Hugging Face token-classification adapter for Presidio."""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional

from presidio_analyzer import RecognizerResult

from result_merging import (
    DETECTION_SOURCE_METADATA_KEY,
    NER_MODEL_METADATA_KEY,
    SOURCE_NER,
)

try:
    from entity_types import NER_ENTITY_TYPES
except ImportError:
    from presidio.entity_types import NER_ENTITY_TYPES

from .text_processing import TokenWindow, normalize_for_ner, plan_token_windows

try:
    from model_artifact import (
        MODEL_DIRECTORY,
        ModelManifest,
        load_and_verify_embedded_manifest,
    )
except ImportError:
    from presidio.model_artifact import (
        MODEL_DIRECTORY,
        ModelManifest,
        load_and_verify_embedded_manifest,
    )


logger = logging.getLogger(__name__)

MODEL_ID = "fef2/ner_rus_bert-secret_detection"
MODEL_REVISION = "52b5b0745aac14f73fcf2ac0f91d9b5001a85ae4"
MODEL_ARCHITECTURE = "BertForTokenClassification"
MAX_CONTENT_TOKENS = 384
WINDOW_OVERLAP_TOKENS = 64
WINDOW_BOUNDARY_SEARCH_TOKENS = 64
DEFAULT_INFERENCE_BATCH_SIZE = 4

EXPECTED_ID2LABEL = {
    0: "O",
    1: "B-PERSON",
    2: "I-PERSON",
    3: "B-LOCATION",
    4: "I-LOCATION",
    5: "B-ORGANIZATION",
    6: "I-ORGANIZATION",
    7: "B-LOGIN",
    8: "I-LOGIN",
    9: "B-PASSWORD",
    10: "I-PASSWORD",
    11: "B-AUTH_TOKEN",
    12: "I-AUTH_TOKEN",
    13: "B-SECRET_KEY",
    14: "I-SECRET_KEY",
    15: "B-CONTRACT_NUMBER",
    16: "I-CONTRACT_NUMBER",
}


class NERConfigurationError(RuntimeError):
    """Raised when the local model does not match the fixed NER contract."""


class NERWindowBoundaryError(RuntimeError):
    """Raised when overlapping windows cannot establish a complete entity."""

    def __init__(
        self,
        unresolved_clusters: tuple["UnresolvedWindowEntityCluster", ...],
    ):
        first_type = (
            unresolved_clusters[0].entity_type
            if unresolved_clusters
            else "unknown"
        )
        super().__init__(
            f"NER produced only truncated {first_type} window predictions"
        )
        self.unresolved_clusters = unresolved_clusters


class NERInferenceCancelled(RuntimeError):
    """Raised when an in-flight request stops between bounded model batches."""


class NERBackendError(RuntimeError):
    """Safe, bounded failure raised by the required NER backend."""

    def __init__(
        self,
        *,
        phase: str,
        failure_class: str,
        windows_processed: int = 0,
    ):
        super().__init__("required NER backend is unavailable")
        self.phase = phase if phase in NER_FAILURE_PHASES else "readiness"
        self.failure_class = (
            failure_class
            if failure_class in NER_FAILURE_CLASSES
            else "unexpected_failure"
        )
        self.windows_processed = max(0, int(windows_processed))


class NERProcessingError(NERBackendError):
    """Raised when a runtime NER processing stage fails."""


class NERUnavailableError(NERBackendError):
    """Raised after the required NER backend entered a failed state."""


NER_STATE_NOT_LOADED = "not_loaded"
NER_STATE_LOADING = "loading"
NER_STATE_WARMING_UP = "warming_up"
NER_STATE_READY = "ready"
NER_STATE_FAILED = "failed"
NER_FAILURE_PHASES = frozenset(
    {
        "artifact_verification",
        "model_validation",
        "model_loading",
        "warmup",
        "readiness",
        "normalization",
        "tokenization",
        "windowing",
        "inference",
        "decoding",
        "offset_mapping",
        "startup",
    }
)
NER_FAILURE_CLASSES = frozenset(
    {
        "artifact_invalid",
        "manifest_contract_invalid",
        "dependency_import_failed",
        "tokenizer_load_failed",
        "model_load_failed",
        "runtime_contract_invalid",
        "warmup_failed",
        "not_ready",
        "normalization_failed",
        "tokenizer_failed",
        "window_planning_failed",
        "window_boundary_unresolved",
        "forward_pass_failed",
        "bio_decoding_failed",
        "offset_mapping_failed",
        "unexpected_failure",
        "startup_failed",
    }
)
NER_INFERENCE_OUTCOMES = frozenset(
    {"success", "skipped", "failure", "unavailable"}
)
NER_REQUEST_SCOPED_FAILURE_CLASSES = frozenset(
    {
        "normalization_failed",
        "tokenizer_failed",
        "window_planning_failed",
        "window_boundary_unresolved",
        "bio_decoding_failed",
        "offset_mapping_failed",
    }
)


@dataclass(frozen=True)
class NERInferenceTelemetry:
    outcome: str
    duration_seconds: float
    windows_processed: int
    input_tokens: int = 0
    failure_phase: str | None = None
    failure_class: str | None = None

    def __post_init__(self) -> None:
        if self.outcome not in NER_INFERENCE_OUTCOMES:
            raise ValueError("unsupported NER inference outcome")
        if self.duration_seconds < 0:
            raise ValueError("NER inference duration must not be negative")
        if self.windows_processed < 0:
            raise ValueError("NER processed-window count must not be negative")
        if self.input_tokens < 0:
            raise ValueError("NER input-token count must not be negative")
        if (
            self.failure_phase is not None
            and self.failure_phase not in NER_FAILURE_PHASES
        ):
            raise ValueError("unsupported NER failure phase")
        if (
            self.failure_class is not None
            and self.failure_class not in NER_FAILURE_CLASSES
        ):
            raise ValueError("unsupported NER failure class")
        if self.outcome in {"success", "skipped"} and (
            self.failure_phase is not None or self.failure_class is not None
        ):
            raise ValueError("successful NER telemetry cannot contain failure details")


@dataclass(frozen=True)
class TokenPrediction:
    label: str
    start: int
    end: int
    score: float


@dataclass(frozen=True)
class EntityPrediction:
    entity_type: str
    start: int
    end: int
    score: float


@dataclass(frozen=True)
class WindowEntityPrediction:
    entity: EntityPrediction
    window_index: int
    touches_left_boundary: bool
    touches_right_boundary: bool

    @property
    def touches_internal_boundary(self) -> bool:
        return self.touches_left_boundary or self.touches_right_boundary


@dataclass(frozen=True)
class UnresolvedWindowEntityCluster:
    """Same-type predictions which are all truncated by internal windows."""

    entity_type: str
    predictions: tuple[WindowEntityPrediction, ...]


@dataclass(frozen=True)
class NERWindowAnalysis:
    """Window predictions plus bounded workload dimensions."""

    predictions: list[WindowEntityPrediction]
    windows_processed: int
    input_tokens: int

    def __iter__(self):
        # Preserve the historical two-value private helper contract in tests.
        yield self.predictions
        yield self.windows_processed


def _normalize_requested_entities(
    entities: Optional[list[str]],
) -> Optional[set[str]]:
    if not entities:
        return None
    return {str(entity).upper() for entity in entities}


def should_run_ner(
    requested_entities: Optional[list[str]],
    score_threshold: float,
) -> bool:
    """Return whether the fixed NER model can contribute to this request."""
    del score_threshold
    normalized = _normalize_requested_entities(requested_entities)
    return normalized is None or bool(normalized & NER_ENTITY_TYPES)


def _split_bio_label(label: str) -> tuple[str, str | None]:
    if label == "O":
        return "O", None
    if "-" not in label:
        return "O", None
    prefix, entity_type = label.split("-", 1)
    if prefix not in {"B", "I"} or entity_type not in NER_ENTITY_TYPES:
        return "O", None
    return prefix, entity_type


def decode_bio_predictions(
    predictions: list[TokenPrediction],
) -> list[EntityPrediction]:
    """Decode token predictions, treating malformed I-tags as new spans."""
    entities: list[EntityPrediction] = []
    current_type: str | None = None
    current_start = 0
    current_end = 0
    current_scores: list[float] = []

    def close_current() -> None:
        nonlocal current_type, current_start, current_end, current_scores
        if current_type is not None:
            entities.append(
                EntityPrediction(
                    entity_type=current_type,
                    start=current_start,
                    end=current_end,
                    score=min(current_scores),
                )
            )
        current_type = None
        current_start = 0
        current_end = 0
        current_scores = []

    for prediction in predictions:
        prefix, entity_type = _split_bio_label(prediction.label)
        if prefix == "O" or prediction.end <= prediction.start:
            close_current()
            continue

        starts_new = prefix == "B" or current_type != entity_type
        if starts_new:
            close_current()
            current_type = entity_type
            current_start = prediction.start
            current_end = prediction.end
            current_scores = [prediction.score]
            continue

        current_end = max(current_end, prediction.end)
        current_scores.append(prediction.score)

    close_current()
    return entities


def _spans_overlap(first: EntityPrediction, second: EntityPrediction) -> bool:
    return first.start < second.end and second.start < first.end


def _filter_window_predictions(
    predictions: list[WindowEntityPrediction],
    *,
    score_threshold: float,
    requested_entities: Optional[set[str]],
) -> list[WindowEntityPrediction]:
    return [
        prediction
        for prediction in predictions
        if prediction.entity.score >= score_threshold
        and (
            requested_entities is None
            or prediction.entity.entity_type in requested_entities
        )
    ]


def _select_boundary_recovery_predictions(
    predictions: list[WindowEntityPrediction],
    unresolved_clusters: tuple[UnresolvedWindowEntityCluster, ...],
) -> list[WindowEntityPrediction]:
    return [
        prediction
        for prediction in predictions
        if not prediction.touches_internal_boundary
        and any(
            prediction.entity.entity_type == cluster.entity_type
            and any(
                _spans_overlap(prediction.entity, original.entity)
                for original in cluster.predictions
            )
            for cluster in unresolved_clusters
        )
    ]


def merge_window_predictions(
    predictions: list[WindowEntityPrediction],
) -> list[EntityPrediction]:
    """Resolve duplicate same-type predictions from overlapping windows."""
    grouped: dict[str, list[WindowEntityPrediction]] = {}
    for prediction in predictions:
        grouped.setdefault(prediction.entity.entity_type, []).append(prediction)

    merged: list[EntityPrediction] = []
    unresolved_clusters: list[UnresolvedWindowEntityCluster] = []
    for entity_type in sorted(grouped):
        ordered = sorted(
            grouped[entity_type],
            key=lambda item: (
                item.entity.start,
                item.entity.end,
                item.window_index,
            ),
        )
        clusters: list[list[WindowEntityPrediction]] = []
        for prediction in ordered:
            if not clusters or not any(
                _spans_overlap(prediction.entity, existing.entity)
                for existing in clusters[-1]
            ):
                clusters.append([prediction])
            else:
                clusters[-1].append(prediction)

        for cluster in clusters:
            best = max(
                cluster,
                key=lambda item: (
                    not item.touches_internal_boundary,
                    item.entity.end - item.entity.start,
                    item.entity.score,
                    -item.window_index,
                ),
            )
            if best.touches_internal_boundary:
                unresolved_clusters.append(
                    UnresolvedWindowEntityCluster(
                        entity_type=entity_type,
                        predictions=tuple(cluster),
                    )
                )
            else:
                merged.append(best.entity)

    if unresolved_clusters:
        raise NERWindowBoundaryError(tuple(unresolved_clusters))

    return sorted(
        merged,
        key=lambda entity: (entity.start, entity.end, entity.entity_type),
    )


class HuggingFaceNERRecognizer:
    """NER recognizer backed by one immutable local Transformers model."""

    def __init__(
        self,
        *,
        model_directory: Path = MODEL_DIRECTORY,
        tokenizer: Any | None = None,
        model: Any | None = None,
        inference_batch_size: int = DEFAULT_INFERENCE_BATCH_SIZE,
    ):
        if (tokenizer is None) != (model is None):
            raise ValueError("tokenizer and model must be supplied together")
        if inference_batch_size < 1:
            raise ValueError("inference_batch_size must be positive")
        self.model_directory = Path(model_directory)
        self._tokenizer = tokenizer
        self._model = model
        self._manifest: ModelManifest | None = None
        self._inference_batch_size = inference_batch_size
        self._warmed_up = False
        self._state = NER_STATE_NOT_LOADED
        self._failure_phase: str | None = None
        self._failure_class: str | None = None
        if self.is_loaded():
            self._validate_runtime_components(tokenizer, model)
            self._warmed_up = True
            self._state = NER_STATE_READY

    def is_loaded(self) -> bool:
        return self._tokenizer is not None and self._model is not None

    def is_warmed_up(self) -> bool:
        return self._warmed_up

    def is_ready(self) -> bool:
        return (
            self._state == NER_STATE_READY
            and self.is_loaded()
            and self.is_warmed_up()
        )

    def state(self) -> str:
        return self._state

    def failure_phase(self) -> str | None:
        return self._failure_phase

    def failure_class(self) -> str | None:
        return self._failure_class

    def require_ready(self) -> None:
        if self.is_ready():
            return
        raise NERUnavailableError(
            phase=self._failure_phase or "readiness",
            failure_class=self._failure_class or "not_ready",
        )

    def _mark_failed(
        self,
        *,
        phase: str,
        failure_class: str,
        clear_components: bool,
    ) -> None:
        if clear_components:
            self._tokenizer = None
            self._model = None
            self._manifest = None
        self._warmed_up = False
        self._state = NER_STATE_FAILED
        self._failure_phase = phase
        self._failure_class = failure_class

    def _clear_failure(self) -> None:
        self._failure_phase = None
        self._failure_class = None

    def _mark_failed_if_service_wide(self, error: NERBackendError) -> None:
        if error.failure_class in NER_REQUEST_SCOPED_FAILURE_CLASSES:
            return
        self._mark_failed(
            phase=error.phase,
            failure_class=error.failure_class,
            clear_components=False,
        )

    def load_model(self) -> None:
        if self.is_ready():
            return
        if self._state == NER_STATE_FAILED:
            self.require_ready()
        if self.is_loaded():
            try:
                self._warm_up()
            except Exception as exc:
                failure_class = (
                    exc.failure_class
                    if isinstance(exc, NERBackendError)
                    else "warmup_failed"
                )
                self._mark_failed(
                    phase="warmup",
                    failure_class=failure_class,
                    clear_components=True,
                )
                raise
            self._state = NER_STATE_READY
            return

        self._state = NER_STATE_LOADING
        self._clear_failure()
        try:
            manifest = load_and_verify_embedded_manifest(self.model_directory)
        except Exception:
            self._mark_failed(
                phase="artifact_verification",
                failure_class="artifact_invalid",
                clear_components=True,
            )
            raise
        try:
            self._validate_manifest(manifest)
        except Exception:
            self._mark_failed(
                phase="model_validation",
                failure_class="manifest_contract_invalid",
                clear_components=True,
            )
            raise

        try:
            from transformers import AutoModelForTokenClassification, AutoTokenizer
        except Exception:
            self._mark_failed(
                phase="model_loading",
                failure_class="dependency_import_failed",
                clear_components=True,
            )
            raise

        logger.info(
            "Loading pinned Hugging Face NER model: model_id=%s revision=%s",
            MODEL_ID,
            MODEL_REVISION,
        )
        try:
            tokenizer = AutoTokenizer.from_pretrained(
                str(self.model_directory),
                local_files_only=True,
                use_fast=True,
                trust_remote_code=False,
                # Transformers 4.57 misidentifies this BertPreTokenizer as Mistral.
                fix_mistral_regex=False,
            )
        except Exception:
            self._mark_failed(
                phase="model_loading",
                failure_class="tokenizer_load_failed",
                clear_components=True,
            )
            raise
        try:
            model = AutoModelForTokenClassification.from_pretrained(
                str(self.model_directory),
                local_files_only=True,
                trust_remote_code=False,
                use_safetensors=True,
            )
        except Exception:
            self._mark_failed(
                phase="model_loading",
                failure_class="model_load_failed",
                clear_components=True,
            )
            raise
        try:
            self._validate_runtime_components(tokenizer, model)
        except Exception:
            self._mark_failed(
                phase="model_validation",
                failure_class="runtime_contract_invalid",
                clear_components=True,
            )
            raise
        try:
            model.eval()
        except Exception:
            self._mark_failed(
                phase="model_validation",
                failure_class="runtime_contract_invalid",
                clear_components=True,
            )
            raise

        self._tokenizer = tokenizer
        self._model = model
        self._manifest = manifest
        self._state = NER_STATE_WARMING_UP
        try:
            self._warm_up()
        except Exception as exc:
            failure_class = (
                exc.failure_class
                if isinstance(exc, NERBackendError)
                else "warmup_failed"
            )
            self._mark_failed(
                phase="warmup",
                failure_class=failure_class,
                clear_components=True,
            )
            raise
        self._state = NER_STATE_READY
        logger.info(
            "Pinned Hugging Face NER model loaded and warmed up: "
            "model_id=%s revision=%s",
            MODEL_ID,
            MODEL_REVISION,
        )

    def _warm_up(self) -> None:
        if not self.is_loaded():
            raise RuntimeError("cannot warm up an unloaded NER model")
        self._state = NER_STATE_WARMING_UP
        self._warmed_up = False
        self._predict_window_entities(
            normalize_for_ner("Проверка готовности NER.").text
        )
        self._warmed_up = True

    @staticmethod
    def _validate_manifest(manifest: ModelManifest) -> None:
        if manifest.model_id != MODEL_ID or manifest.revision != MODEL_REVISION:
            raise NERConfigurationError("local NER model identity does not match code")
        if manifest.architecture != MODEL_ARCHITECTURE:
            raise NERConfigurationError("local NER model architecture is unsupported")

    @staticmethod
    def _validate_runtime_components(tokenizer: Any, model: Any) -> None:
        if not getattr(tokenizer, "is_fast", False):
            raise NERConfigurationError("NER requires a fast tokenizer")

        config = getattr(model, "config", None)
        architectures = set(getattr(config, "architectures", None) or [])
        if MODEL_ARCHITECTURE not in architectures:
            raise NERConfigurationError("NER model architecture does not match contract")
        raw_id2label = getattr(config, "id2label", None)
        if not isinstance(raw_id2label, dict):
            raise NERConfigurationError("NER model does not expose id2label")
        try:
            id2label = {int(index): str(label) for index, label in raw_id2label.items()}
        except (TypeError, ValueError) as exc:
            raise NERConfigurationError("NER model id2label is invalid") from exc
        if id2label != EXPECTED_ID2LABEL:
            raise NERConfigurationError("NER model labels do not match contract")

    def analyze(
        self,
        text: str,
        score_threshold: float = 0.35,
        entities: Optional[list[str]] = None,
        telemetry_callback: Callable[[NERInferenceTelemetry], None] | None = None,
        cancellation_event: threading.Event | None = None,
    ) -> list[RecognizerResult]:
        started_at = time.perf_counter()
        if not should_run_ner(entities, score_threshold):
            self._emit_inference_telemetry(
                telemetry_callback,
                outcome="skipped",
                started_at=started_at,
                windows_processed=0,
            )
            return []

        try:
            results, windows_processed, input_tokens = self._analyze_required(
                text,
                score_threshold=score_threshold,
                entities=entities,
                cancellation_event=cancellation_event,
            )
        except NERInferenceCancelled:
            raise
        except NERBackendError as error:
            self._emit_inference_telemetry(
                telemetry_callback,
                outcome=(
                    "failure"
                    if isinstance(error, NERProcessingError)
                    else "unavailable"
                ),
                started_at=started_at,
                windows_processed=error.windows_processed,
                input_tokens=0,
                failure_phase=error.phase,
                failure_class=error.failure_class,
            )
            raise
        except Exception:
            self._emit_inference_telemetry(
                telemetry_callback,
                outcome="failure",
                started_at=started_at,
                windows_processed=0,
                input_tokens=0,
                failure_phase=self.failure_phase() or "readiness",
                failure_class=self.failure_class() or "unexpected_failure",
            )
            raise

        self._emit_inference_telemetry(
            telemetry_callback,
            outcome="success",
            started_at=started_at,
            windows_processed=windows_processed,
            input_tokens=input_tokens,
        )
        return results

    @staticmethod
    def _emit_inference_telemetry(
        callback: Callable[[NERInferenceTelemetry], None] | None,
        *,
        outcome: str,
        started_at: float,
        windows_processed: int,
        input_tokens: int = 0,
        failure_phase: str | None = None,
        failure_class: str | None = None,
    ) -> None:
        if callback is None:
            return
        telemetry = NERInferenceTelemetry(
            outcome=outcome,
            duration_seconds=max(0.0, time.perf_counter() - started_at),
            windows_processed=max(0, windows_processed),
            input_tokens=max(0, input_tokens),
            failure_phase=failure_phase,
            failure_class=failure_class,
        )
        try:
            callback(telemetry)
        except Exception as exc:
            logger.warning(
                "NER telemetry callback failed: error_type=%s",
                type(exc).__name__,
            )

    def _analyze_required(
        self,
        text: str,
        *,
        score_threshold: float,
        entities: Optional[list[str]],
        cancellation_event: threading.Event | None,
    ) -> tuple[list[RecognizerResult], int, int]:
        _raise_if_cancelled(cancellation_event)
        if self._state == NER_STATE_FAILED:
            self.require_ready()
        if not self.is_ready():
            self.load_model()

        try:
            normalized = normalize_for_ner(text)
        except Exception as exc:
            error = NERProcessingError(
                phase="normalization",
                failure_class="normalization_failed",
                windows_processed=0,
            )
            raise error from exc
        try:
            window_analysis = self._predict_window_entities(
                normalized.text,
                cancellation_event=cancellation_event,
            )
            window_predictions, windows_processed = window_analysis
            input_tokens = getattr(window_analysis, "input_tokens", 0)
        except NERInferenceCancelled:
            raise
        except NERBackendError as error:
            self._mark_failed_if_service_wide(error)
            raise
        except Exception as exc:
            error = NERProcessingError(
                phase="inference",
                failure_class="unexpected_failure",
                windows_processed=0,
            )
            self._mark_failed(
                phase=error.phase,
                failure_class=error.failure_class,
                clear_components=False,
            )
            raise error from exc
        requested_entities = _normalize_requested_entities(entities)
        filtered_predictions = _filter_window_predictions(
            window_predictions,
            score_threshold=score_threshold,
            requested_entities=requested_entities,
        )
        try:
            entity_predictions = merge_window_predictions(filtered_predictions)
        except NERWindowBoundaryError as unresolved:
            try:
                recovery_predictions, recovery_windows_processed = (
                    self._recover_window_boundaries(
                        normalized.text,
                        unresolved.unresolved_clusters,
                        cancellation_event=cancellation_event,
                    )
                )
            except NERInferenceCancelled:
                raise
            except NERBackendError as error:
                combined_error = NERProcessingError(
                    phase=error.phase,
                    failure_class=error.failure_class,
                    windows_processed=windows_processed + error.windows_processed,
                )
                self._mark_failed_if_service_wide(combined_error)
                raise combined_error from error
            except Exception as exc:
                error = NERProcessingError(
                    phase="inference",
                    failure_class="unexpected_failure",
                    windows_processed=windows_processed,
                )
                self._mark_failed_if_service_wide(error)
                raise error from exc
            windows_processed += recovery_windows_processed
            filtered_recovery_predictions = _filter_window_predictions(
                recovery_predictions,
                score_threshold=score_threshold,
                requested_entities=requested_entities,
            )
            selected_recovery_predictions = _select_boundary_recovery_predictions(
                filtered_recovery_predictions,
                unresolved.unresolved_clusters,
            )
            try:
                entity_predictions = merge_window_predictions(
                    filtered_predictions + selected_recovery_predictions
                )
            except NERWindowBoundaryError as exc:
                error = NERProcessingError(
                    phase="windowing",
                    failure_class="window_boundary_unresolved",
                    windows_processed=windows_processed,
                )
                raise error from exc
            except Exception as exc:
                error = NERProcessingError(
                    phase="inference",
                    failure_class="unexpected_failure",
                    windows_processed=windows_processed,
                )
                self._mark_failed_if_service_wide(error)
                raise error from exc
        except Exception as exc:
            error = NERProcessingError(
                phase="inference",
                failure_class="unexpected_failure",
                windows_processed=windows_processed,
            )
            self._mark_failed_if_service_wide(error)
            raise error from exc

        results: list[RecognizerResult] = []
        for entity in entity_predictions:
            try:
                start, end = normalized.to_original_span(entity.start, entity.end)
            except Exception as exc:
                error = NERProcessingError(
                    phase="offset_mapping",
                    failure_class="offset_mapping_failed",
                    windows_processed=windows_processed,
                )
                raise error from exc
            results.append(
                RecognizerResult(
                    entity_type=entity.entity_type,
                    start=start,
                    end=end,
                    score=entity.score,
                    analysis_explanation=None,
                    recognition_metadata={
                        DETECTION_SOURCE_METADATA_KEY: SOURCE_NER,
                        NER_MODEL_METADATA_KEY: MODEL_ID,
                    },
                )
            )
        try:
            self.require_ready()
        except NERUnavailableError as error:
            raise NERUnavailableError(
                phase=error.phase,
                failure_class=error.failure_class,
                windows_processed=windows_processed,
            ) from None
        return results, windows_processed, input_tokens

    def _tokenize_content(
        self,
        text: str,
    ) -> tuple[list[int], list[tuple[int, int]]]:
        encoded = self._tokenizer(
            text,
            add_special_tokens=False,
            padding=False,
            return_attention_mask=False,
            return_offsets_mapping=True,
            truncation=False,
        )
        input_ids = encoded.get("input_ids")
        raw_offsets = encoded.get("offset_mapping")
        if not isinstance(input_ids, list) or any(
            not isinstance(token_id, int) for token_id in input_ids
        ):
            raise NERConfigurationError("NER tokenizer returned invalid input_ids")
        if not isinstance(raw_offsets, list) or len(raw_offsets) != len(input_ids):
            raise NERConfigurationError("NER tokenizer returned invalid offsets")
        try:
            offsets = [
                (int(offset[0]), int(offset[1]))
                for offset in raw_offsets
            ]
        except (TypeError, ValueError, IndexError) as exc:
            raise NERConfigurationError("NER tokenizer offsets are invalid") from exc
        return input_ids, offsets

    def _predict_window_entities(
        self,
        normalized_text: str,
        *,
        cancellation_event: threading.Event | None = None,
    ) -> NERWindowAnalysis:
        _raise_if_cancelled(cancellation_event)
        input_ids, offsets, windows = self._prepare_window_input(normalized_text)
        predictions, windows_processed = self._predict_windows(
            input_ids,
            offsets,
            windows,
            cancellation_event=cancellation_event,
        )
        return NERWindowAnalysis(
            predictions=predictions,
            windows_processed=windows_processed,
            input_tokens=len(input_ids),
        )

    def _prepare_window_input(
        self,
        normalized_text: str,
    ) -> tuple[list[int], list[tuple[int, int]], tuple[TokenWindow, ...]]:
        try:
            input_ids, offsets = self._tokenize_content(normalized_text)
        except Exception as exc:
            raise NERProcessingError(
                phase="tokenization",
                failure_class="tokenizer_failed",
                windows_processed=0,
            ) from exc
        try:
            windows = plan_token_windows(
                normalized_text,
                offsets,
                max_content_tokens=MAX_CONTENT_TOKENS,
                overlap_tokens=WINDOW_OVERLAP_TOKENS,
                boundary_search_tokens=WINDOW_BOUNDARY_SEARCH_TOKENS,
            )
        except Exception as exc:
            raise NERProcessingError(
                phase="windowing",
                failure_class="window_planning_failed",
                windows_processed=0,
            ) from exc
        return input_ids, offsets, windows

    def _predict_windows(
        self,
        input_ids: list[int],
        offsets: list[tuple[int, int]],
        windows: tuple[TokenWindow, ...],
        *,
        cancellation_event: threading.Event | None = None,
    ) -> tuple[list[WindowEntityPrediction], int]:
        predictions: list[WindowEntityPrediction] = []
        windows_processed = 0
        token_count = len(input_ids)
        for batch_start in range(0, len(windows), self._inference_batch_size):
            _raise_if_cancelled(cancellation_event)
            batch = windows[
                batch_start : batch_start + self._inference_batch_size
            ]
            try:
                batch_predictions = self._infer_window_batch(
                    [
                        input_ids[window.start_token : window.end_token]
                        for window in batch
                    ],
                    [
                        offsets[window.start_token : window.end_token]
                        for window in batch
                    ],
                )
            except Exception as exc:
                raise NERProcessingError(
                    phase="inference",
                    failure_class="forward_pass_failed",
                    windows_processed=windows_processed,
                ) from exc
            for window, token_predictions in zip(batch, batch_predictions):
                first_start = offsets[window.start_token][0]
                last_end = offsets[window.end_token - 1][1]
                try:
                    decoded_entities = decode_bio_predictions(token_predictions)
                except Exception as exc:
                    raise NERProcessingError(
                        phase="decoding",
                        failure_class="bio_decoding_failed",
                        windows_processed=windows_processed,
                    ) from exc
                windows_processed += 1
                for entity in decoded_entities:
                    predictions.append(
                        WindowEntityPrediction(
                            entity=entity,
                            window_index=window.index,
                            touches_left_boundary=(
                                window.start_token > 0
                                and entity.start <= first_start
                            ),
                            touches_right_boundary=(
                                window.end_token < token_count
                                and entity.end >= last_end
                            ),
                        )
                    )
            _raise_if_cancelled(cancellation_event)
        return predictions, windows_processed

    def _recover_window_boundaries(
        self,
        normalized_text: str,
        unresolved_clusters: tuple[UnresolvedWindowEntityCluster, ...],
        *,
        cancellation_event: threading.Event | None = None,
    ) -> tuple[list[WindowEntityPrediction], int]:
        input_ids, offsets, original_windows = self._prepare_window_input(
            normalized_text
        )
        token_count = len(input_ids)
        if token_count <= MAX_CONTENT_TOKENS:
            return [], 0

        boundary_tokens: set[int] = set()
        for cluster in unresolved_clusters:
            for prediction in cluster.predictions:
                if not 0 <= prediction.window_index < len(original_windows):
                    continue
                window = original_windows[prediction.window_index]
                if prediction.touches_left_boundary and window.start_token > 0:
                    boundary_tokens.add(window.start_token)
                if (
                    prediction.touches_right_boundary
                    and window.end_token < token_count
                ):
                    boundary_tokens.add(window.end_token)

        original_ranges = {
            (window.start_token, window.end_token) for window in original_windows
        }
        recovery_ranges: set[tuple[int, int]] = set()
        window_size = min(MAX_CONTENT_TOKENS, token_count)
        latest_start = token_count - window_size
        for boundary_token in sorted(boundary_tokens):
            start_token = min(
                max(0, boundary_token - (window_size // 2)),
                latest_start,
            )
            recovery_range = (start_token, start_token + window_size)
            if recovery_range not in original_ranges:
                recovery_ranges.add(recovery_range)

        recovery_windows = tuple(
            TokenWindow(
                index=len(original_windows) + index,
                start_token=start_token,
                end_token=end_token,
            )
            for index, (start_token, end_token) in enumerate(
                sorted(recovery_ranges)
            )
        )
        if not recovery_windows:
            return [], 0
        return self._predict_windows(
            input_ids,
            offsets,
            recovery_windows,
            cancellation_event=cancellation_event,
        )

    def _infer_window(
        self,
        input_ids: list[int],
        offsets: list[tuple[int, int]],
    ) -> list[TokenPrediction]:
        return self._infer_window_batch([input_ids], [offsets])[0]

    def _infer_window_batch(
        self,
        input_id_batches: list[list[int]],
        offset_batches: list[list[tuple[int, int]]],
    ) -> list[list[TokenPrediction]]:
        """Run one padded BERT forward pass for one or more token windows."""
        import torch

        if not input_id_batches or len(input_id_batches) != len(offset_batches):
            raise NERConfigurationError("NER batch input is empty or inconsistent")
        prepared_batches: list[list[int]] = []
        token_type_batches: list[list[int]] = []
        special_token_batches: list[list[int]] = []
        window_offset_batches: list[list[tuple[int, int]]] = []
        for input_ids, offsets in zip(input_id_batches, offset_batches):
            if len(input_ids) != len(offsets):
                raise NERConfigurationError("NER ids and offsets do not match")
            prepared_ids = self._tokenizer.build_inputs_with_special_tokens(
                input_ids
            )
            if (
                not isinstance(prepared_ids, list)
                or len(prepared_ids) != len(input_ids) + 2
                or prepared_ids[1:-1] != input_ids
            ):
                raise NERConfigurationError(
                    "NER tokenizer does not implement expected BERT special tokens"
                )
            token_type_ids = self._tokenizer.create_token_type_ids_from_sequences(
                input_ids
            )
            if (
                not isinstance(token_type_ids, list)
                or len(token_type_ids) != len(prepared_ids)
            ):
                raise NERConfigurationError(
                    "NER tokenizer returned invalid token_type_ids"
                )
            special_tokens = [1, *([0] * len(input_ids)), 1]
            window_offsets = [(0, 0), *offsets, (0, 0)]
            prepared_batches.append(prepared_ids)
            token_type_batches.append(token_type_ids)
            special_token_batches.append(special_tokens)
            window_offset_batches.append(window_offsets)

        pad_token_id = getattr(self._tokenizer, "pad_token_id", 0)
        if pad_token_id is None:
            raise NERConfigurationError("NER tokenizer has no padding token")
        maximum_length = max(len(item) for item in prepared_batches)
        padded_ids = []
        padded_attention = []
        padded_token_types = []
        for prepared_ids, token_type_ids in zip(
            prepared_batches,
            token_type_batches,
        ):
            padding = maximum_length - len(prepared_ids)
            padded_ids.append([*prepared_ids, *([int(pad_token_id)] * padding)])
            padded_attention.append([1] * len(prepared_ids) + [0] * padding)
            padded_token_types.append([*token_type_ids, *([0] * padding)])

        model_inputs = {
            "input_ids": torch.tensor(padded_ids, dtype=torch.long),
            "attention_mask": torch.tensor(padded_attention, dtype=torch.long),
            "token_type_ids": torch.tensor(padded_token_types, dtype=torch.long),
        }

        with torch.inference_mode():
            logits = self._model(**model_inputs).logits
            probabilities = torch.softmax(logits, dim=-1)
            predicted_ids = torch.argmax(probabilities, dim=-1)
            predicted_scores = probabilities.gather(
                2,
                predicted_ids.unsqueeze(2),
            ).squeeze(2)

        if len(predicted_ids) != len(prepared_batches):
            raise NERConfigurationError("NER model output batch does not match input")

        prediction_batches: list[list[TokenPrediction]] = []
        for batch_index, (window_offsets, special_tokens) in enumerate(
            zip(window_offset_batches, special_token_batches)
        ):
            item_ids = predicted_ids[batch_index]
            item_scores = predicted_scores[batch_index]
            if len(item_ids) < len(special_tokens):
                raise NERConfigurationError(
                    "NER model output length does not match input"
                )
            predictions: list[TokenPrediction] = []
            for index, ((start, end), special) in enumerate(
                zip(window_offsets, special_tokens)
            ):
                if special:
                    continue
                label_id = int(item_ids[index].item())
                try:
                    label = EXPECTED_ID2LABEL[label_id]
                except KeyError as exc:
                    raise NERConfigurationError(
                        "NER model returned unknown label id"
                    ) from exc
                predictions.append(
                    TokenPrediction(
                        label=label,
                        start=int(start),
                        end=int(end),
                        score=float(item_scores[index].item()),
                    )
                )
            prediction_batches.append(predictions)
        return prediction_batches


def _raise_if_cancelled(cancellation_event: threading.Event | None) -> None:
    if cancellation_event is not None and cancellation_event.is_set():
        raise NERInferenceCancelled("NER inference was cancelled")
