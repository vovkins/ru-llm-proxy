"""Opt-in fixed-bundle boundary proof transport, cache and final mask checking."""

import json, hmac, hashlib
from contextvars import ContextVar
from collections import Counter
from threading import Lock
from .boundary_contract import Coverage
from .processing_bundle import BOUNDARY_COVERAGE_ENABLED

_PROOF_COUNTER = Counter()
_PROOF_LOCK = Lock()


def proof_metrics_snapshot():
    with _PROOF_LOCK:
        return dict(_PROOF_COUNTER)


_ERROR_TYPE = ContextVar("boundary_error_type", default=None)
_EXPECTED_SIGNATURE = ContextVar("boundary_expected_signature", default=None)
_CACHE_INPUT = ContextVar("boundary_cache_input", default=None)
_CACHE_KEY = ContextVar("boundary_cache_key", default=None)
_CACHE_SECRET = ContextVar("boundary_cache_secret", default=None)


class AnalysisEntities(list):
    __slots__ = ("coverage", "cache_key", "error_type")

    def __init__(self, entities, coverage, cache_key=None, error_type=None):
        super().__init__(entities)
        self.coverage = coverage
        self.cache_key = cache_key
        self.error_type = error_type

    def __bool__(self):
        return len(self) > 0 or bool(self.coverage.spans)


def clone_result(value):
    return (
        AnalysisEntities(list(value), value.coverage, value.cache_key, value.error_type)
        if isinstance(value, AnalysisEntities)
        else list(value)
    )


def retain_result(original, filtered):
    return (
        AnalysisEntities(list(filtered), original.coverage, original.cache_key, original.error_type)
        if isinstance(original, AnalysisEntities)
        else filtered
    )


def fail(key=None, error_type=None, entity_types=()):
    from .boundary_contract import TYPES

    with _PROOF_LOCK:
        for kind in set(entity_types) or {"OTHER"}:
            _PROOF_COUNTER[(kind if kind in TYPES else "OTHER", "invariant_failure")] += 1
    error_type = error_type or _ERROR_TYPE.get()
    if error_type is None:
        from .pii_guardrail import AnalyzerUnavailableError

        error_type = AnalyzerUnavailableError
    if key is None:
        key = _CACHE_KEY.get()
    error = error_type(phase="postprocessing", failure_class="boundary_positive_coverage_failed")
    error.boundary_cache_key = key
    raise error


def _decode_response(text, data, input_kind):
    if not isinstance(data, dict) or not isinstance(data.get("entities"), list):
        fail()
    try:
        proof = Coverage.parse(
            data.get("boundary_coverage"),
            text=text,
            expected_kind=input_kind,
            expected_threshold=0.35,
        )
    except (ValueError, TypeError, OverflowError):
        fail()
    expected = _EXPECTED_SIGNATURE.get()
    if expected is not None and proof.signature != expected:
        fail()
    return AnalysisEntities(data["entities"], proof, error_type=_ERROR_TYPE.get())


def decode_response(text, data, input_kind, error_type=None):
    token = _ERROR_TYPE.set(error_type or _ERROR_TYPE.get())
    try:
        return _decode_response(text, data, input_kind)
    finally:
        _ERROR_TYPE.reset(token)


def check_result(text, entities, ranges, exclusions=()):
    if not isinstance(entities, AnalysisEntities):
        return
    try:
        entities.coverage.bind(text)
        missing = entities.coverage.missing(ranges, exclusions)
    except (ValueError, TypeError, OverflowError):
        fail(entities.cache_key, entities.error_type)
    if missing:
        fail(entities.cache_key, entities.error_type, [x[2] for x in missing])


def install(cls):
    from . import pii_guardrail as pg

    init0 = cls.__init__
    mask0 = cls._mask_text
    cached0 = cls._analyze_text_with_cache
    serialize0 = cls._serialize_analysis_cache_entry.__func__
    deserialize0 = cls._deserialize_analysis_cache_entry

    async def cached(self, text, cache_context, request_id, text_field_index):
        if not getattr(self, "_boundary_coverage_enabled", False):
            return await cached0(self, text, cache_context, request_id, text_field_index)
        key = self._analysis_cache_key(text, cache_context) if cache_context is not None else None
        values = [
            (_ERROR_TYPE, self._boundary_error_type),
            (
                _EXPECTED_SIGNATURE,
                cache_context["analyzer_signature"] if cache_context is not None else None,
            ),
            (_CACHE_INPUT, text),
            (_CACHE_KEY, key),
            (_CACHE_SECRET, self._analysis_cache_secret),
        ]
        tokens = [(variable, variable.set(value)) for variable, value in values]
        try:
            result = await cached0(self, text, cache_context, request_id, text_field_index)
            if not isinstance(result, AnalysisEntities):
                fail(key)
            result.cache_key = key
            result.error_type = self._boundary_error_type
            if (
                cache_context is not None
                and result.coverage.signature != cache_context["analyzer_signature"]
            ):
                fail(key)
            try:
                result.coverage.bind(text)
            except ValueError:
                fail(key)
            return result
        finally:
            for variable, token in reversed(tokens):
                variable.reset(token)

    def mask(self, text, entities, *args, **kwargs):
        # Count actual emitted placeholders on this instance/call without intercepting
        # shared methods or retaining source values in the proof.
        if not getattr(self, "_boundary_coverage_enabled", False) or not isinstance(
            entities, AnalysisEntities
        ):
            return mask0(self, text, entities, *args, **kwargs)
        result = mask0(self, text, entities, *args, **kwargs)
        # Equivalent deterministic mask plan, including escape unions and numeric atoms.
        # A direct mask-plan return is installed in pii_guardrail._mask_text source;
        # this context-free wrapper uses that captured plan from the return mapping.
        mapping = result[1]
        ranges = getattr(mapping, "boundary_mask_ranges", None)
        if ranges is None:
            ranges = ()
        check_result(text, entities, ranges, kwargs.get("exclusion_spans") or ())
        return result

    @classmethod
    def serialize(klass, text, entities):
        if not BOUNDARY_COVERAGE_ENABLED:
            return serialize0(klass, text, entities)
        secret, key = _CACHE_SECRET.get(), _CACHE_KEY.get()
        if (
            not isinstance(entities, AnalysisEntities)
            or not isinstance(secret, bytes)
            or key is None
        ):
            return None
        try:
            entities.coverage.bind(text)
        except ValueError:
            return None
        base = serialize0(klass, text, list(entities))
        if base is None:
            return None
        proof = entities.coverage.wire()
        proof.pop("text_sha256")
        payload = json.loads(base)
        payload.update(schema=2, boundary_coverage=proof, cache_binding=key)
        canonical = json.dumps(payload, ensure_ascii=True, separators=(",", ":"), sort_keys=True)
        payload["proof_mac"] = hmac.new(
            secret, b"boundary-cache-v1\0" + canonical.encode(), hashlib.sha256
        ).hexdigest()
        encoded = json.dumps(payload, ensure_ascii=True, separators=(",", ":"), sort_keys=True)
        return encoded if len(encoded.encode()) <= pg.ANALYSIS_CACHE_MAX_VALUE_BYTES else None

    def deserialize(serialized, text_length):
        if not BOUNDARY_COVERAGE_ENABLED:
            return deserialize0(serialized, text_length)
        try:
            secret, key, text = _CACHE_SECRET.get(), _CACHE_KEY.get(), _CACHE_INPUT.get()
            if not isinstance(secret, bytes) or key is None or text is None:
                return None
            if len(serialized.encode()) > pg.ANALYSIS_CACHE_MAX_VALUE_BYTES:
                return None
            payload = json.loads(serialized)
            if (
                not isinstance(payload, dict)
                or set(payload)
                != set(
                    [
                        "entities",
                        "schema",
                        "text_length",
                        "boundary_coverage",
                        "cache_binding",
                        "proof_mac",
                    ]
                )
                or payload["schema"] != 2
                or payload["cache_binding"] != key
            ):
                return None
            signature = payload.pop("proof_mac")
            canonical = json.dumps(
                payload, ensure_ascii=True, separators=(",", ":"), sort_keys=True
            )
            expected = hmac.new(
                secret, b"boundary-cache-v1\0" + canonical.encode(), hashlib.sha256
            ).hexdigest()
            if not isinstance(signature, str) or not hmac.compare_digest(signature, expected):
                return None
            encoded_proof = payload.pop("boundary_coverage")
            if not isinstance(encoded_proof, dict) or "text_sha256" in encoded_proof:
                return None
            from .boundary_contract import digest

            encoded_proof = dict(encoded_proof, text_sha256=digest(text))
            proof = Coverage.parse(encoded_proof, text=text, text_length=text_length)
            payload.pop("cache_binding")
            payload["schema"] = pg.ANALYSIS_CACHE_SCHEMA_VERSION
            entities = deserialize0(json.dumps(payload), text_length)
            return (
                None
                if entities is None
                else AnalysisEntities(entities, proof, key, _ERROR_TYPE.get())
            )
        except (ValueError, TypeError, KeyError, OverflowError):
            return None

    cls._boundary_coverage_enabled = BOUNDARY_COVERAGE_ENABLED
    cls._boundary_error_type = init0.__globals__["AnalyzerUnavailableError"]
    cls._analyze_text_with_cache = cached
    cls._mask_text = mask
    cls._serialize_analysis_cache_entry = serialize
    cls._deserialize_analysis_cache_entry = staticmethod(deserialize)
