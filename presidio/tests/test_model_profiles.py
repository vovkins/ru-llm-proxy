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


def test_default_profile_is_the_single_pinned_model():
    profile = resolve_profile({})

    assert DEFAULT_PROFILE == "tiny2"
    assert set(PROFILES) == {"tiny2"}
    assert profile.name == "tiny2"
    assert profile.model_id == "Katerina-Bir/rubert-tiny2-secret-detection"


def test_tiny2_profile_enables_validated_decoding_settings():
    profile = resolve_profile({})

    assert profile.o_logit_bias == 1.0
    assert profile.span_postprocessing == "secrets-contracts"
    assert profile.directory == Path("/opt/models/rubert-tiny2-secret-detection")
    # The weights were fine-tuned to replace spaCy NER; lemmas stay for context.
    assert "ner" in profile.spacy_disabled_pipes
    assert "lemmatizer" not in profile.spacy_disabled_pipes


@pytest.mark.parametrize("value", ["", "tiny2", "bert", "1.5", "secrets"])
@pytest.mark.parametrize("key", [
    "NER_MODEL_PROFILE", "PRESIDIO_ANALYZER_NER_MODEL_PROFILE",
    "PRESIDIO_ANALYZER_NER_O_LOGIT_BIAS", "PRESIDIO_ANALYZER_NER_SPAN_POSTPROCESSING",
])
def test_removed_overrides_are_rejected_even_when_they_match_defaults(key, value):
    with pytest.raises(ModelProfileError, match=key):
        resolve_profile({key: value})


@pytest.mark.parametrize(
    "environ",
    [
        {"PRESIDIO_ANALYZER_NER_MODEL_PROFILE": "gpt"},
        {"PRESIDIO_ANALYZER_NER_MODEL_PROFILE": "bert"},
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
    manifest = load_manifest(PRESIDIO / "model_manifest.json")
    weights = {model_file.path: model_file for model_file in manifest.files}

    assert (
        weights["model.safetensors"].sha256
        == "dcbacfba6daeb68cf87bfcf229fbb29f9639b8cc81383e631262247d07bd1d7d"
    )
    assert manifest.base_model == "cointegrated/rubert-tiny2"


def test_lemma_only_pipes_exist_in_the_spacy_model_and_keep_lemmas():
    import spacy

    from model_profiles import SPACY_LEMMA_ONLY_DISABLED

    nlp = spacy.load("ru_core_news_sm")
    assert set(SPACY_LEMMA_ONLY_DISABLED) <= set(nlp.pipe_names)
    for pipe in SPACY_LEMMA_ONLY_DISABLED:
        nlp.disable_pipe(pipe)
    doc = nlp("Паспорт выдан отделением в Твери")
    assert [token.lemma_ for token in doc][0] == "паспорт"
    assert not doc.ents


def test_tiny2_sentencizer_restores_sentence_starts_without_the_parser():
    import spacy

    from model_profiles import SPACY_LEMMA_ONLY_DISABLED

    assert resolve_profile({}).spacy_sentencizer
    nlp = spacy.load("ru_core_news_sm")
    for pipe in SPACY_LEMMA_ONLY_DISABLED:
        nlp.disable_pipe(pipe)
    nlp.add_pipe("sentencizer", first=True)
    text = "Компания ООО Вектор. Верни ровно MODEL_OK."
    starts = [token.idx for token in nlp(text) if token.is_sent_start]
    assert text.index("Верни") in starts
