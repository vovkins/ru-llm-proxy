"""Runtime contracts independent from Torch and model execution weights."""

import hashlib
from dataclasses import replace

import numpy as np
import pytest
from tokenizers import Tokenizer, models, pre_tokenizers, processors

from presidio import model_artifact as artifact
from presidio.ner.decoding import classify_logits
from presidio.ner.tokenization import ContentTokenizer


@pytest.mark.parametrize("bias", [0.0, 1.0, 10.0])
def test_fp32_decoding_matches_high_precision_reference(bias):
    logits = np.random.default_rng(72).normal(size=(4, 386, 17)).astype(np.float32)
    actual_ids, actual_scores = classify_logits(logits, bias)
    reference = logits.astype(np.float64)
    probabilities = np.exp(reference - reference.max(-1, keepdims=True))
    probabilities /= probabilities.sum(-1, keepdims=True)
    choices = reference.copy()
    choices[..., 0] += bias
    expected_ids = choices.argmax(-1)
    assert np.array_equal(actual_ids, expected_ids)
    np.testing.assert_allclose(actual_scores,
        np.take_along_axis(probabilities, expected_ids[..., None], -1)[..., 0], atol=2e-7, rtol=2e-6)


def test_ties_extreme_logits_and_o_bias_do_not_change_reported_probability():
    logits = np.zeros((1, 4, 17), dtype=np.float32)
    logits[0, 1, 1] = 0.5
    logits[0, 2, 1] = 10000.0
    logits[0, 3, :] = -10000.0
    plain_ids, plain_scores = classify_logits(logits, 0)
    corrected_ids, corrected_scores = classify_logits(logits, 1)
    assert plain_ids.tolist() == [[0, 1, 1, 0]]
    assert corrected_ids.tolist() == [[0, 0, 1, 0]]
    assert corrected_scores[0, 2] == plain_scores[0, 2] == 1
    assert corrected_scores[0, 1] < plain_scores[0, 1]
    assert np.isfinite(corrected_scores).all()


@pytest.mark.parametrize("value", [np.zeros((1, 386, 16), np.float32),
    np.zeros((1, 386, 17), np.float64), np.full((1, 386, 17), np.nan, np.float32),
    np.full((1, 386, 17), np.inf, np.float32)])
def test_invalid_logits_are_rejected(value):
    with pytest.raises(ValueError):
        classify_logits(value, 1)


def test_content_tokenizer_preserves_offsets_and_special_tokens(tmp_path):
    tokenizer = Tokenizer(models.WordPiece({"[UNK]": 0, "[CLS]": 1, "[SEP]": 2,
                                           "[PAD]": 3, "hello": 4, "world": 5}))
    tokenizer.pre_tokenizer = pre_tokenizers.BertPreTokenizer()
    tokenizer.post_processor = processors.BertProcessing(("[SEP]", 2), ("[CLS]", 1))
    tokenizer.save(str(tmp_path / "tokenizer.json"))
    (tmp_path / "special_tokens_map.json").write_text(
        '{"cls_token":"[CLS]","sep_token":"[SEP]","pad_token":"[PAD]"}')
    actual = ContentTokenizer(tmp_path)
    assert actual("hello world") == {"input_ids": [4, 5], "offset_mapping": [(0, 5), (6, 11)]}
    assert actual("") == {"input_ids": [], "offset_mapping": []}
    assert actual.build_inputs_with_special_tokens([4, 5]) == [1, 4, 5, 2]
    assert actual.create_token_type_ids_from_sequences([4, 5]) == [0] * 4
    assert actual.pad_token_id == 3
    with pytest.raises(ValueError):
        actual("hello", truncation=True)


def runtime_bundle(tmp_path, monkeypatch):
    files = []
    for name in ("config.json", "tokenizer.json", "special_tokens_map.json"):
        data = name.encode()
        (tmp_path / name).write_bytes(data)
        files.append(artifact.ModelFile(name, len(data), hashlib.sha256(data).hexdigest()))
    files.append(artifact.ModelFile("model.safetensors", 1, "a" * 64))
    manifest = artifact.ModelManifest("test/model", "revision", "architecture", "mit", "base", tuple(files))
    (tmp_path / artifact.EMBEDDED_MANIFEST_NAME).write_text("{}")
    monkeypatch.setattr(artifact, "load_manifest", lambda _p: manifest)
    return manifest


def test_runtime_verifies_subset_without_requiring_execution_weights(tmp_path, monkeypatch):
    manifest = runtime_bundle(tmp_path, monkeypatch)
    assert artifact.load_and_verify_runtime_artifact(tmp_path) == manifest
    (tmp_path / "model.safetensors").write_bytes(b"x")
    with pytest.raises(artifact.ModelArtifactError, match="contents mismatch"):
        artifact.load_and_verify_runtime_artifact(tmp_path)


def test_runtime_rejects_changed_tokenizer_and_source_manifest(tmp_path, monkeypatch):
    manifest = runtime_bundle(tmp_path, monkeypatch)
    (tmp_path / "tokenizer.json").write_bytes(b"wrong")
    with pytest.raises(artifact.ModelArtifactError):
        artifact.load_and_verify_runtime_artifact(tmp_path)
    monkeypatch.setattr(artifact, "load_manifest", lambda p:
        replace(manifest, revision="changed") if p.name.startswith(".") else manifest)
    with pytest.raises(artifact.ModelArtifactError, match="canonical"):
        artifact.load_and_verify_runtime_artifact(tmp_path)
