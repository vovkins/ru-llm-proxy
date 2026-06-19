# Claude Code

Claude Code connects to this proxy through the Anthropic Messages API.

Supported scope:

- Claude Code CLI.
- Anthropic-compatible clients that can set `ANTHROPIC_BASE_URL`.

Not covered here:

- Placing shared Claude.ai or Claude Code login files on the proxy as upstream credentials.
- Claude Desktop or cloud features that do not honor the same gateway settings.

## Credentials

Use a LiteLLM virtual key as the client token:

```bash
export RU_LLM_PROXY_TOKEN="sk-..."
```

The real `ANTHROPIC_API_KEY` stays only on the proxy host in server-funded mode. Do not put `ANTHROPIC_API_KEY` or `LITELLM_MASTER_KEY` into local Claude Code config.

Create a Claude-capable client key from the proxy host:

```bash
scripts/create_virtual_key.sh --alias claude-code-local --models anthropic,standard --duration 30d
```

## Server-Funded Token Setup

Use this mode when the proxy should pay with its server-side `ANTHROPIC_API_KEY`. Point Claude Code at the proxy:

```bash
export ANTHROPIC_BASE_URL="http://localhost:4000"
export ANTHROPIC_AUTH_TOKEN="$RU_LLM_PROXY_TOKEN"
export ANTHROPIC_MODEL="claude-sonnet-4.6"
```

Use the premium model only when the key allows it:

```bash
export ANTHROPIC_MODEL="claude-opus-4.8"
```

Claude Code sends the virtual key to the proxy. The proxy then uses its server-side `ANTHROPIC_API_KEY` to call Anthropic.

## Claude Subscription Passthrough

Use this mode when Claude Code should use the local user's Claude subscription while still routing through the proxy for guardrails, tracking, and proxy access control.

Do not set `ANTHROPIC_AUTH_TOKEN` to the proxy key in this mode. Instead, send the proxy key with `ANTHROPIC_CUSTOM_HEADERS` and let Claude Code manage Claude account auth locally:

```bash
export ANTHROPIC_BASE_URL="http://localhost:4000"
export ANTHROPIC_MODEL="claude-sonnet-4.6"
export ANTHROPIC_CUSTOM_HEADERS="x-litellm-api-key: Bearer $RU_LLM_PROXY_TOKEN"

claude
```

If Claude Code asks for login, choose the Claude account subscription flow. The proxy authenticates the request with `x-litellm-api-key`; Claude Code sends its Claude OAuth provider auth separately and LiteLLM forwards it upstream.

Do not copy a shared Claude credentials file onto the proxy for all users. This mode must be live-validated against the pinned LiteLLM image before production rollout.

## Dynamic Token Setup

Claude Code supports `apiKeyHelper` for CLI workflows. Use it when a local command should fetch or rotate the proxy token:

```json
{
  "apiKeyHelper": "/absolute/path/to/print-ru-llm-proxy-token.sh"
}
```

The helper must print the LiteLLM virtual key, not the upstream Anthropic API key.

## Required Proxy Endpoints

Claude Code requires the Anthropic-compatible gateway surface:

```text
POST /v1/messages
POST /v1/messages/count_tokens
GET /v1/models
```

`make client-auth-smoke` checks `/v1/messages` when `ANTHROPIC_API_KEY` is configured on the proxy.

## References

- Claude Code LLM gateway: https://docs.anthropic.com/en/docs/claude-code/llm-gateway
- Claude Code environment variables: https://docs.anthropic.com/en/docs/claude-code/settings#environment-variables
- LiteLLM Claude Code Max subscription: https://docs.litellm.ai/docs/tutorials/claude_code_max_subscription
- LiteLLM forward client headers: https://docs.litellm.ai/docs/proxy/forward_client_headers
- LiteLLM Anthropic Messages API: https://docs.litellm.ai/docs/anthropic_unified/
