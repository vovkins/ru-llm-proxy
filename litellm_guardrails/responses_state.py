"""Bounded immutable Responses mappings, isolated by trusted virtual-key identity."""

from __future__ import annotations

import hashlib
import hmac
import json
import re
from typing import Any


STATE_METADATA_KEY = "pii_responses_state"
KEY_PREFIX = "pii_responses_state:v1:"
MAX_MAPPING_BYTES = 1024 * 1024
MAX_MAPPING_PAIRS = 10_000
MAX_OWNER_REFERENCES = 128
MAX_OWNER_BYTES = 16 * 1024 * 1024


class ResponsesStateError(Exception):
    def __init__(self, code: str, status: int, message: str):
        super().__init__(message)
        self.code = code
        self.status = status


def missing_state() -> ResponsesStateError:
    return ResponsesStateError(
        "pii_history_unavailable", 409,
        "Sensitive history is unavailable. Start a new request with the full history "
        "and without previous_response_id.",
    )


def unavailable_state() -> ResponsesStateError:
    return ResponsesStateError(
        "pii_history_storage_unavailable", 503,
        "Sensitive history storage is unavailable. Retry later.",
    )


# One Redis hash per owner keeps transactions atomic, including on Redis Cluster.
# The small reference index is ordered by publication; mapping bodies are separate
# hash fields so loading one response does not read the owner's entire PII budget.
_STATE_SCRIPT = """
local key = KEYS[1]
local now = tonumber(redis.call('TIME')[1])
local refs = cjson.decode(redis.call('HGET', key, 'references') or '[]')
local old_snapshots = {}
local active = {}
for _, ref in ipairs(refs) do
    old_snapshots[ref.snapshot] = true
    if ref.expires > now then table.insert(active, ref) end
end
local function collect()
    local used = {}
    local bytes = string.len(cjson.encode(active))
    for _, ref in ipairs(active) do
        if not used[ref.snapshot] then
            used[ref.snapshot] = true
            bytes = bytes + redis.call('HSTRLEN', key, 's:' .. ref.snapshot)
        end
    end
    for snapshot, _ in pairs(old_snapshots) do
        if not used[snapshot] then redis.call('HDEL', key, 's:' .. snapshot) end
    end
    return bytes
end
if ARGV[1] == 'save' then
    for _, ref in ipairs(active) do
        if ref.id == ARGV[2] then
            if ref.snapshot ~= ARGV[3] then return {'conflict'} end
            return {'saved'}
        end
    end
    redis.call('HSET', key, 's:' .. ARGV[3], ARGV[4])
    table.insert(active, {id=ARGV[2], snapshot=ARGV[3], expires=now+tonumber(ARGV[5])})
    local bytes = collect()
    while #active > tonumber(ARGV[6]) or bytes > tonumber(ARGV[7]) do
        local removed = table.remove(active, 1)
        old_snapshots[removed.snapshot] = true
        bytes = collect()
    end
    redis.call('HSET', key, 'references', cjson.encode(active))
    redis.call('EXPIRE', key, tonumber(ARGV[5]))
    return {'saved'}
end
collect()
if #active == 0 then
    redis.call('DEL', key)
else
    redis.call('HSET', key, 'references', cjson.encode(active))
end
for _, ref in ipairs(active) do
    if ref.id == ARGV[2] then
        local body = redis.call('HGET', key, 's:' .. ref.snapshot)
        if body then return {'found', body} end
    end
end
return {'missing'}
"""


class ResponsesStateStore:
    def __init__(
        self, redis: Any, secret: bytes, ttl: int,
        *, max_references: int = MAX_OWNER_REFERENCES,
        max_owner_bytes: int = MAX_OWNER_BYTES,
    ):
        if not secret or ttl <= 0 or max_references < 1 or max_owner_bytes < MAX_MAPPING_BYTES + 1024:
            raise unavailable_state()
        self.redis = redis
        self.secret = secret
        self.ttl = ttl
        self.max_references = max_references
        self.max_owner_bytes = max_owner_bytes

    def _digest(self, domain: str, *parts: str) -> str:
        body = json.dumps([domain, *parts], ensure_ascii=False, separators=(",", ":"))
        return hmac.new(self.secret, body.encode(), hashlib.sha256).hexdigest()

    def owner(self, trusted_identity: str) -> str:
        return self._digest("responses-owner:v1", trusted_identity)

    def _reference(self, model: str, response_id: str) -> str:
        if not isinstance(response_id, str) or not response_id or len(response_id) > 4096:
            raise missing_state()
        return self._digest("responses-reference:v1", model, response_id)

    @staticmethod
    def serialize(mapping: dict[str, str]) -> str:
        if (
            not isinstance(mapping, dict)
            or any(not isinstance(k, str) or not k or not isinstance(v, str) for k, v in mapping.items())
        ):
            raise unavailable_state()
        if len(mapping) > MAX_MAPPING_PAIRS:
            raise ResponsesStateError("pii_history_too_large", 413, "Sensitive history mapping exceeds its limit.")
        body = json.dumps(mapping, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        try:
            size = len(body.encode())
        except UnicodeError:
            raise unavailable_state() from None
        if size > MAX_MAPPING_BYTES:
            raise ResponsesStateError("pii_history_too_large", 413, "Sensitive history mapping exceeds its limit.")
        return body

    async def _execute(self, owner: str, *args: Any) -> list:
        try:
            return await self.redis.eval(_STATE_SCRIPT, 1, KEY_PREFIX + owner, *args)
        except Exception:
            raise unavailable_state() from None

    async def load(self, owner: str, model: str, response_id: str) -> dict[str, str]:
        result = await self._execute(owner, "load", self._reference(model, response_id))
        if not result or result[0] != "found":
            raise missing_state()
        try:
            mapping = json.loads(result[1])
            self.serialize(mapping)
            return mapping
        except (ValueError, TypeError, ResponsesStateError):
            raise unavailable_state() from None

    async def save(self, owner: str, model: str, response_id: str, mapping: dict[str, str]) -> None:
        body = self.serialize(mapping)
        result = await self._execute(
            owner, "save", self._reference(model, response_id),
            self._digest("responses-snapshot:v1", body), body, self.ttl,
            self.max_references, self.max_owner_bytes,
        )
        if result == ["conflict"]:
            raise missing_state()
        if result != ["saved"]:
            raise unavailable_state()


def placeholder_counters(mapping: dict[str, str]) -> dict[str, int]:
    """Use the greatest suffix, not the number of entries (gaps are legitimate)."""
    counters: dict[str, int] = {}
    for placeholder in mapping:
        match = re.fullmatch(r"<([A-Z][A-Z0-9_]*)_([0-9]+)>", placeholder)
        if match:
            entity, index = match.groups()
            counters[entity] = max(counters.get(entity, 0), int(index))
    return counters
