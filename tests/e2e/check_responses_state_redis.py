"""Atomic state contract against an explicitly selected disposable Redis."""

import asyncio
import json
import statistics
import sys
import time
import unittest
import uuid
from unittest.mock import AsyncMock

import redis.asyncio as redis

from litellm_guardrails.responses_state import (
    KEY_PREFIX, MAX_MAPPING_BYTES, MAX_OWNER_BYTES, MAX_OWNER_REFERENCES,
    ResponsesStateError, ResponsesStateStore,
)


class StateContract(unittest.IsolatedAsyncioTestCase):
    redis_url = None

    async def asyncSetUp(self):
        self.client = redis.from_url(self.redis_url, decode_responses=True, socket_timeout=2)
        await self.client.ping()
        self.store = ResponsesStateStore(self.client, b"test-only-state-salt", 3600)
        self.owner = self.store.owner("synthetic-key-" + uuid.uuid4().hex)

    async def asyncTearDown(self):
        await self.client.delete(KEY_PREFIX + self.owner)
        await self.client.aclose()

    async def measure(self, case, operation, samples=50):
        before = (await self.client.info("commandstats")).get("cmdstat_eval", {})
        durations = []
        for i in range(samples):
            started = time.perf_counter()
            await operation(i)
            durations.append((time.perf_counter() - started) * 1000)
        after = (await self.client.info("commandstats"))["cmdstat_eval"]
        self.assertEqual(after["calls"] - before.get("calls", 0), samples)
        print(json.dumps({
            "case": case, "samples": samples,
            "client_p50_ms": round(statistics.median(durations), 3),
            "client_p95_ms": round(sorted(durations)[(samples * 95 + 99) // 100 - 1], 3),
            "client_max_ms": round(max(durations), 3),
            "redis_eval_mean_ms": round((after["usec"] - before.get("usec", 0)) / samples / 1000, 3),
        }))

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
        self.assertEqual(self.store.max_references, 1024)
        self.assertEqual(self.store.max_owner_bytes, 32 * MAX_MAPPING_BYTES)
        mapping = {"<EMAIL_ADDRESS_1>": "alpha@example.test"}
        turns = MAX_OWNER_REFERENCES + 256
        for i in range(turns):
            model = "gpt-6-luna" if i % 2 else "gpt-6-sol"
            await self.store.save(self.owner, model, f"response-{i}", mapping)
        fields = await self.client.hgetall(KEY_PREFIX + self.owner)
        self.assertEqual(len(fields), 2)
        self.assertEqual(len(json.loads(fields["references"])), MAX_OWNER_REFERENCES)
        self.assertEqual(await self.store.load(self.owner, "gpt-6-luna", f"response-{turns - 1}"), mapping)
        self.assertEqual(await self.store.load(self.owner, "gpt-6-sol", "response-256"), mapping)
        with self.assertRaises(ResponsesStateError):
            await self.store.load(self.owner, "gpt-6-sol", "response-254")
        print(json.dumps({"clean_turns": turns, "references": MAX_OWNER_REFERENCES, "snapshots": 1,
                          "serialized_bytes": sum(len(value.encode()) for value in fields.values())}))

    async def test_full_reference_index_read_write_and_concurrent_branches(self):
        parent = {"<A_1>": "parent"}
        for i in range(MAX_OWNER_REFERENCES):
            await self.store.save(self.owner, "model", f"response-{i}", parent)
        await self.measure("full_index_read", lambda _: self.store.load(
            self.owner, "model", f"response-{MAX_OWNER_REFERENCES - 1}",
        ))
        await self.measure("full_index_save_and_evict", lambda i: self.store.save(
            self.owner, "model", f"new-{i}", parent,
        ))
        other = ResponsesStateStore(self.client, self.store.secret, self.store.ttl)
        await asyncio.gather(*[
            (other if i % 2 else self.store).save(self.owner, "model", f"branch-{i}", {
                **parent, "<A_2>": str(i),
            }) for i in range(64)
        ])
        self.assertEqual(len(json.loads(await self.client.hget(KEY_PREFIX + self.owner, "references"))), MAX_OWNER_REFERENCES)
        self.assertEqual(await other.load(self.owner, "model", f"response-{MAX_OWNER_REFERENCES - 1}"), parent)
        for i in range(64):
            self.assertEqual((await self.store.load(self.owner, "model", f"branch-{i}"))["<A_2>"], str(i))

    async def test_default_byte_budget_with_full_index_and_bulk_eviction(self):
        # One key approaches 32 MiB; no 400-owner memory allocation is needed.
        for i in range(MAX_OWNER_REFERENCES - 31):
            await self.store.save(self.owner, "model", f"shared-{i}", {"<A_1>": "shared"})
        large = "x" * (MAX_MAPPING_BYTES - 64)
        for i in range(31):
            await self.store.save(self.owner, "model", f"large-{i}", {"<A_1>": str(i) + large})
        self.assertEqual(len(json.loads(await self.client.hget(KEY_PREFIX + self.owner, "references"))), MAX_OWNER_REFERENCES)
        await self.measure("full_index_near_byte_budget_read", lambda _: self.store.load(
            self.owner, "model", "large-30",
        ), samples=30)
        await self.measure("bulk_eviction_at_byte_budget", lambda _: self.store.save(
            self.owner, "model", "latest", {"<A_1>": "latest" + large},
        ), samples=1)
        fields = await self.client.hkeys(KEY_PREFIX + self.owner)
        serialized_bytes = sum([await self.client.hstrlen(KEY_PREFIX + self.owner, field) for field in fields])
        self.assertLessEqual(serialized_bytes, MAX_OWNER_BYTES)
        self.assertGreater(serialized_bytes, MAX_OWNER_BYTES - 2 * MAX_MAPPING_BYTES)
        self.assertEqual(len(json.loads(await self.client.hget(KEY_PREFIX + self.owner, "references"))), 31)
        self.assertEqual(len(fields), 32)
        for response_id in ("shared-0", "large-0"):
            with self.assertRaises(ResponsesStateError) as failure:
                await self.store.load(self.owner, "model", response_id)
            self.assertEqual(failure.exception.status, 409)
        self.assertEqual((await self.store.load(self.owner, "model", "latest"))["<A_1>"], "latest" + large)
        self.assertEqual((await self.store.load(self.owner, "model", "large-30"))["<A_1>"], "30" + large)
        print(json.dumps({
            "case": "default_byte_budget", "owner_count": 1,
            "references": 31, "serialized_bytes": serialized_bytes,
            "redis_memory_bytes": await self.client.memory_usage(KEY_PREFIX + self.owner),
            "redis_peak_memory_bytes": (await self.client.info("memory"))["used_memory_peak"],
        }))

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
