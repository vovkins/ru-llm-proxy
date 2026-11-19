"""Validate Analyzer coverage and seal it in cache entries without source data."""

import hashlib
import hmac
import json
from collections import Counter
from contextvars import ContextVar
from dataclasses import dataclass
from threading import Lock

from .boundary_contract import Coverage, TYPES, digest

_PROOF_COUNTER = Counter()
_PROOF_LOCK = Lock()


def proof_metrics_snapshot():
    with _PROOF_LOCK:
        return dict(_PROOF_COUNTER)


@dataclass(frozen=True)
class BoundaryCacheContext:
    text: str
    key: str | None
    secret: bytes | None
    expected_signature: str | None


CACHE_CONTEXT: ContextVar[BoundaryCacheContext | None] = ContextVar(
    "boundary_cache_context", default=None
)


class AnalysisEntities(list):
    __slots__ = ("coverage", "cache_key")

    def __init__(self, entities, coverage, cache_key=None):
        super().__init__(entities)
        self.coverage = coverage
        self.cache_key = cache_key

    def __bool__(self):
        return len(self) > 0 or bool(self.coverage.spans)


def retain_result(original, filtered):
    if isinstance(original, AnalysisEntities):
        return AnalysisEntities(filtered, original.coverage, original.cache_key)
    return filtered


def clone_result(value):
    return retain_result(value, list(value))


def fail(error_type, key=None, entity_types=()):
    with _PROOF_LOCK:
        for kind in set(entity_types) or {"OTHER"}:
            _PROOF_COUNTER[(kind if kind in TYPES else "OTHER", "invariant_failure")] += 1
    context = CACHE_CONTEXT.get()
    error = error_type(phase="postprocessing", failure_class="boundary_positive_coverage_failed")
    error.boundary_cache_key = key if key is not None else (context.key if context else None)
    raise error


def decode_response(text, data, input_kind, threshold, error_type):
    if not isinstance(data, dict) or not isinstance(data.get("entities"), list):
        fail(error_type)
    try:
        proof = Coverage.parse(
            data.get("boundary_coverage"), text=text,
            expected_kind=input_kind, expected_threshold=threshold,
        )
    except (ValueError, TypeError, OverflowError):
        fail(error_type)
    context = CACHE_CONTEXT.get()
    if context and context.expected_signature is not None and proof.signature != context.expected_signature:
        fail(error_type)
    return AnalysisEntities(data["entities"], proof)


def check_result(text, entities, ranges, exclusions=(), *, error_type):
    if not isinstance(entities, AnalysisEntities):
        return
    try:
        entities.coverage.bind(text)
        missing = entities.coverage.missing(ranges, exclusions)
    except (ValueError, TypeError, OverflowError):
        fail(error_type, entities.cache_key)
    if missing:
        fail(error_type, entities.cache_key, [span[2] for span in missing])


def _cache_mac(payload, secret):
    canonical = json.dumps(payload, ensure_ascii=True, separators=(",", ":"), sort_keys=True)
    return hmac.new(secret, b"boundary-cache-v1\0" + canonical.encode(), hashlib.sha256).hexdigest()


def seal_cache_payload(payload, text, entities, context):
    if (not isinstance(entities, AnalysisEntities) or context is None
            or not isinstance(context.secret, bytes) or context.key is None):
        return None
    try:
        entities.coverage.bind(text)
    except ValueError:
        return None
    proof = entities.coverage.wire()
    proof.pop("text_sha256")
    payload = dict(payload, schema=2, boundary_coverage=proof, cache_binding=context.key)
    payload["proof_mac"] = _cache_mac(payload, context.secret)
    return payload


def unseal_cache_payload(payload, text_length, legacy_schema, context):
    if (context is None or not isinstance(context.secret, bytes)
            or context.key is None or len(context.text) != text_length):
        return None
    fields = {"entities", "schema", "text_length", "boundary_coverage", "cache_binding", "proof_mac"}
    if (not isinstance(payload, dict) or set(payload) != fields or payload["schema"] != 2
            or payload["cache_binding"] != context.key):
        return None
    payload = dict(payload)
    signature = payload.pop("proof_mac")
    if not isinstance(signature, str) or not hmac.compare_digest(
        signature, _cache_mac(payload, context.secret)
    ):
        return None
    encoded_proof = payload.pop("boundary_coverage")
    if not isinstance(encoded_proof, dict) or "text_sha256" in encoded_proof:
        return None
    try:
        proof = Coverage.parse(
            dict(encoded_proof, text_sha256=digest(context.text)),
            text=context.text, text_length=text_length,
        )
    except (ValueError, TypeError, KeyError, OverflowError):
        return None
    payload.pop("cache_binding")
    payload["schema"] = legacy_schema
    return payload, proof
