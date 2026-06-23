"""Custom LiteLLM guardrail for Russian PII masking via Presidio."""

import os
import uuid
import json
import logging
import re
import time
from typing import Optional, Union, Any

import httpx
import litellm
from litellm.integrations.custom_guardrail import CustomGuardrail
from litellm.proxy._types import UserAPIKeyAuth
from litellm.caching.caching import DualCache

logger = logging.getLogger(__name__)

try:
    from prometheus_client import Counter, Histogram
except Exception:
    Counter = None
    Histogram = None


class _NoopMetric:
    """Fallback metric used when prometheus_client is unavailable."""

    def labels(self, *args, **kwargs):
        return self

    def inc(self, amount: float = 1):
        return None

    def observe(self, amount: float):
        return None


class _StreamingPlaceholderReplacer:
    """Chunk-safe placeholder replacement for independently streamed text fields."""

    def __init__(self, mapping: dict[str, str]):
        self._mapping = mapping
        self._placeholders = sorted(mapping, key=len, reverse=True)
        self._pending: dict[tuple[int, str], str] = {}

    def push(self, key: tuple[int, str], text: str) -> str:
        """Return text safe to emit now, keeping possible placeholder prefixes."""
        combined = self._pending.get(key, "") + text
        emit, pending = self._split_safe_prefix(combined)
        self._pending[key] = pending
        return self._replace(emit)

    def flush(self, key: tuple[int, str]) -> str:
        """Flush pending text for one stream field."""
        pending = self._pending.pop(key, "")
        return self._replace(pending)

    def flush_choice(self, choice_index: int) -> dict[str, str]:
        """Flush all pending fields for one choice."""
        flushed: dict[str, str] = {}
        for key in list(self._pending):
            key_choice_index, field = key
            if key_choice_index != choice_index:
                continue
            value = self.flush(key)
            if value:
                flushed[field] = flushed.get(field, "") + value
        return flushed

    def flush_all(self) -> dict[tuple[int, str], str]:
        """Flush all remaining pending fields."""
        flushed: dict[tuple[int, str], str] = {}
        for key in list(self._pending):
            value = self.flush(key)
            if value:
                flushed[key] = value
        return flushed

    def _split_safe_prefix(self, text: str) -> tuple[str, str]:
        keep = 0
        for length in range(1, len(text) + 1):
            suffix = text[-length:]
            if any(
                suffix != placeholder and placeholder.startswith(suffix)
                for placeholder in self._placeholders
            ):
                keep = length
        if keep == 0:
            return text, ""
        return text[:-keep], text[-keep:]

    def _replace(self, text: str) -> str:
        for placeholder in self._placeholders:
            text = text.replace(placeholder, self._mapping[placeholder])
        return text


def _build_metric(factory, *args, **kwargs):
    """Create a Prometheus metric or a no-op replacement."""
    if factory is None:
        return _NoopMetric()
    try:
        return factory(*args, **kwargs)
    except ValueError:
        logger.warning("Prometheus metric already registered, using no-op for %s", args[0])
        return _NoopMetric()


PII_PRE_CALLS = _build_metric(
    Counter,
    "ru_pii_guardrail_pre_calls",
    "PII guardrail pre-call requests.",
    ["result"],
)
PII_POST_CALLS = _build_metric(
    Counter,
    "ru_pii_guardrail_post_calls",
    "PII guardrail post-call requests.",
    ["result"],
)
PII_ENTITIES_DETECTED = _build_metric(
    Counter,
    "ru_pii_guardrail_entities_detected",
    "PII entities masked by entity type.",
    ["entity_type"],
)
PII_FAIL_OPEN = _build_metric(
    Counter,
    "ru_pii_guardrail_fail_open",
    "PII guardrail failures handled in fail-open mode.",
    ["operation"],
)
PII_FAIL_CLOSED = _build_metric(
    Counter,
    "ru_pii_guardrail_fail_closed",
    "PII guardrail failures handled in fail-closed mode.",
    ["operation"],
)
PII_ANALYZER_LATENCY = _build_metric(
    Histogram,
    "ru_pii_guardrail_analyzer_latency_seconds",
    "Latency of Presidio Analyzer calls made by the PII guardrail.",
    buckets=(0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30),
)
PII_REDIS_LATENCY = _build_metric(
    Histogram,
    "ru_pii_guardrail_redis_latency_seconds",
    "Latency of Redis operations made by the PII guardrail.",
    ["operation"],
    buckets=(0.001, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5),
)
PII_MAPPING_SIZE = _build_metric(
    Histogram,
    "ru_pii_guardrail_mapping_size",
    "Number of placeholder mappings saved for a masked request.",
    buckets=(1, 2, 5, 10, 25, 50, 100, 250),
)
PII_BLOCKED_ENTITIES = _build_metric(
    Counter,
    "ru_pii_guardrail_blocked",
    "PII entities blocked by entity type.",
    ["entity_type"],
)

# Presidio Analyzer service URL from environment
PRESIDIO_ANALYZER_URL = os.getenv("PRESIDIO_ANALYZER_URL", "http://presidio-analyzer:5001")

FAILURE_MODES = {"fail_open", "fail_closed"}
POLICY_MODES = {"mask", "block"}
DEFAULT_PII_MAPPING_TTL_SECONDS = 3600
PII_BLOCKED_MESSAGE = "Request contains personal data and was blocked by PII policy."
ANALYZER_OVERLOADED_MESSAGE = "PII guardrail analyzer overloaded"
PII_REQUEST_ID_METADATA_KEY = "pii_request_id"
PII_STREAMING_RESTORATION_DONE_METADATA_KEY = "pii_streaming_restoration_done"


class AnalyzerOverloadedError(RuntimeError):
    """Raised when the Presidio Analyzer rejects work due to capacity policy."""

    def __init__(self, reason: str = "unknown"):
        super().__init__(f"Presidio Analyzer overloaded: {reason}")
        self.reason = reason


def _get_int_env(name: str, default: int) -> int:
    """Read integer environment variable with a safe fallback."""
    raw_value = os.getenv(name)
    if raw_value is None:
        return default
    try:
        value = int(raw_value)
    except ValueError:
        logger.warning("Invalid %s=%r, falling back to %s", name, raw_value, default)
        return default
    return _normalize_positive_int(name, value, default)


def _normalize_positive_int(name: str, value: int, default: int) -> int:
    """Return value when positive, otherwise log and return default."""
    if value > 0:
        return value
    logger.warning("Invalid %s=%r, falling back to %s", name, value, default)
    return default


# Redis for storing PII mappings (for unmasking responses)
REDIS_URL = os.getenv("REDIS_URL", "redis://redis:6379")
PII_MAPPING_TTL_SECONDS = _get_int_env(
    "PII_MAPPING_TTL_SECONDS",
    DEFAULT_PII_MAPPING_TTL_SECONDS,
)
PII_GUARDRAIL_FAILURE_MODE = os.getenv("PII_GUARDRAIL_FAILURE_MODE", "fail_open")
PII_GUARDRAIL_MODE = os.getenv("PII_GUARDRAIL_MODE", "mask")


def _safe_log(level: int, event: str, **fields) -> None:
    """Write structured logs without prompt text or raw PII values."""
    logger.log(
        level,
        json.dumps(
            {"event": event, **fields},
            ensure_ascii=False,
            sort_keys=True,
        ),
    )


class RuPIIGuardrail(CustomGuardrail):
    """LiteLLM custom guardrail that masks or blocks Russian PII using Presidio.

    Flow:
    1. async_pre_call_hook: mask PII and save mapping, or block PII requests
    2. async_post_call_success_hook: unmask PII in responses when mapping exists
    """

    def __init__(
        self,
        failure_mode: Optional[str] = None,
        pii_mode: Optional[str] = None,
        mapping_ttl_seconds: Optional[int] = None,
        **kwargs,
    ):
        self._redis = None
        self.failure_mode = self._normalize_failure_mode(
            failure_mode or PII_GUARDRAIL_FAILURE_MODE
        )
        self.pii_mode = self._normalize_policy_mode(pii_mode or PII_GUARDRAIL_MODE)
        self.mapping_ttl_seconds = _normalize_positive_int(
            "PII_MAPPING_TTL_SECONDS",
            mapping_ttl_seconds
            if mapping_ttl_seconds is not None
            else PII_MAPPING_TTL_SECONDS,
            PII_MAPPING_TTL_SECONDS,
        )
        super().__init__(**kwargs)

    @staticmethod
    def _normalize_failure_mode(value: str) -> str:
        """Normalize and validate guardrail failure behavior."""
        mode = value.strip().lower().replace("-", "_")
        if mode not in FAILURE_MODES:
            logger.warning(
                "Unknown PII_GUARDRAIL_FAILURE_MODE=%r, falling back to fail_open",
                value,
            )
            return "fail_open"
        return mode

    @staticmethod
    def _normalize_policy_mode(value: str) -> str:
        """Normalize and validate normal PII policy behavior."""
        mode = value.strip().lower().replace("-", "_")
        if mode not in POLICY_MODES:
            logger.warning(
                "Unknown PII_GUARDRAIL_MODE=%r, falling back to mask",
                value,
            )
            return "mask"
        return mode

    def _handle_failure(self, operation: str, error: Exception, data: dict) -> dict:
        """Apply configured fail-open/fail-closed behavior."""
        operation_label = operation.replace(" ", "_")
        if self.failure_mode == "fail_closed":
            PII_FAIL_CLOSED.labels(operation=operation_label).inc()
        else:
            PII_FAIL_OPEN.labels(operation=operation_label).inc()
        _safe_log(
            logging.ERROR,
            "pii_guardrail_failed_open"
            if self.failure_mode == "fail_open"
            else "pii_guardrail_failed_closed",
            operation=operation_label,
            failure_mode=self.failure_mode,
            error_type=type(error).__name__,
        )
        if self.failure_mode == "fail_closed":
            raise RuntimeError(f"PII guardrail {operation} failed") from error
        return data

    def _raise_analyzer_overloaded(self, error: AnalyzerOverloadedError) -> None:
        """Fail closed on capacity overload even when other failures fail open."""
        PII_FAIL_CLOSED.labels(operation="analyzer_overloaded").inc()
        _safe_log(
            logging.ERROR,
            "pii_guardrail_analyzer_overloaded",
            failure_mode="fail_closed",
            reason=error.reason,
        )
        raise RuntimeError(ANALYZER_OVERLOADED_MESSAGE) from error

    async def _get_redis(self):
        """Lazy Redis connection."""
        if self._redis is None:
            import redis.asyncio as aioredis
            self._redis = aioredis.from_url(REDIS_URL, decode_responses=True)
        return self._redis

    @staticmethod
    def _iter_text_content_block_targets(blocks: list) -> list[tuple[dict, str]]:
        """Return mutable text fields from OpenAI-style content block lists."""
        targets: list[tuple[dict, str]] = []
        for block in blocks:
            if not isinstance(block, dict):
                continue
            block_type = block.get("type")
            if block_type in (
                None,
                "text",
                "input_text",
                "output_text",
            ) and isinstance(
                block.get("text"),
                str,
            ):
                targets.append((block, "text"))
        return targets

    @classmethod
    def _iter_message_text_targets(cls, message: dict) -> list[tuple[dict, str]]:
        """Return mutable text fields that are safe to send through analyzer."""
        if not isinstance(message, dict):
            return []

        targets: list[tuple[dict, str]] = []
        content = message.get("content")
        if isinstance(content, str):
            targets.append((message, "content"))
        elif isinstance(content, list):
            targets.extend(cls._iter_text_content_block_targets(content))

        tool_calls = message.get("tool_calls")
        if isinstance(tool_calls, list):
            for tool_call in tool_calls:
                if not isinstance(tool_call, dict):
                    continue
                function = tool_call.get("function")
                if isinstance(function, dict) and isinstance(
                    function.get("arguments"),
                    str,
                ):
                    targets.append((function, "arguments"))

        function_call = message.get("function_call")
        if isinstance(function_call, dict) and isinstance(
            function_call.get("arguments"),
            str,
        ):
            targets.append((function_call, "arguments"))

        return targets

    @classmethod
    def _iter_responses_input_item_text_targets(
        cls,
        items: list,
    ) -> list[tuple[dict, str]]:
        """Return mutable text fields from Responses API input item lists."""
        targets: list[tuple[dict, str]] = []
        for item in items:
            if not isinstance(item, dict):
                continue

            item_type = item.get("type")
            if item_type in ("text", "input_text", "output_text") and isinstance(
                item.get("text"),
                str,
            ):
                targets.append((item, "text"))

            targets.extend(cls._iter_message_text_targets(item))

            if isinstance(item.get("arguments"), str):
                targets.append((item, "arguments"))

            output = item.get("output")
            if isinstance(output, str):
                targets.append((item, "output"))
            elif isinstance(output, list):
                targets.extend(cls._iter_text_content_block_targets(output))

        return targets

    @classmethod
    def _iter_responses_field_text_targets(
        cls,
        data: dict,
        field: str,
    ) -> list[tuple[dict, str]]:
        """Return mutable Responses API text fields for a top-level field."""
        if not isinstance(data, dict) or field not in data:
            return []

        value = data.get(field)
        if isinstance(value, str):
            return [(data, field)]

        if isinstance(value, list):
            return cls._iter_responses_input_item_text_targets(value)

        return []

    @classmethod
    def _iter_request_text_targets(cls, data: dict) -> list[tuple[dict, str]]:
        """Return request text fields covered by the guardrail."""
        targets: list[tuple[dict, str]] = []

        messages = data.get("messages")
        if isinstance(messages, list):
            for message in messages:
                targets.extend(cls._iter_message_text_targets(message))

        targets.extend(cls._iter_responses_field_text_targets(data, "instructions"))
        targets.extend(cls._iter_responses_field_text_targets(data, "input"))
        return targets

    @staticmethod
    def _get_container_field(container: Any, field: str) -> Any:
        """Read a field from dict-like or object-like LiteLLM containers."""
        if isinstance(container, dict):
            return container.get(field)
        return getattr(container, field, None)

    @staticmethod
    def _set_container_field(container: Any, field: str, value: Any) -> None:
        """Write a field to dict-like or object-like LiteLLM containers."""
        if isinstance(container, dict):
            container[field] = value
        else:
            setattr(container, field, value)

    @classmethod
    def _iter_response_text_targets(cls, message: Any) -> list[tuple[Any, str]]:
        """Return mutable response fields that may contain PII placeholders."""
        if message is None:
            return []

        targets: list[tuple[Any, str]] = []
        content = cls._get_container_field(message, "content")
        if isinstance(content, str):
            targets.append((message, "content"))
        elif isinstance(content, list):
            for block in content:
                block_text = cls._get_container_field(block, "text")
                if isinstance(block_text, str):
                    targets.append((block, "text"))

        reasoning_content = cls._get_container_field(message, "reasoning_content")
        if isinstance(reasoning_content, str):
            targets.append((message, "reasoning_content"))

        tool_calls = cls._get_container_field(message, "tool_calls")
        if isinstance(tool_calls, list):
            for tool_call in tool_calls:
                function = cls._get_container_field(tool_call, "function")
                arguments = cls._get_container_field(function, "arguments")
                if isinstance(arguments, str):
                    targets.append((function, "arguments"))

        function_call = cls._get_container_field(message, "function_call")
        arguments = cls._get_container_field(function_call, "arguments")
        if isinstance(arguments, str):
            targets.append((function_call, "arguments"))

        return targets

    async def _analyze_text(self, text: str) -> list[dict]:
        """Send text to Presidio Analyzer for PII detection."""
        started_at = time.perf_counter()
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                response = await client.post(
                    f"{PRESIDIO_ANALYZER_URL}/api/v1/analyze",
                    json={"text": text, "language": "ru", "score_threshold": 0.35},
                )
                if response.status_code == 503:
                    try:
                        detail = response.json().get("detail", {})
                    except ValueError:
                        detail = {}
                    if isinstance(detail, dict) and (
                        detail.get("code") == "analyzer_overloaded"
                    ):
                        raise AnalyzerOverloadedError(
                            reason=str(detail.get("reason") or "unknown")
                        )
                response.raise_for_status()
                data = response.json()
                return data.get("entities", [])
        finally:
            PII_ANALYZER_LATENCY.observe(time.perf_counter() - started_at)

    def _mask_text(
        self,
        text: str,
        entities: list[dict],
        entity_counts: Optional[dict[str, int]] = None,
    ) -> tuple[str, dict[str, str]]:
        """Mask text with unique placeholders and return placeholder mapping."""
        if not entities:
            return text, {}

        if entity_counts is None:
            entity_counts = {}

        normalized_entities = self._normalize_entities(text, entities)
        if not normalized_entities:
            return text, {}

        parts = []
        mapping = {}
        last_end = 0
        for start, end, entity_type in normalized_entities:
            placeholder = self._next_placeholder(entity_type, entity_counts)
            parts.append(text[last_end:start])
            parts.append(placeholder)
            mapping[placeholder] = text[start:end]
            last_end = end

        parts.append(text[last_end:])
        return "".join(parts), mapping

    def _normalize_entities(
        self,
        text: str,
        entities: list[dict],
    ) -> list[tuple[int, int, str]]:
        """Return valid non-overlapping entity spans with normalized types."""
        normalized_entities = []
        for entity in entities:
            try:
                start = int(entity["start"])
                end = int(entity["end"])
            except (KeyError, TypeError, ValueError):
                continue

            if start < 0 or end > len(text) or start >= end:
                continue

            entity_type = self._normalize_entity_type(
                str(entity.get("entity_type") or "PII")
            )
            normalized_entities.append((start, end, entity_type))

        if not normalized_entities:
            return []

        normalized_entities.sort(key=lambda item: (item[0], -(item[1] - item[0])))

        non_overlapping_entities = []
        last_end = 0
        for start, end, entity_type in normalized_entities:
            if start < last_end:
                continue
            non_overlapping_entities.append((start, end, entity_type))
            last_end = end

        return non_overlapping_entities

    @staticmethod
    def _normalize_entity_type(entity_type: str) -> str:
        """Keep placeholders predictable even for custom entity names."""
        normalized = re.sub(r"[^A-Za-z0-9_]", "_", entity_type).strip("_").upper()
        return normalized or "PII"

    @staticmethod
    def _next_placeholder(entity_type: str, entity_counts: dict[str, int]) -> str:
        """Return the next request-scoped placeholder for an entity type."""
        next_index = entity_counts.get(entity_type, 0) + 1
        entity_counts[entity_type] = next_index
        return f"<{entity_type}_{next_index}>"

    async def _save_mapping(self, request_id: str, mapping: dict):
        """Save PII mapping to Redis with TTL."""
        if not mapping:
            return
        r = await self._get_redis()
        started_at = time.perf_counter()
        try:
            await r.setex(
                f"pii_mapping:{request_id}",
                self.mapping_ttl_seconds,
                json.dumps(mapping, ensure_ascii=False),
            )
        finally:
            PII_REDIS_LATENCY.labels(operation="save").observe(
                time.perf_counter() - started_at
            )

    async def _load_mapping(self, request_id: str) -> dict:
        """Load PII mapping from Redis."""
        r = await self._get_redis()
        started_at = time.perf_counter()
        try:
            data = await r.get(f"pii_mapping:{request_id}")
        finally:
            PII_REDIS_LATENCY.labels(operation="load").observe(
                time.perf_counter() - started_at
            )
        if data:
            return json.loads(data)
        return {}

    async def _delete_mapping(self, request_id: str) -> None:
        """Delete PII mapping from Redis after post-call processing."""
        r = await self._get_redis()
        started_at = time.perf_counter()
        try:
            await r.delete(f"pii_mapping:{request_id}")
        finally:
            PII_REDIS_LATENCY.labels(operation="delete").observe(
                time.perf_counter() - started_at
            )

    def _get_request_id(self, data: dict) -> str:
        """Generate a server-side mapping ID for request-scoped PII."""
        return str(uuid.uuid4())

    @staticmethod
    def _get_response_request_id(data: dict) -> Optional[str]:
        """Return request ID used for post-call response restoration."""
        metadata = data.get("metadata", {})
        if not isinstance(metadata, dict):
            return None
        request_id = metadata.get(PII_REQUEST_ID_METADATA_KEY)
        if not request_id:
            return None
        return str(request_id)

    @staticmethod
    def _mark_streaming_restoration_done(data: dict) -> None:
        """Mark that streaming iterator hook already handled post-call restoration."""
        if not isinstance(data.get("metadata"), dict):
            data["metadata"] = {}
        data["metadata"][PII_STREAMING_RESTORATION_DONE_METADATA_KEY] = True

    @staticmethod
    def _streaming_restoration_done(data: dict) -> bool:
        """Return whether streaming iterator hook already handled this response."""
        metadata = data.get("metadata", {})
        return (
            isinstance(metadata, dict)
            and metadata.get(PII_STREAMING_RESTORATION_DONE_METADATA_KEY) is True
        )

    @staticmethod
    def _clear_inbound_pii_metadata(data: dict) -> None:
        """Remove caller-supplied guardrail-internal metadata from a new request."""
        metadata = data.get("metadata")
        if not isinstance(metadata, dict):
            return
        metadata.pop(PII_REQUEST_ID_METADATA_KEY, None)
        metadata.pop(PII_STREAMING_RESTORATION_DONE_METADATA_KEY, None)

    @staticmethod
    def _entity_counts_from_mapping(mapping: dict[str, str]) -> dict[str, int]:
        """Return entity counts derived from generated placeholders."""
        counts: dict[str, int] = {}
        for placeholder in mapping:
            entity_type = placeholder.strip("<>").rsplit("_", 1)[0] or "PII"
            counts[entity_type] = counts.get(entity_type, 0) + 1
        return counts

    @staticmethod
    def _record_entities(entity_counts: dict[str, int]) -> None:
        """Increment entity metrics with bounded entity_type labels."""
        for entity_type, count in entity_counts.items():
            PII_ENTITIES_DETECTED.labels(entity_type=entity_type).inc(count)

    @staticmethod
    def _record_blocked_entities(entity_counts: dict[str, int]) -> None:
        """Increment blocked entity metrics with bounded entity_type labels."""
        for entity_type, count in entity_counts.items():
            PII_BLOCKED_ENTITIES.labels(entity_type=entity_type).inc(count)

    @staticmethod
    def _entity_counts_from_normalized_entities(
        normalized_entities: list[tuple[int, int, str]],
    ) -> dict[str, int]:
        """Return entity counts without exposing source text or offsets."""
        counts: dict[str, int] = {}
        for _, _, entity_type in normalized_entities:
            counts[entity_type] = counts.get(entity_type, 0) + 1
        return counts

    def _raise_blocked_request(
        self,
        data: dict,
        request_id: str,
        entity_counts: dict[str, int],
    ) -> None:
        """Raise a LiteLLM-compatible client error without raw PII."""
        entity_types = sorted(entity_counts)
        self._record_blocked_entities(entity_counts)
        PII_PRE_CALLS.labels(result="blocked").inc()
        _safe_log(
            logging.INFO,
            "pii_guardrail_blocked",
            request_id=request_id,
            entity_types=entity_types,
            entity_counts=entity_counts,
        )

        body = {
            "error": {
                "message": PII_BLOCKED_MESSAGE,
                "type": "pii_detected",
                "code": "pii_blocked",
                "details": {"entities": entity_types},
            }
        }
        response = httpx.Response(
            status_code=422,
            json=body,
            request=httpx.Request("POST", "http://ru-llm-proxy.local/pii-policy"),
        )
        raise litellm.UnprocessableEntityError(
            message=PII_BLOCKED_MESSAGE,
            model=str(data.get("model") or "unknown"),
            llm_provider="ru-llm-proxy",
            response=response,
        )

    async def async_pre_call_hook(
        self,
        user_api_key_dict: UserAPIKeyAuth,
        cache: DualCache,
        data: dict,
        call_type: Optional[str] = None,
    ) -> Optional[Union[Exception, str, dict]]:
        """Apply request-time PII policy before sending to LLM."""
        self._clear_inbound_pii_metadata(data)
        request_targets = self._iter_request_text_targets(data)
        if not request_targets:
            PII_PRE_CALLS.labels(result="skipped").inc()
            return data

        request_id = self._get_request_id(data)
        full_mapping = {}
        entity_counts: dict[str, int] = {}
        blocked_entity_counts: dict[str, int] = {}
        pending_updates = []

        for target, field in request_targets:
            content = target[field]
            if not content.strip():
                continue

            try:
                entities = await self._analyze_text(content)
                if not entities:
                    continue

                if self.pii_mode == "block":
                    normalized_entities = self._normalize_entities(content, entities)
                    entity_counts_for_block = (
                        self._entity_counts_from_normalized_entities(
                            normalized_entities
                        )
                    )
                    for entity_type, count in entity_counts_for_block.items():
                        blocked_entity_counts[entity_type] = (
                            blocked_entity_counts.get(entity_type, 0) + count
                        )
                    continue

                masked_text, mapping = self._mask_text(
                    content,
                    entities,
                    entity_counts,
                )
                if mapping:
                    full_mapping.update(mapping)
                    pending_updates.append((target, field, masked_text))

            except AnalyzerOverloadedError as e:
                PII_PRE_CALLS.labels(result="error").inc()
                self._raise_analyzer_overloaded(e)
            except Exception as e:
                if self.pii_mode == "block" and blocked_entity_counts:
                    self._raise_blocked_request(
                        data,
                        request_id,
                        blocked_entity_counts,
                    )
                PII_PRE_CALLS.labels(result="error").inc()
                return self._handle_failure("masking", e, data)

        if blocked_entity_counts:
            self._raise_blocked_request(data, request_id, blocked_entity_counts)

        # Save mapping for post-call unmasking
        if full_mapping:
            try:
                await self._save_mapping(request_id, full_mapping)
            except Exception as e:
                PII_PRE_CALLS.labels(result="error").inc()
                return self._handle_failure("mapping save", e, data)

            for target, field, masked_text in pending_updates:
                target[field] = masked_text

            # Store internal mapping ID in data for post-call hooks.
            if not isinstance(data.get("metadata"), dict):
                data["metadata"] = {}
            data["metadata"][PII_REQUEST_ID_METADATA_KEY] = request_id
            entity_counts_for_log = self._entity_counts_from_mapping(full_mapping)
            self._record_entities(entity_counts_for_log)
            PII_MAPPING_SIZE.observe(len(full_mapping))
            PII_PRE_CALLS.labels(result="masked").inc()
            _safe_log(
                logging.INFO,
                "pii_guardrail_masked",
                request_id=request_id,
                masked_count=len(full_mapping),
                entity_counts=entity_counts_for_log,
                mapping_ttl_seconds=self.mapping_ttl_seconds,
            )
        else:
            PII_PRE_CALLS.labels(result="clean").inc()

        return data

    async def async_post_call_success_hook(
        self,
        data: dict,
        user_api_key_dict: UserAPIKeyAuth,
        response: Any,
    ) -> None:
        """Unmask PII in response after receiving from LLM."""
        if data.get("stream") is True and (
            self._streaming_restoration_done(data)
            or not isinstance(response, litellm.ModelResponse)
        ):
            PII_POST_CALLS.labels(result="skipped").inc()
            return

        request_id = self._get_response_request_id(data)
        if not request_id:
            PII_POST_CALLS.labels(result="skipped").inc()
            return

        # Load mapping
        try:
            mapping = await self._load_mapping(request_id)
        except Exception as e:
            PII_POST_CALLS.labels(result="error").inc()
            self._handle_failure("mapping load", e, data)
            return

        if not mapping:
            PII_POST_CALLS.labels(result="no_mapping").inc()
            _safe_log(
                logging.INFO,
                "pii_guardrail_no_mapping",
                request_id=request_id,
            )
            return

        # Unmask in response
        restored_fields = 0
        if isinstance(response, litellm.ModelResponse):
            for choice in response.choices:
                message = getattr(choice, "message", None)
                for target, field in self._iter_response_text_targets(message):
                    original_value = self._get_container_field(target, field)
                    restored_value = self._replace_placeholders(
                        original_value,
                        mapping,
                    )
                    if restored_value != original_value:
                        restored_fields += 1
                        self._set_container_field(target, field, restored_value)
        else:
            PII_POST_CALLS.labels(result="unsupported_response").inc()
            _safe_log(
                logging.WARNING,
                "pii_guardrail_unsupported_response",
                request_id=request_id,
                response_type=type(response).__name__,
                mapping_size=len(mapping),
            )
            return

        # Clean up Redis key
        try:
            await self._delete_mapping(request_id)
        except Exception as e:
            PII_FAIL_OPEN.labels(operation="mapping_delete").inc()
            _safe_log(
                logging.WARNING,
                "pii_guardrail_cleanup_failed",
                request_id=request_id,
                error_type=type(e).__name__,
            )

        PII_POST_CALLS.labels(
            result="restored" if restored_fields else "no_placeholders"
        ).inc()
        _safe_log(
            logging.INFO,
            "pii_guardrail_restored",
            request_id=request_id,
            mapping_size=len(mapping),
            restored_fields=restored_fields,
        )

    @classmethod
    def _iter_stream_delta_text_targets(
        cls,
        chunk: Any,
    ) -> list[tuple[int, Any, str]]:
        """Return stream delta text fields that can contain placeholders."""
        targets: list[tuple[int, Any, str]] = []
        choices = getattr(chunk, "choices", None)
        if not isinstance(choices, list):
            return targets

        for choice in choices:
            try:
                choice_index = int(cls._get_container_field(choice, "index") or 0)
            except (TypeError, ValueError):
                choice_index = 0

            delta = cls._get_container_field(choice, "delta")
            if delta is None:
                continue

            for field in ("content", "reasoning_content"):
                value = cls._get_container_field(delta, field)
                if isinstance(value, str):
                    targets.append((choice_index, delta, field))

        return targets

    @classmethod
    def _append_stream_delta_text(cls, delta: Any, field: str, text: str) -> None:
        """Append restored trailing text to a stream delta field."""
        current_value = cls._get_container_field(delta, field)
        if isinstance(current_value, str):
            cls._set_container_field(delta, field, current_value + text)
        else:
            cls._set_container_field(delta, field, text)

    def _flush_stream_choice(
        self,
        choice: Any,
        replacer: _StreamingPlaceholderReplacer,
    ) -> None:
        """Flush pending text for a finished streaming choice onto its delta."""
        try:
            choice_index = int(self._get_container_field(choice, "index") or 0)
        except (TypeError, ValueError):
            choice_index = 0

        delta = self._get_container_field(choice, "delta")
        if delta is None:
            return

        for field, text in replacer.flush_choice(choice_index).items():
            self._append_stream_delta_text(delta, field, text)

    @staticmethod
    def _build_stream_flush_chunk(
        choice_index: int,
        field: str,
        text: str,
    ) -> Any:
        """Build a final stream chunk for pending text when no finish chunk exists."""
        return litellm.ModelResponseStream(
            choices=[
                litellm.StreamingChoices(
                    index=choice_index,
                    delta={field: text},
                )
            ]
        )

    async def async_post_call_streaming_iterator_hook(
        self,
        user_api_key_dict: UserAPIKeyAuth,
        response: Any,
        request_data: dict,
    ):
        """Unmask placeholders in streaming response chunks."""
        request_id = self._get_response_request_id(request_data)
        if not request_id:
            PII_POST_CALLS.labels(result="skipped").inc()
            async for item in response:
                yield item
            return

        try:
            mapping = await self._load_mapping(request_id)
        except Exception as e:
            PII_POST_CALLS.labels(result="error").inc()
            self._handle_failure("stream mapping load", e, request_data)
            async for item in response:
                yield item
            return

        if not mapping:
            PII_POST_CALLS.labels(result="no_mapping").inc()
            _safe_log(
                logging.INFO,
                "pii_guardrail_no_mapping",
                request_id=request_id,
            )
            self._mark_streaming_restoration_done(request_data)
            async for item in response:
                yield item
            return

        replacer = _StreamingPlaceholderReplacer(mapping)
        restored_fields = 0
        try:
            async for item in response:
                for choice_index, target, field in self._iter_stream_delta_text_targets(
                    item
                ):
                    original_value = self._get_container_field(target, field)
                    restored_value = replacer.push(
                        (choice_index, field),
                        original_value,
                    )
                    if restored_value != original_value:
                        restored_fields += 1
                    self._set_container_field(target, field, restored_value)

                choices = getattr(item, "choices", None)
                if isinstance(choices, list):
                    for choice in choices:
                        if self._get_container_field(choice, "finish_reason") is not None:
                            self._flush_stream_choice(choice, replacer)

                yield item

            for (choice_index, field), text in replacer.flush_all().items():
                restored_fields += 1
                yield self._build_stream_flush_chunk(choice_index, field, text)
        finally:
            self._mark_streaming_restoration_done(request_data)
            try:
                await self._delete_mapping(request_id)
            except Exception as e:
                PII_FAIL_OPEN.labels(operation="mapping_delete").inc()
                _safe_log(
                    logging.WARNING,
                    "pii_guardrail_cleanup_failed",
                    request_id=request_id,
                    error_type=type(e).__name__,
                )

        PII_POST_CALLS.labels(
            result="restored" if restored_fields else "no_placeholders"
        ).inc()
        _safe_log(
            logging.INFO,
            "pii_guardrail_stream_restored",
            request_id=request_id,
            mapping_size=len(mapping),
            restored_fields=restored_fields,
        )

    @staticmethod
    def _replace_placeholders(text: str, mapping: dict[str, str]) -> str:
        """Replace placeholders with originals, longest keys first for stability."""
        for placeholder, original in sorted(
            mapping.items(), key=lambda item: len(item[0]), reverse=True
        ):
            text = text.replace(placeholder, original)
        return text
