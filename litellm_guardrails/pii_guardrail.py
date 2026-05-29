"""Custom LiteLLM guardrail for Russian PII masking via Presidio."""

import os
import uuid
import json
import logging
from typing import Optional, Union, Literal, Any

import httpx
import litellm
from litellm.integrations.custom_guardrail import CustomGuardrail
from litellm.proxy._types import UserAPIKeyAuth
from litellm.caching.caching import DualCache

logger = logging.getLogger(__name__)

# Presidio service URLs from environment
PRESIDIO_ANALYZER_URL = os.getenv("PRESIDIO_ANALYZER_URL", "http://presidio-analyzer:5001")
PRESIDIO_ANONYMIZER_URL = os.getenv("PRESIDIO_ANONYMIZER_URL", "http://presidio-anonymizer:5002")

# Redis for storing PII mappings (for unmasking responses)
REDIS_URL = os.getenv("REDIS_URL", "redis://redis:6379")


class RuPIIGuardrail(CustomGuardrail):
    """LiteLLM custom guardrail that masks Russian PII using Presidio.

    Flow:
    1. async_pre_call_hook: Mask PII in request → save mapping to Redis
    2. async_post_call_success_hook: Unmask PII in response using mapping
    """

    def __init__(self, **kwargs):
        self._redis = None
        super().__init__(**kwargs)

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

    async def _anonymize_text(self, text: str, entities: list[dict]) -> tuple[str, dict]:
        """Send text + entities to Presidio Anonymizer.

        Returns:
            Tuple of (anonymized_text, mapping of placeholder → original)
        """
        if not entities:
            return text, {}

        # Build operator config: replace each entity type with a placeholder
        entity_types = set(e["entity_type"] for e in entities)
        operators = {}
        for et in entity_types:
            operators[et] = "replace"

        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.post(
                f"{PRESIDIO_ANONYMIZER_URL}/api/v1/anonymize",
                json={
                    "text": text,
                    "entities": entities,
                    "operators": operators,
                },
            )
            response.raise_for_status()
            data = response.json()

        # Build mapping: placeholder → original text
        mapping = {}
        for item in data.get("items", []):
            placeholder = item.get("text", "")
            # Find original text from entity start/end
            for e in entities:
                if e.get("start") == item.get("start") and e.get("entity_type") == item.get("entity_type"):
                    # Original text from the analyze response
                    mapping[placeholder] = e.get("text", placeholder)
                    break

        return data.get("text", text), mapping

    async def _save_mapping(self, request_id: str, mapping: dict):
        """Save PII mapping to Redis with TTL."""
        if not mapping:
            return
        r = await self._get_redis()
        await r.setex(
            f"pii_mapping:{request_id}",
            3600,  # 1 hour TTL
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

        for message in messages:
            content = message.get("content")
            if not isinstance(content, str) or not content.strip():
                continue

            try:
                # 1. Analyze for PII
                entities = await self._analyze_text(content)
                if not entities:
                    continue

                # 2. Anonymize
                masked_text, mapping = await self._anonymize_text(content, entities)
                if mapping:
                    message["content"] = masked_text
                    full_mapping.update(mapping)

            except Exception as e:
                logger.error(f"PII masking error for message: {e}")
                # On error, continue without masking (fail open)
                continue

        # Save mapping for post-call unmasking
        if full_mapping:
            await self._save_mapping(request_id, full_mapping)
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
        request_id = metadata.get("pii_request_id")
        if not request_id:
            return

        # Load mapping
        mapping = await self._load_mapping(request_id)
        if not mapping:
            return

        # Unmask in response
        if isinstance(response, litellm.ModelResponse):
            for choice in response.choices:
                if isinstance(choice, litellm.Choices):
                    # Unmask in content
                    if choice.message.content:
                        content = choice.message.content
                        for placeholder, original in mapping.items():
                            content = content.replace(placeholder, original)
                        choice.message.content = content
                    # Unmask in reasoning_content (GLM-5.1 coding plan)
                    if hasattr(choice.message, 'reasoning_content') and choice.message.reasoning_content:
                        rc = choice.message.reasoning_content
                        for placeholder, original in mapping.items():
                            rc = rc.replace(placeholder, original)
                        choice.message.reasoning_content = rc

        # Clean up Redis key
        try:
            r = await self._get_redis()
            await r.delete(f"pii_mapping:{request_id}")
        except Exception:
            pass

        logger.info(f"PII unmasked for request {request_id}")
