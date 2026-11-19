"""Coordinate-only Analyzer/guardrail boundary contract; no source values cached."""

from dataclasses import dataclass
import hashlib, math

SCHEMA = 1
MAX_SPANS = 4096
TYPES = frozenset(
    [
        "PERSON",
        "LOCATION",
        "ORGANIZATION",
        "LOGIN",
        "PASSWORD",
        "AUTH_TOKEN",
        "SECRET_KEY",
        "CONTRACT_NUMBER",
    ]
)


def digest(text):
    return hashlib.sha256(text.encode("utf-8", "surrogatepass")).hexdigest()


@dataclass(frozen=True)
class Coverage:
    length: int
    sha256: str
    threshold: float
    input_kind: str
    signature: str
    spans: tuple

    def wire(self):
        return dict(
            schema=SCHEMA,
            text_length=self.length,
            text_sha256=self.sha256,
            score_threshold=self.threshold,
            input_kind=self.input_kind,
            analysis_signature=self.signature,
            spans=[dict(start=a, end=b, entity_type=k, score=s) for a, b, k, s in self.spans],
        )

    @classmethod
    def create(cls, text, spans, threshold=0.35, input_kind="text", signature="0" * 64):
        unique = {}
        for a, b, k, s in spans:
            key = (a, b, k)
            unique[key] = max(float(s), unique.get(key, 0.0))
        if len(unique) > MAX_SPANS:
            raise ValueError("boundary proof budget exceeded")
        packet = dict(
            schema=SCHEMA,
            text_length=len(text),
            text_sha256=digest(text),
            score_threshold=threshold,
            input_kind=input_kind,
            analysis_signature=signature,
            spans=[
                dict(start=a, end=b, entity_type=k, score=s)
                for (a, b, k), s in sorted(unique.items())
            ],
        )
        return cls.parse(packet, text=text)

    @classmethod
    def parse(
        cls, payload, *, text=None, text_length=None, expected_kind=None, expected_threshold=None
    ):
        if not isinstance(payload, dict) or set(payload) != set(
            [
                "schema",
                "text_length",
                "text_sha256",
                "score_threshold",
                "input_kind",
                "analysis_signature",
                "spans",
            ]
        ):
            raise ValueError("invalid boundary contract")
        n = payload["text_length"]
        h = payload["text_sha256"]
        t = payload["score_threshold"]
        kind = payload["input_kind"]
        signature = payload["analysis_signature"]
        values = payload["spans"]
        if (
            type(payload["schema"]) is not int
            or payload["schema"] != SCHEMA
            or type(n) is not int
            or n < 0
            or not isinstance(h, str)
            or len(h) != 64
            or any(c not in "0123456789abcdef" for c in h)
        ):
            raise ValueError("invalid boundary contract")
        if (
            isinstance(t, bool)
            or not isinstance(t, (int, float))
            or not math.isfinite(t)
            or not 0 <= t <= 1
            or kind not in ["text", "json_arguments"]
        ):
            raise ValueError("invalid boundary contract")
        if (
            not isinstance(signature, str)
            or len(signature) != 64
            or any(c not in "0123456789abcdef" for c in signature)
        ):
            raise ValueError("invalid analyzer signature")
        if not isinstance(values, list) or len(values) > MAX_SPANS:
            raise ValueError("invalid boundary contract")
        spans = []
        for e in values:
            if not isinstance(e, dict) or set(e) != set(["start", "end", "entity_type", "score"]):
                raise ValueError("invalid boundary range")
            a, b, k, s = e["start"], e["end"], e["entity_type"], e["score"]
            if (
                type(a) is not int
                or type(b) is not int
                or not 0 <= a < b <= n
                or k not in TYPES
                or isinstance(s, bool)
                or not isinstance(s, (float, int))
                or not math.isfinite(s)
                or not t <= s <= 1
            ):
                raise ValueError("invalid boundary range")
            spans.append((a, b, k, float(s)))
        result = cls(n, h, float(t), kind, signature, tuple(spans))
        if text is not None:
            result.bind(text)
        if text_length is not None and text_length != n:
            raise ValueError("wrong boundary length")
        if (
            expected_kind is not None
            and expected_kind != kind
            or expected_threshold is not None
            and expected_threshold != t
        ):
            raise ValueError("wrong boundary request")
        return result

    def bind(self, text):
        if len(text) != self.length or digest(text) != self.sha256:
            raise ValueError("wrong boundary input")

    def missing(self, ranges, exclusions=()):
        merged = []
        for a, b in sorted([*ranges, *exclusions]):
            if merged and a <= merged[-1][1]:
                merged[-1] = (merged[-1][0], max(b, merged[-1][1]))
            else:
                merged.append((a, b))
        missing = []
        cursor = 0
        for a, b, k, s in sorted(self.spans):
            while cursor < len(merged) and merged[cursor][1] <= a:
                cursor += 1
            if cursor >= len(merged) or merged[cursor][0] > a or merged[cursor][1] < b:
                missing.append((a, b, k, s))
        return tuple(missing)
