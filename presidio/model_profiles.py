"""Fixed decoding bundle for the single pinned Tiny2 artifact.

Parsing helpers also serve isolated offline component experiments. Production
does not accept algorithm or artifact overrides through environment variables.
"""

from __future__ import annotations

import math
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

try:
    from .model_contract import MODEL_ID, MODEL_REVISION, reject_removed_settings
except ImportError:
    from model_contract import MODEL_ID, MODEL_REVISION, reject_removed_settings


SPAN_POSTPROCESSING_NONE = "none"
SPAN_POSTPROCESSING_SECRETS = "secrets"
SPAN_POSTPROCESSING_SECRETS_CONTRACTS = "secrets-contracts"
SPAN_POSTPROCESSING_MODES = frozenset(
    {
        SPAN_POSTPROCESSING_NONE,
        SPAN_POSTPROCESSING_SECRETS,
        SPAN_POSTPROCESSING_SECRETS_CONTRACTS,
    }
)
MAX_O_LOGIT_BIAS = 10.0
# spaCy components a profile can drop when its weights replace spaCy NER; the
# tokenizer and lemmatizer stay because recognizer context words need lemmas.
SPACY_LEMMA_ONLY_DISABLED = ("tok2vec", "morphologizer", "parser", "attribute_ruler", "ner")


class ModelProfileError(ValueError):
    """Raised for an unknown profile or an invalid decoding override."""


@dataclass(frozen=True)
class NERModelProfile:
    name: str
    model_id: str
    revision: str
    manifest_filename: str
    directory: Path
    o_logit_bias: float
    span_postprocessing: str
    spacy_disabled_pipes: tuple[str, ...] = ()
    # Rule-based sentence starts for a pipeline without the spaCy parser; the
    # name refinement needs them to cut instruction tails (ООО Вектор. Верни).
    spacy_sentencizer: bool = False


PROFILES: dict[str, NERModelProfile] = {
    # rubert-tiny2 trained on data rebuilt with Nikita's dataset pipeline, then
    # fine-tuned to work without spaCy NER (+2 to PERSON/LOCATION/ORGANIZATION
    # B-/I- classifier bias). The O-logit bias and span post-processing were
    # selected on validation data; see docs/research/ner-tiny2.md.
    "tiny2": NERModelProfile(
        name="tiny2",
        model_id=MODEL_ID,
        revision=MODEL_REVISION,
        manifest_filename="model_manifest.json",
        directory=Path("/opt/models/rubert-tiny2-secret-detection"),
        o_logit_bias=1.0,
        span_postprocessing=SPAN_POSTPROCESSING_SECRETS_CONTRACTS,
        spacy_disabled_pipes=SPACY_LEMMA_ONLY_DISABLED,
        spacy_sentencizer=True,
    ),
}
DEFAULT_PROFILE = "tiny2"


def parse_o_logit_bias(raw: object) -> float:
    try:
        value = float(raw)
    except (TypeError, ValueError) as exc:
        raise ModelProfileError("NER O-logit bias must be a number") from exc
    if not math.isfinite(value) or not 0.0 <= value <= MAX_O_LOGIT_BIAS:
        raise ModelProfileError(
            f"NER O-logit bias must be between 0 and {MAX_O_LOGIT_BIAS:g}"
        )
    return value


def parse_span_postprocessing(raw: object) -> str:
    value = str(raw).strip().lower()
    if value not in SPAN_POSTPROCESSING_MODES:
        raise ModelProfileError(
            "NER span post-processing must be one of "
            + ", ".join(sorted(SPAN_POSTPROCESSING_MODES))
        )
    return value


def resolve_profile(environ: Mapping[str, str] | None = None) -> NERModelProfile:
    """Return the pinned processing bundle, without runtime algorithm switches."""
    environ = os.environ if environ is None else environ
    try:
        reject_removed_settings(environ)
    except ValueError as error:
        raise ModelProfileError(str(error)) from None
    return PROFILES[DEFAULT_PROFILE]
