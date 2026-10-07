"""Test-only observation of mapping cleanup and the stock 499 path."""

import time

import redis as sync_redis

from litellm_guardrails.pii_guardrail import RuPIIGuardrail


class ObservedGuardrail(RuPIIGuardrail):
    # LiteLLM detects iterator overrides in the leaf class, not its MRO.
    async_post_call_streaming_iterator_hook = RuPIIGuardrail.async_post_call_streaming_iterator_hook

    def _observe_stream(self, request_id, field):
        # Synchronous test-only observation must not shield the actual cleanup.
        client = sync_redis.Redis.from_url(
            "redis://redis:6379", decode_responses=True,
            socket_connect_timeout=1, socket_timeout=1,
        )
        try:
            key = f"stream_observation:{request_id}"
            client.hset(key, field, time.monotonic())
            client.expire(key, 120)
        finally:
            client.close()

    async def _load_mapping(self, request_id):
        if not hasattr(self, "_observed_loads"):
            self._observed_loads = set()
        self._observed_loads.add(request_id)
        self._observe_stream(request_id, "load_entered_at")
        result = await super()._load_mapping(request_id)
        self._observe_stream(request_id, "loaded_at")
        return result

    async def _delete_mapping(self, request_id):
        observe = request_id in getattr(self, "_observed_loads", ())
        if observe:
            self._observe_stream(request_id, "delete_entered_at")
        await super()._delete_mapping(request_id)
        if observe:
            self._observe_stream(request_id, "deleted_at")
            self._observed_loads.discard(request_id)

    async def async_post_call_failure_hook(
        self, request_data, original_exception, user_api_key_dict, traceback_str=None
    ):
        request_id = self._get_response_request_id(request_data)
        observe = (
            self.event_hook in (None, "pre_call")
            and request_id
            and getattr(original_exception, "status_code", None) == 499
        )
        if observe:
            redis = await self._get_redis()
            await redis.hset(
                f"disconnect_observation:{request_id}", "detected_at", time.monotonic()
            )
            await redis.expire(f"disconnect_observation:{request_id}", 120)
        result = await super().async_post_call_failure_hook(
            request_data, original_exception, user_api_key_dict, traceback_str
        )
        if observe:
            await redis.hset(
                f"disconnect_observation:{request_id}",
                "cleanup_returned_at", time.monotonic(),
            )
        return result
