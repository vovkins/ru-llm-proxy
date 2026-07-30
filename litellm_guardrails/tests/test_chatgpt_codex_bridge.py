"""Tests for the opt-in ChatGPT Codex OAuth bridge."""

import pytest

from litellm.proxy._types import ProxyException

from litellm_guardrails.chatgpt_codex_bridge import ChatGPTCodexAuthBridge


async def _run(data: dict) -> dict:
    bridge = ChatGPTCodexAuthBridge()
    result = await bridge.async_pre_call_hook(None, None, data, "responses")
    assert isinstance(result, dict)
    return result


@pytest.mark.asyncio
async def test_forwards_chatgpt_oauth_and_scrubs_proxy_credentials():
    data = {
        "model": "gpt-5.6-sol",
        "input": "hello",
        "headers": {
            "Authorization": "Bearer redacted-copy",
            "X-LiteLLM-API-Key": "proxy-key",
            "Host": "127.0.0.1:4100",
        },
        "secret_fields": {
            "raw_headers": {
                "Authorization": "Bearer oauth-value",
                "ChatGPT-Account-Id": "account-id",
                "Originator": "codex_cli_rs",
                "Session-Id": "session-id",
                "Thread-Id": "thread-id",
                "X-Codex-Beta-Features": "feature",
                "X-LiteLLM-API-Key": "proxy-key",
                "X-Unrelated": "do-not-forward",
            }
        },
    }

    result = await _run(data)
    upstream = {key.lower(): value for key, value in result["extra_headers"].items()}

    assert result["api_key"] == "oauth-value"
    assert "authorization" not in upstream
    assert upstream["chatgpt-account-id"] == "account-id"
    assert upstream["originator"] == "codex_cli_rs"
    assert upstream["session-id"] == "session-id"
    assert upstream["thread-id"] == "thread-id"
    assert upstream["x-codex-beta-features"] == "feature"
    assert "x-litellm-api-key" not in upstream
    assert "x-unrelated" not in upstream

    for bucket in (result["headers"], result["secret_fields"]["raw_headers"]):
        lowered = {str(key).lower() for key in bucket}
        assert "authorization" not in lowered
        assert "x-litellm-api-key" not in lowered
        assert "host" not in lowered


@pytest.mark.asyncio
async def test_moves_only_internal_pii_metadata_out_of_provider_payload():
    data = {
        "input": "hello",
        "metadata": {
            "pii_request_id": "mapping-id",
            "pii_streaming_restoration_done": True,
            "client_value": "must-not-reach-provider",
        },
        "secret_fields": {
            "raw_headers": {
                "Authorization": "Bearer oauth-value",
                "ChatGPT-Account-Id": "account-id",
            }
        },
    }

    result = await _run(data)

    assert "metadata" not in result
    assert result["litellm_metadata"]["pii_request_id"] == "mapping-id"
    assert result["litellm_metadata"]["pii_streaming_restoration_done"] is True
    assert "client_value" not in result["litellm_metadata"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "headers",
    [
        {"ChatGPT-Account-Id": "account-id"},
        {"Authorization": "Bearer oauth-value"},
        {
            "Authorization": "Basic oauth-value",
            "ChatGPT-Account-Id": "account-id",
        },
    ],
)
async def test_rejects_missing_or_invalid_chatgpt_auth(headers):
    data = {"input": "hello", "secret_fields": {"raw_headers": headers}}

    with pytest.raises(ProxyException) as error:
        await _run(data)

    assert error.value.status_code == 401
    assert error.value.provider_specific_fields["error"]["code"] == (
        "chatgpt_codex_auth_invalid"
    )
