"""Supported NER model profiles: pinned artifact identity plus decoding settings.

A profile binds one immutable model artifact (manifest, install directory) to the
decoding settings it was validated with. The Analyzer serves exactly one profile,
selected by ``PRESIDIO_ANALYZER_NER_MODEL_PROFILE``; ``bert`` keeps the original
behaviour. Two optional variables override the profile's decoding defaults.
"""

from __future__ import annotations

import math
import os
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Mapping


MODEL_PROFILE_ENV = "PRESIDIO_ANALYZER_NER_MODEL_PROFILE"
O_LOGIT_BIAS_ENV = "PRESIDIO_ANALYZER_NER_O_LOGIT_BIAS"
SPAN_POSTPROCESSING_ENV = "PRESIDIO_ANALYZER_NER_SPAN_POSTPROCESSING"

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


PROFILES: dict[str, NERModelProfile] = {
    # Nikita's fine-tuned rubert-base: the original, unchanged behaviour.
    "bert": NERModelProfile(
        name="bert",
        model_id="fef2/ner_rus_bert-secret_detection",
        revision="52b5b0745aac14f73fcf2ac0f91d9b5001a85ae4",
        manifest_filename="model_manifest.json",
        directory=Path("/opt/models/ner_rus_bert-secret_detection"),
        o_logit_bias=0.0,
        span_postprocessing=SPAN_POSTPROCESSING_NONE,
    ),
    # rubert-tiny2 trained on data rebuilt with Nikita's dataset pipeline. The
    # O-logit bias and span post-processing were selected on validation data
    # together with these weights; see docs/research/ner-tiny2.md.
    "tiny2": NERModelProfile(
        name="tiny2",
        model_id="local/rubert-tiny2-secret-detection",
        revision="nikita-style-tiny2-2026-09-28-mix-a-s1",
        manifest_filename="model_manifest.tiny2.json",
        directory=Path("/opt/models/rubert-tiny2-secret-detection"),
        o_logit_bias=1.0,
        span_postprocessing=SPAN_POSTPROCESSING_SECRETS_CONTRACTS,
    ),
}
DEFAULT_PROFILE = "bert"


def _env_value(environ: Mapping[str, str], name: str) -> str:
    return str(environ.get(name, "") or "").strip()


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
    """Return the configured profile with validated decoding overrides applied."""
    environ = os.environ if environ is None else environ
    name = (_env_value(environ, MODEL_PROFILE_ENV) or DEFAULT_PROFILE).lower()
    profile = PROFILES.get(name)
    if profile is None:
        raise ModelProfileError(
            "unknown NER model profile; expected one of " + ", ".join(sorted(PROFILES))
        )
    raw_bias = _env_value(environ, O_LOGIT_BIAS_ENV)
    if raw_bias:
        profile = replace(profile, o_logit_bias=parse_o_logit_bias(raw_bias))
    raw_postprocessing = _env_value(environ, SPAN_POSTPROCESSING_ENV)
    if raw_postprocessing:
        profile = replace(
            profile,
            span_postprocessing=parse_span_postprocessing(raw_postprocessing),
        )
    return profile
