"""FP32 label selection; the O correction does not alter reported scores."""

import numpy as np


def classify_logits(logits, o_logit_bias):
    if (not isinstance(logits, np.ndarray) or logits.dtype != np.float32
        or logits.ndim != 3 or logits.shape[-1] != 17 or not np.isfinite(logits).all()):
        raise ValueError("Invalid NER logits")
    probabilities = np.exp(logits - np.max(logits, axis=-1, keepdims=True))
    probabilities /= probabilities.sum(axis=-1, keepdims=True)
    if o_logit_bias:
        choices = logits.copy()
        choices[..., 0] += o_logit_bias
        predicted_ids = choices.argmax(axis=-1)
    else:
        predicted_ids = probabilities.argmax(axis=-1)
    scores = np.take_along_axis(probabilities, predicted_ids[..., None], axis=-1)[..., 0]
    return predicted_ids, scores
