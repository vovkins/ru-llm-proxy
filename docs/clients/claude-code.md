# Claude Code

Claude Code connects to this proxy through the Anthropic Messages API.

Supported scope:

- Claude Code CLI.
- Anthropic-compatible clients that can set `ANTHROPIC_BASE_URL`.

Not covered here:

- Using Claude.ai or Claude Code login files as upstream proxy credentials.
- Claude Desktop or cloud features that do not honor the same gateway settings.

## Credentials

Use a LiteLLM virtual key as the client token:

```bash
export RU_LLM_PROXY_TOKEN="sk-..."
```

The real `ANTHROPIC_API_KEY` stays only on the proxy host. Do not put `ANTHROPIC_API_KEY` or `LITELLM_MASTER_KEY` into local Claude Code config.

Create a Claude-capable client key from the proxy host:

```bash
scripts/create_virtual_key.sh --alias claude-code-local --models anthropic,standard --duration 30d
```

## Static Token Setup

Point Claude Code at the proxy:

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
- LiteLLM Anthropic Messages API: https://docs.litellm.ai/docs/anthropic_unified/
