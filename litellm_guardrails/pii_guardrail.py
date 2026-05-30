"""Custom LiteLLM guardrail for Russian PII masking via Presidio."""

import os
import uuid
import json
import logging
import re
from typing import Optional, Union, Any

import httpx
import litellm
from litellm.integrations.custom_guardrail import CustomGuardrail
from litellm.proxy._types import UserAPIKeyAuth
from litellm.caching.caching import DualCache

logger = logging.getLogger(__name__)

# Presidio Analyzer service URL from environment
PRESIDIO_ANALYZER_URL = os.getenv("PRESIDIO_ANALYZER_URL", "http://presidio-analyzer:5001")

FAILURE_MODES = {"fail_open", "fail_closed"}


def _get_int_env(name: str, default: int) -> int:
    """Read integer environment variable with a safe fallback."""
    raw_value = os.getenv(name)
    if raw_value is None:
        return default
    try:
        return int(raw_value)
    except ValueError:
        logger.warning("Invalid %s=%r, falling back to %s", name, raw_value, default)
        return default


# Redis for storing PII mappings (for unmasking responses)
REDIS_URL = os.getenv("REDIS_URL", "redis://redis:6379")
PII_MAPPING_TTL_SECONDS = _get_int_env("PII_MAPPING_TTL_SECONDS", 3600)
PII_GUARDRAIL_FAILURE_MODE = os.getenv("PII_GUARDRAIL_FAILURE_MODE", "fail_open")


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
        self.mapping_ttl_seconds = mapping_ttl_seconds or PII_MAPPING_TTL_SECONDS
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
        logger.error("PII guardrail %s failed: %s", operation, error)
        if self.failure_mode == "fail_closed":
            raise RuntimeError(f"PII guardrail {operation} failed") from error
        return data

    async def _get_redis(self):
        """Lazy Redis connection."""
        if self._redis is None:
            import redis.asyncio as aioredis
            self._redis = aioredis.from_url(REDIS_URL, decode_responses=True)
        return self._redis

    async def _analyze_text(self, text: str) -> list[dict]:
        """Send text to Presidio Analyzer for PII detection."""
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.post(
                f"{PRESIDIO_ANALYZER_URL}/api/v1/analyze",
                json={"text": text, "language": "ru", "score_threshold": 0.35},
            )
            response.raise_for_status()
            data = response.json()
            return data.get("entities", [])

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

        normalized_entities.sort(key=lambda item: (item[0], item[1]))

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
        await r.setex(
            f"pii_mapping:{request_id}",
            self.mapping_ttl_seconds,
            json.dumps(mapping, ensure_ascii=False),
        )

    async def _load_mapping(self, request_id: str) -> dict:
        """Load PII mapping from Redis."""
        r = await self._get_redis()
        data = await r.get(f"pii_mapping:{request_id}")
        if data:
            return json.loads(data)
        return {}

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
            return data

        request_id = self._get_request_id(data)
        full_mapping = {}
        entity_counts: dict[str, int] = {}
        pending_updates = []

        for message in messages:
            content = message.get("content")
            if not isinstance(content, str) or not content.strip():
                continue

            try:
                entities = await self._analyze_text(content)
                if not entities:
                    continue

                masked_text, mapping = self._mask_text(content, entities, entity_counts)
                if mapping:
                    full_mapping.update(mapping)
                    pending_updates.append((message, masked_text))

            except Exception as e:
                return self._handle_failure("masking", e, data)

        # Save mapping for post-call unmasking
        if full_mapping:
            try:
                await self._save_mapping(request_id, full_mapping)
            except Exception as e:
                return self._handle_failure("mapping save", e, data)

            for message, masked_text in pending_updates:
                message["content"] = masked_text

            # Store request_id in data for post-call hook
            if "metadata" not in data:
                data["metadata"] = {}
            data["metadata"]["pii_request_id"] = request_id
            logger.info(f"PII masked {len(full_mapping)} entities for request {request_id}")

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
            return
        request_id = str(request_id)

        # Load mapping
        try:
            mapping = await self._load_mapping(request_id)
        except Exception as e:
            self._handle_failure("mapping load", e, data)
            return

        if not mapping:
            return

        # Unmask in response
        if isinstance(response, litellm.ModelResponse):
            for choice in response.choices:
                message = getattr(choice, "message", None)
                if message is None:
                    continue

                if getattr(message, "content", None):
                    message.content = self._replace_placeholders(message.content, mapping)

                # GLM-5.1 coding plan can put final text in reasoning_content.
                if getattr(message, "reasoning_content", None):
                    message.reasoning_content = self._replace_placeholders(
                        message.reasoning_content,
                        mapping,
                    )

        # Clean up Redis key
        try:
            r = await self._get_redis()
            await r.delete(f"pii_mapping:{request_id}")
        except Exception as e:
            logger.warning("Failed to delete PII mapping for request %s: %s", request_id, e)

        logger.info(f"PII unmasked for request {request_id}")

    @staticmethod
    def _replace_placeholders(text: str, mapping: dict[str, str]) -> str:
        """Replace placeholders with originals, longest keys first for stability."""
        for placeholder, original in sorted(
            mapping.items(), key=lambda item: len(item[0]), reverse=True
        ):
            text = text.replace(placeholder, original)
        return text
