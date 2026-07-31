"""Opt-in ChatGPT Codex OAuth bridge for the LiteLLM proxy."""

from __future__ import annotations

from typing import Any, Optional, Union

from litellm.caching.caching import DualCache
from litellm.integrations.custom_guardrail import CustomGuardrail
from litellm.proxy._types import ProxyException, UserAPIKeyAuth


_PII_METADATA_KEYS = ("pii_request_id", "pii_streaming_restoration_done")
_HEADER_ALLOWLIST = frozenset(
    {
        "authorization",
        "chatgpt-account-id",
        "originator",
        "session-id",
        "thread-id",
        "user-agent",
        "x-openai-internal-codex-responses-lite",
    }
)


def _extract_headers(data: dict[str, Any]) -> dict[str, str]:
    """Merge LiteLLM header copies, preferring the unredacted secret bucket."""
    buckets: list[Any] = [data.get("headers")]
    for key in ("metadata", "litellm_metadata", "proxy_server_request"):
        bucket = data.get(key)
        if isinstance(bucket, dict):
            buckets.append(bucket.get("headers"))

    litellm_params = data.get("litellm_params")
    if isinstance(litellm_params, dict):
        metadata = litellm_params.get("metadata")
        if isinstance(metadata, dict):
            buckets.append(metadata.get("headers"))
        proxy_request = litellm_params.get("proxy_server_request")
        if isinstance(proxy_request, dict):
            buckets.append(proxy_request.get("headers"))

    secret_fields = data.get("secret_fields")
    if isinstance(secret_fields, dict):
        buckets.append(secret_fields.get("raw_headers"))

    merged: dict[str, tuple[str, str]] = {}
    for bucket in buckets:
        if not isinstance(bucket, dict):
            continue
        for key, value in bucket.items():
            name = str(key)
            merged[name.lower()] = (name, str(value))
    return dict(merged.values())


def _provider_headers(inbound: dict[str, str]) -> tuple[str, dict[str, str]]:
    selected: dict[str, str] = {}
    authorization: Optional[str] = None
    account_id: Optional[str] = None

    for name, value in inbound.items():
        lower = name.lower()
        if lower in _HEADER_ALLOWLIST or lower.startswith("x-codex-"):
            if "\r" in value or "\n" in value:
                raise ValueError("invalid provider header")
            selected[name] = value
        if lower == "authorization":
            authorization = value
        elif lower == "chatgpt-account-id":
            account_id = value

    if authorization is None or not authorization.lower().startswith("bearer "):
        raise ValueError("missing bearer authorization")
    token = authorization[7:].strip()
    if not token or "\r" in token or "\n" in token:
        raise ValueError("invalid bearer authorization")
    if account_id is None or not account_id.strip():
        raise ValueError("missing ChatGPT account id")

    return token, {
        name: value
        for name, value in selected.items()
        if name.lower() != "authorization"
    }


def _header_buckets(data: dict[str, Any]) -> list[dict[Any, Any]]:
    """Return every mutable header bucket LiteLLM may retain or forward."""
    buckets: list[dict[Any, Any]] = []

    def add(value: Any) -> None:
        if isinstance(value, dict):
            buckets.append(value)

    add(data.get("headers"))
    for key in ("metadata", "litellm_metadata", "proxy_server_request"):
        container = data.get(key)
        if isinstance(container, dict):
            add(container.get("headers"))

    litellm_params = data.get("litellm_params")
    if isinstance(litellm_params, dict):
        for key in ("metadata", "proxy_server_request"):
            container = litellm_params.get(key)
            if isinstance(container, dict):
                add(container.get("headers"))

    secret_fields = data.get("secret_fields")
    if isinstance(secret_fields, dict):
        add(secret_fields.get("raw_headers"))
    return buckets


def _scrub_inbound_headers(data: dict[str, Any]) -> None:
    """Drop proxy and wire headers after OAuth has been extracted."""
    for headers in _header_buckets(data):
        for name in list(headers):
            lower = str(name).lower()
            if lower == "authorization":
                del headers[name]
                continue
            if lower not in _HEADER_ALLOWLIST and not lower.startswith("x-codex-"):
                del headers[name]


def _move_internal_metadata(data: dict[str, Any]) -> None:
    """Keep guardrail correlation internal; ChatGPT rejects Responses metadata."""
    metadata = data.pop("metadata", None)
    if not isinstance(metadata, dict):
        return
    internal = data.get("litellm_metadata")
    if not isinstance(internal, dict):
        internal = {}
        data["litellm_metadata"] = internal
    for key in _PII_METADATA_KEYS:
        if key in metadata:
            internal[key] = metadata[key]


def _raise_provider_auth_error() -> None:
    error = {
        "message": "ChatGPT Codex OAuth authentication is missing or invalid.",
        "type": "provider_auth_error",
        "code": "chatgpt_codex_auth_invalid",
    }
    exc = ProxyException(
        message=error["message"],
        type=error["type"],
        param="authorization",
        code=401,
        provider_specific_fields={"error": error},
    )
    exc.status_code = 401
    raise exc


class ChatGPTCodexAuthBridge(CustomGuardrail):
    """Forward Codex OAuth while LiteLLM authenticates via x-litellm-api-key."""

    async def async_pre_call_hook(
        self,
        user_api_key_dict: UserAPIKeyAuth,
        cache: DualCache,
        data: dict,
        call_type: Optional[str] = None,
    ) -> Optional[Union[Exception, str, dict]]:
        try:
            token, provider_headers = _provider_headers(_extract_headers(data))
        except ValueError:
            _raise_provider_auth_error()

        # OpenAI's provider builds Authorization from api_key after merging
        # extra_headers, so the OAuth token must be a per-request api_key.
        data["api_key"] = token
        data["extra_headers"] = provider_headers
        _scrub_inbound_headers(data)
        _move_internal_metadata(data)
        return data
