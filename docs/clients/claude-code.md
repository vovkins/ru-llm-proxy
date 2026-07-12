# Claude Code

This guide tracks Claude Code as a gateway target for ru-llm-proxy. The default repo config validates the basic Anthropic Messages auth path only; it is not a fully validated Claude Code gateway yet.

Validation scope today:

- Basic Anthropic Messages clients that can set `ANTHROPIC_BASE_URL` and send a LiteLLM virtual key.
- Claude Code CLI setup as a target path, pending dedicated gateway validation.

Not fully validated yet:

- Claude Code's full `POST /v1/messages?beta=true` gateway contract.
- Streaming SSE behavior through the proxy.
- Forwarding `anthropic-version` and `anthropic-beta` in the default config.
- Optional token counting and model discovery endpoints.

Not covered here:

- Placing shared Claude.ai or Claude Code login files on the proxy as upstream credentials.
- Claude Desktop or cloud features that do not honor the same gateway settings.

## Credentials

Use a LiteLLM virtual key as the client token:

```bash
export RU_LLM_PROXY_TOKEN="sk-..."
```

The real `ANTHROPIC_API_KEY` stays only on the proxy host in server-funded mode. Do not put `ANTHROPIC_API_KEY` or `LITELLM_MASTER_KEY` into local Claude Code config.

All environment variables used in this guide are documented in [../configuration.md](../configuration.md).

Create routine user/client keys in LiteLLM Admin UI. The CLI helper is only an optional DevOps/CI/bootstrap path from the proxy host:

```bash
scripts/create_virtual_key.sh --alias claude-code-local --models anthropic,standard --duration 30d
```

## Server-Funded Token Setup

Use this mode when the proxy should pay with its server-side `ANTHROPIC_API_KEY`. Point Claude Code at the proxy for the basic Anthropic Messages path:

```bash
export ANTHROPIC_BASE_URL="http://localhost:4000"
export ANTHROPIC_AUTH_TOKEN="$RU_LLM_PROXY_TOKEN"
export ANTHROPIC_MODEL="anthropic-example-standard"
```

Use the premium model only when the key allows it:

```bash
export ANTHROPIC_MODEL="anthropic-example-premium"
```

Anthropic aliases are optional provider examples now. Add them from `examples/litellm-config.optional-providers.yaml` only after validating raw model IDs against the target LiteLLM image and provider subscription.

Claude Code sends the virtual key to the proxy. The proxy then uses its server-side `ANTHROPIC_API_KEY` to call Anthropic. Treat this as a setup target until the dedicated Claude Code gateway smoke covers `?beta=true`, SSE streaming, and `anthropic-*` header forwarding.

## Anthropic API-Key BYOK

Use this mode when a client should bring an Anthropic API key while still authenticating to the proxy with a LiteLLM virtual key. This is an opt-in deployment mode and is not enabled by the default repo config.

LiteLLM BYOK forwarding is designed for provider-specific headers such as `x-api-key` and `api-key`. The proxy token remains separate:

```bash
export ANTHROPIC_BASE_URL="http://localhost:4000"

curl "$ANTHROPIC_BASE_URL/v1/messages" \
  -H "Content-Type: application/json" \
  -H "x-litellm-api-key: Bearer $RU_LLM_PROXY_TOKEN" \
  -H "x-api-key: $ANTHROPIC_BYOK_API_KEY" \
  -d '{"model":"anthropic-example-standard","max_tokens":32,"messages":[{"role":"user","content":"Reply with ok."}]}'
```

## Claude Subscription Passthrough

Use this mode when Claude Code should use the local user's Claude subscription while still routing through the proxy for guardrails, tracking, and proxy access control.

This is an opt-in validation target, not a production-ready default. Claude subscription auth normally relies on provider `Authorization`; the standard LiteLLM path is not assumed to forward that header upstream. The default repo config does not enable header forwarding.

Do not set `ANTHROPIC_AUTH_TOKEN` to the proxy key in this mode. Instead, send the proxy key with `ANTHROPIC_CUSTOM_HEADERS` and let Claude Code manage Claude account auth locally:

```bash
export ANTHROPIC_BASE_URL="http://localhost:4000"
export ANTHROPIC_MODEL="anthropic-example-standard"
export ANTHROPIC_CUSTOM_HEADERS="x-litellm-api-key: Bearer $RU_LLM_PROXY_TOKEN"

claude
```

If Claude Code asks for login, choose the Claude account subscription flow. The proxy authenticates the request with `x-litellm-api-key`; Claude Code sends its Claude OAuth provider auth separately. If live validation shows the normal LiteLLM `/v1/messages` route strips the required provider `Authorization` header, this mode needs a pass-through route, sidecar, or custom adapter before it can be marked production-ready.

Do not copy a shared Claude credentials file onto the proxy for all users. This mode must be live-validated against the pinned LiteLLM image before production rollout.

## Dynamic Token Setup

Claude Code supports `apiKeyHelper` for CLI workflows. Use it when a local command should fetch or rotate the proxy token:

```json
{
  "apiKeyHelper": "/absolute/path/to/print-ru-llm-proxy-token.sh"
}
```

The helper must print the LiteLLM virtual key, not the upstream Anthropic API key.

## Anthropic Messages Gateway Surface

For a fully validated Claude Code gateway via `ANTHROPIC_BASE_URL`, the core inference path is:

```text
POST /v1/messages?beta=true
```

The gateway must preserve Anthropic Messages semantics, relay streaming SSE responses, and forward `anthropic-version` and `anthropic-beta` unchanged. LiteLLM supports the Anthropic-compatible `/v1/messages` endpoint, but this repository has not yet added a dedicated Claude Code gateway smoke for the `?beta=true` streaming path or default-config header forwarding.

Optional Claude Code gateway endpoints:

```text
POST /v1/messages/count_tokens
GET /v1/models?limit=1000
```

`POST /v1/messages/count_tokens` is optional; Claude Code can fall back to local context estimates when it is absent. `GET /v1/models?limit=1000` is optional model discovery when gateway model discovery is enabled.

`make client-auth-smoke` is a basic Anthropic Messages auth smoke. It sends a non-streaming `POST /v1/messages` with a LiteLLM virtual key only when `ANTHROPIC_API_KEY` is configured and `MESSAGES_MODEL` is set to a live-validated proxy alias:

```bash
MESSAGES_MODEL=<validated-messages-alias> make client-auth-smoke
```

It does not validate Claude Code's `?beta=true` query, `anthropic-*` header forwarding, SSE streaming, token counting, or model discovery. Add a separate Claude Code gateway smoke before marking this surface fully validated.

## References

- Claude Code LLM gateway: https://docs.anthropic.com/en/docs/claude-code/llm-gateway
- Claude Code environment variables: https://docs.anthropic.com/en/docs/claude-code/settings#environment-variables
- LiteLLM Claude Code Max subscription: https://docs.litellm.ai/docs/tutorials/claude_code_max_subscription
- LiteLLM forward client headers: https://docs.litellm.ai/docs/proxy/forward_client_headers
- LiteLLM Anthropic Messages API: https://docs.litellm.ai/docs/anthropic_unified/
