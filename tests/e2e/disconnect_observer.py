"""Test-only observation of the stock 499 -> guardrail cleanup path."""

import time

from litellm_guardrails.pii_guardrail import RuPIIGuardrail


class ObservedGuardrail(RuPIIGuardrail):
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
