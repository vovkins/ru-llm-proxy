"""Exercise old and new gateway hooks in separate processes on one disposable Redis."""

import argparse
import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

from redis.asyncio import Redis

from litellm_guardrails import pii_guardrail
from litellm_guardrails.responses_state import KEY_PREFIX


MODEL = "mock-redis-upgrade"
SOURCE = "privateAlpha"
ALIAS = "publicValue"
AUTH = SimpleNamespace(token="synthetic-redis-upgrade-key")


async def turn(guard, previous, reply_id, *, key=False, full_history=False):
    payload = {"model": MODEL, "input": SOURCE, "store": False}
    if key:
        payload["input"] = [{
            "type": "function_call", "name": "Write", "call_id": "local",
            "arguments": json.dumps({SOURCE: 1}),
        }]
    elif full_history:
        payload["input"] = [
            {"role": "user", "content": SOURCE},
            {"role": "assistant", "content": SOURCE},
            {"role": "user", "content": "Continue this conversation."},
        ]
    if previous:
        payload["previous_response_id"] = previous
    await guard.async_pre_call_hook(AUTH, None, payload, "aresponses")
    if key:
        arguments = payload["input"][0]["arguments"]
        assert json.loads(arguments) == {ALIAS: 1}
        output = {"type": "function_call", "name": "Write", "call_id": "local", "arguments": arguments}
    else:
        if full_history:
            assert [item["content"] for item in payload["input"]] == [
                ALIAS, ALIAS, "Continue this conversation.",
            ]
            assert "previous_response_id" not in payload
        else:
            assert payload["input"] == ALIAS
        output = {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": ALIAS}]}
    reply = {"object": "response", "id": reply_id, "status": "completed", "output": [output]}
    await guard.async_post_call_success_hook(payload, AUTH, reply)
    if key:
        assert json.loads(output["arguments"]) == {SOURCE: 1}
    else:
        assert output["content"][0]["text"] == SOURCE
    assert not await guard._redis.keys("pii_mapping:*"), "Temporary request mapping was not deleted"


async def run(url, phase):
    pii_guardrail.LITELLM_SALT_KEY = "synthetic-redis-upgrade-salt"
    async with Redis.from_url(url, decode_responses=True, socket_timeout=2) as redis:
        await redis.ping()
        guard = pii_guardrail.RuPIIGuardrail(
            dictionary_substitutions_enabled=True,
            dictionary_substitutions_json=json.dumps({"substitutions": [
                {"id": "name", "source": SOURCE, "replacement": ALIAS},
            ]}),
        )
        guard._redis = redis
        guard._analysis_cache_secret = None
        guard._analyze_text = AsyncMock(return_value=[])
        store = await guard._responses_state_store()
        owner = store.owner(guard._analysis_cache_scope(AUTH))
        if phase == "legacy":
            await turn(guard, None, "legacy-parent")
        elif phase == "upgrade":
            await turn(guard, "legacy-parent", "new-legacy")
            await turn(guard, "new-legacy", "new-key", key=True)
            assert (await store.load(owner, MODEL, "new-key")).json_keys == {ALIAS}
        elif phase == "new-worker":
            await turn(guard, "new-key", "new-continued", key=True)
            assert (await store.load(owner, MODEL, "new-continued")).json_keys == {ALIAS}
        elif phase == "rollback":
            before = await redis.hgetall(KEY_PREFIX + owner)
            try:
                await turn(guard, "new-continued", "rollback-refused")
            except pii_guardrail.HTTPException as error:
                assert error.status_code == 503
                assert error.detail["error"]["code"] == "pii_history_storage_unavailable"
            else:
                raise AssertionError("v0.7.2 unexpectedly accepted v4 state")
            assert await redis.hgetall(KEY_PREFIX + owner) == before
            assert await redis.ttl(KEY_PREFIX + owner) > 0
            await turn(guard, "new-legacy", "rollback-legacy")
            await turn(guard, None, "rollback-full-history", full_history=True)
        print(f"Redis mapping {phase}: passed")


if __name__ == "__main__":
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument("disposable_redis_url")
    cli.add_argument("phase", choices=("legacy", "upgrade", "new-worker", "rollback"))
    args = cli.parse_args()
    asyncio.run(run(args.disposable_redis_url, args.phase))
