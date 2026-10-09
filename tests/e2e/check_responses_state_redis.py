"""Atomic state contract against an explicitly selected disposable Redis."""

import asyncio
import json
import sys
import unittest
import uuid
from unittest.mock import AsyncMock

import redis.asyncio as redis

from litellm_guardrails.responses_state import (
    KEY_PREFIX, MAX_MAPPING_BYTES, ResponsesStateError, ResponsesStateStore,
)


class StateContract(unittest.IsolatedAsyncioTestCase):
    redis_url = None

    async def asyncSetUp(self):
        self.client = redis.from_url(self.redis_url, decode_responses=True)
        await self.client.ping()
        self.store = ResponsesStateStore(self.client, b"test-only-state-salt", 3600)
        self.owner = self.store.owner("synthetic-key-" + uuid.uuid4().hex)

    async def asyncTearDown(self):
        await self.client.delete(KEY_PREFIX + self.owner)
        await self.client.aclose()

    async def test_roundtrip_idempotence_opaque_keys_and_models(self):
        mapping = {"<EMAIL_ADDRESS_1>": "alpha@example.test"}
        await self.store.save(self.owner, "gpt-6-luna", "resp-public-encoded", mapping)
        await self.store.save(self.owner, "gpt-6-luna", "resp-public-encoded", mapping)
        self.assertEqual(await self.store.load(self.owner, "gpt-6-luna", "resp-public-encoded"), mapping)
        for owner, model in [(self.owner, "gpt-6-sol"), (self.store.owner("foreign-key"), "gpt-6-luna")]:
            with self.assertRaises(ResponsesStateError) as failure:
                await self.store.load(owner, model, "resp-public-encoded")
            self.assertEqual(failure.exception.status, 409)
        fields = await self.client.hkeys(KEY_PREFIX + self.owner)
        self.assertEqual(len(fields), 2)
        self.assertTrue(all("alpha" not in value and "resp-public" not in value for value in fields))

    async def test_clean_turns_deduplicate_and_bound_reference_growth(self):
        mapping = {"<EMAIL_ADDRESS_1>": "alpha@example.test"}
        for i in range(500):
            await self.store.save(self.owner, "gpt-6-luna", f"response-{i}", mapping)
        fields = await self.client.hgetall(KEY_PREFIX + self.owner)
        self.assertEqual(len(fields), 2)
        self.assertEqual(len(json.loads(fields["references"])), 128)
        self.assertEqual(await self.store.load(self.owner, "gpt-6-luna", "response-499"), mapping)
        with self.assertRaises(ResponsesStateError):
            await self.store.load(self.owner, "gpt-6-luna", "response-0")
        print(json.dumps({"clean_turns": 500, "references": 128, "snapshots": 1,
                          "serialized_bytes": sum(len(value.encode()) for value in fields.values())}))

    async def test_expiry_is_per_response_even_when_child_keeps_owner_alive(self):
        self.store.ttl = 2
        await self.store.save(self.owner, "model", "parent", {"<A_1>": "old"})
        await asyncio.sleep(1.1)
        self.store.ttl = 10
        await self.store.save(self.owner, "model", "child", {"<A_1>": "old", "<A_2>": "new"})
        await asyncio.sleep(2)
        with self.assertRaises(ResponsesStateError):
            await self.store.load(self.owner, "model", "parent")
        self.assertEqual(await self.store.load(self.owner, "model", "child"), {"<A_1>": "old", "<A_2>": "new"})
        self.assertEqual(len(await self.client.hkeys(KEY_PREFIX + self.owner)), 2)

    async def test_budget_prunes_oldest_snapshots_not_latest(self):
        self.store.max_owner_bytes = 2 * MAX_MAPPING_BYTES
        for i in range(4):
            await self.store.save(self.owner, "model", str(i), {"<A_1>": str(i) + "x" * 700_000})
        fields = await self.client.hgetall(KEY_PREFIX + self.owner)
        self.assertLessEqual(sum(len(value.encode()) for value in fields.values()), self.store.max_owner_bytes)
        self.assertEqual(len(json.loads(fields["references"])), 2)
        with self.assertRaises(ResponsesStateError):
            await self.store.load(self.owner, "model", "1")
        self.assertTrue((await self.store.load(self.owner, "model", "3"))["<A_1>"].startswith("3"))

    async def test_concurrent_instances_do_not_lose_references_or_change_parent(self):
        other = ResponsesStateStore(self.client, self.store.secret, self.store.ttl)
        await self.store.save(self.owner, "model", "parent", {"<A_1>": "parent"})
        await asyncio.gather(*[
            (other if i % 2 else self.store).save(self.owner, "model", f"branch-{i}", {"<A_1>": "parent", "<A_2>": str(i)})
            for i in range(64)
        ])
        self.assertEqual(len(json.loads(await self.client.hget(KEY_PREFIX + self.owner, "references"))), 65)
        self.assertEqual(await self.store.load(self.owner, "model", "parent"), {"<A_1>": "parent"})
        for i in range(64):
            self.assertEqual((await other.load(self.owner, "model", f"branch-{i}"))["<A_2>"], str(i))

    async def test_collision_and_oversize_do_not_mutate_successful_parent(self):
        await self.store.save(self.owner, "model", "parent", {"<A_1>": "old"})
        for response_id, mapping, expected in [
            ("parent", {"<A_1>": "different"}, 409),
            ("oversized", {"<A_1>": "x" * MAX_MAPPING_BYTES}, 413),
        ]:
            with self.assertRaises(ResponsesStateError) as failure:
                await self.store.save(self.owner, "model", response_id, mapping)
            self.assertEqual(failure.exception.status, expected)
        self.assertEqual(await self.store.load(self.owner, "model", "parent"), {"<A_1>": "old"})
        self.assertEqual(len(await self.client.hkeys(KEY_PREFIX + self.owner)), 2)

    async def test_unavailable_redis_has_safe_error(self):
        self.store.redis = AsyncMock()
        self.store.redis.eval.side_effect = RuntimeError("raw-secret-must-not-appear")
        with self.assertRaises(ResponsesStateError) as failure:
            await self.store.save(self.owner, "model", "child", {})
        self.assertEqual(failure.exception.status, 503)
        self.assertNotIn("raw-secret", str(failure.exception))


if __name__ == "__main__":
    if len(sys.argv) < 2 or not sys.argv[1].startswith("redis://"):
        raise SystemExit("Provide the URL of a disposable Redis server as the first argument.")
    StateContract.redis_url = sys.argv.pop(1)
    unittest.main()
