"""Tests for NER model profile selection."""

from __future__ import annotations

from pathlib import Path

import pytest

from presidio.model_artifact import load_manifest
from presidio.model_profiles import (
    DEFAULT_PROFILE,
    PROFILES,
    ModelProfileError,
    resolve_profile,
)

PRESIDIO = Path(__file__).resolve().parents[1]


def test_default_profile_is_the_original_bert_without_decoding_changes():
    profile = resolve_profile({})

    assert DEFAULT_PROFILE == "bert"
    assert profile.name == "bert"
    assert profile.model_id == "fef2/ner_rus_bert-secret_detection"
    assert profile.o_logit_bias == 0.0
    assert profile.span_postprocessing == "none"


def test_tiny2_profile_enables_validated_decoding_settings():
    profile = resolve_profile({"PRESIDIO_ANALYZER_NER_MODEL_PROFILE": "tiny2"})

    assert profile.o_logit_bias == 1.0
    assert profile.span_postprocessing == "secrets-contracts"
    assert profile.directory == Path("/opt/models/rubert-tiny2-secret-detection")


def test_empty_overrides_keep_profile_defaults():
    profile = resolve_profile(
        {
            "PRESIDIO_ANALYZER_NER_MODEL_PROFILE": "tiny2",
            "PRESIDIO_ANALYZER_NER_O_LOGIT_BIAS": "",
            "PRESIDIO_ANALYZER_NER_SPAN_POSTPROCESSING": " ",
        }
    )

    assert profile == PROFILES["tiny2"]


def test_overrides_are_applied_to_the_selected_profile():
    profile = resolve_profile(
        {
            "PRESIDIO_ANALYZER_NER_MODEL_PROFILE": "BERT",
            "PRESIDIO_ANALYZER_NER_O_LOGIT_BIAS": "1.5",
            "PRESIDIO_ANALYZER_NER_SPAN_POSTPROCESSING": "secrets",
        }
    )

    assert profile.name == "bert"
    assert profile.o_logit_bias == 1.5
    assert profile.span_postprocessing == "secrets"


@pytest.mark.parametrize(
    "environ",
    [
        {"PRESIDIO_ANALYZER_NER_MODEL_PROFILE": "gpt"},
        {"PRESIDIO_ANALYZER_NER_O_LOGIT_BIAS": "high"},
        {"PRESIDIO_ANALYZER_NER_O_LOGIT_BIAS": "-1"},
        {"PRESIDIO_ANALYZER_NER_O_LOGIT_BIAS": "nan"},
        {"PRESIDIO_ANALYZER_NER_O_LOGIT_BIAS": "11"},
        {"PRESIDIO_ANALYZER_NER_SPAN_POSTPROCESSING": "everything"},
    ],
)
def test_invalid_configuration_fails_closed(environ):
    with pytest.raises(ModelProfileError):
        resolve_profile(environ)


@pytest.mark.parametrize("name", sorted(PROFILES))
def test_each_profile_manifest_matches_its_identity(name):
    profile = PROFILES[name]
    manifest = load_manifest(PRESIDIO / profile.manifest_filename)

    assert manifest.model_id == profile.model_id
    assert manifest.revision == profile.revision
    assert manifest.architecture == "BertForTokenClassification"
    assert {model_file.path for model_file in manifest.files} == {
        "config.json",
        "model.safetensors",
        "special_tokens_map.json",
        "tokenizer.json",
        "tokenizer_config.json",
        "vocab.txt",
    }


def test_tiny2_manifest_pins_the_released_weights():
    manifest = load_manifest(PRESIDIO / "model_manifest.tiny2.json")
    weights = {model_file.path: model_file for model_file in manifest.files}

    assert (
        weights["model.safetensors"].sha256
        == "53cde9f0e47f95478411457e1d74fdc16070517d54a2b45a27d74e49ea75cb67"
    )
    assert manifest.base_model == "cointegrated/rubert-tiny2"
