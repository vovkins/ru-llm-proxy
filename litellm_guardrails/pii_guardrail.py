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

# Presidio Analyzer service URL from environment
PRESIDIO_ANALYZER_URL = os.getenv("PRESIDIO_ANALYZER_URL", "http://presidio-analyzer:5001")

FAILURE_MODES = {"fail_open", "fail_closed"}
DEFAULT_PII_MAPPING_TTL_SECONDS = 3600


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
    """LiteLLM custom guardrail that masks Russian PII using Presidio.

    Flow:
    1. async_pre_call_hook: Mask PII in request → save mapping to Redis
    2. async_post_call_success_hook: Unmask PII in response using mapping
    """

    def __init__(
        self,
        failure_mode: Optional[str] = None,
        mapping_ttl_seconds: Optional[int] = None,
        **kwargs,
    ):
        self._redis = None
        self.failure_mode = self._normalize_failure_mode(
            failure_mode or PII_GUARDRAIL_FAILURE_MODE
        )
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

    async def _get_redis(self):
        """Lazy Redis connection."""
        if self._redis is None:
            import redis.asyncio as aioredis
            self._redis = aioredis.from_url(REDIS_URL, decode_responses=True)
        return self._redis

    @staticmethod
    def _iter_message_text_targets(message: dict) -> list[tuple[dict, str]]:
        """Return mutable text fields that are safe to send through analyzer."""
        if not isinstance(message, dict):
            return []

        targets: list[tuple[dict, str]] = []
        content = message.get("content")
        if isinstance(content, str):
            targets.append((message, "content"))
        elif isinstance(content, list):
            for block in content:
                if not isinstance(block, dict):
                    continue
                block_type = block.get("type")
                if block_type in (None, "text", "input_text") and isinstance(
                    block.get("text"),
                    str,
                ):
                    targets.append((block, "text"))

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

    async def _analyze_text(self, text: str) -> list[dict]:
        """Send text to Presidio Analyzer for PII detection."""
        started_at = time.perf_counter()
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                response = await client.post(
                    f"{PRESIDIO_ANALYZER_URL}/api/v1/analyze",
                    json={"text": text, "language": "ru", "score_threshold": 0.35},
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
            return text, {}

        normalized_entities.sort(key=lambda item: (item[0], -(item[1] - item[0])))

        parts = []
        mapping = {}
        last_end = 0
        for start, end, entity_type in normalized_entities:
            if start < last_end:
                continue

            placeholder = self._next_placeholder(entity_type, entity_counts)
            parts.append(text[last_end:start])
            parts.append(placeholder)
            mapping[placeholder] = text[start:end]
            last_end = end

        parts.append(text[last_end:])
        return "".join(parts), mapping

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
        """Get or generate request ID from data."""
        # LiteLLM stores request ID in various places
        if "metadata" in data and isinstance(data["metadata"], dict):
            req_id = data["metadata"].get("request_id")
            if req_id:
                return str(req_id)
        # Use litellm call id if available
        if "litellm_call_id" in data:
            return str(data["litellm_call_id"])
        # Generate new
        return str(uuid.uuid4())

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

    async def async_pre_call_hook(
        self,
        user_api_key_dict: UserAPIKeyAuth,
        cache: DualCache,
        data: dict,
        call_type: Optional[str] = None,
    ) -> Optional[Union[Exception, str, dict]]:
        """Mask PII in request messages before sending to LLM."""
        messages = data.get("messages")
        if not messages:
            PII_PRE_CALLS.labels(result="skipped").inc()
            return data

        request_id = self._get_request_id(data)
        full_mapping = {}
        entity_counts: dict[str, int] = {}
        pending_updates = []

        for message in messages:
            for target, field in self._iter_message_text_targets(message):
                content = target[field]
                if not content.strip():
                    continue

                try:
                    entities = await self._analyze_text(content)
                    if not entities:
                        continue

                    masked_text, mapping = self._mask_text(
                        content,
                        entities,
                        entity_counts,
                    )
                    if mapping:
                        full_mapping.update(mapping)
                        pending_updates.append((target, field, masked_text))

                except Exception as e:
                    PII_PRE_CALLS.labels(result="error").inc()
                    return self._handle_failure("masking", e, data)

        # Save mapping for post-call unmasking
        if full_mapping:
            try:
                await self._save_mapping(request_id, full_mapping)
            except Exception as e:
                PII_PRE_CALLS.labels(result="error").inc()
                return self._handle_failure("mapping save", e, data)

            for target, field, masked_text in pending_updates:
                target[field] = masked_text

            # Store request_id in data for post-call hook
            if not isinstance(data.get("metadata"), dict):
                data["metadata"] = {}
            data["metadata"]["pii_request_id"] = request_id
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
        # Get request ID
        metadata = data.get("metadata", {})
        request_id = (
            metadata.get("pii_request_id")
            or metadata.get("request_id")
            or data.get("litellm_call_id")
        )
        if not request_id:
            PII_POST_CALLS.labels(result="skipped").inc()
            return
        request_id = str(request_id)

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
                if message is None:
                    continue

                if getattr(message, "content", None):
                    restored_content = self._replace_placeholders(message.content, mapping)
                    if restored_content != message.content:
                        restored_fields += 1
                    message.content = restored_content

                # GLM-5.1 coding plan can put final text in reasoning_content.
                if getattr(message, "reasoning_content", None):
                    restored_reasoning = self._replace_placeholders(
                        message.reasoning_content,
                        mapping,
                    )
                    if restored_reasoning != message.reasoning_content:
                        restored_fields += 1
                    message.reasoning_content = restored_reasoning
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

    @staticmethod
    def _replace_placeholders(text: str, mapping: dict[str, str]) -> str:
        """Replace placeholders with originals, longest keys first for stability."""
        for placeholder, original in sorted(
            mapping.items(), key=lambda item: len(item[0]), reverse=True
        ):
            text = text.replace(placeholder, original)
        return text
