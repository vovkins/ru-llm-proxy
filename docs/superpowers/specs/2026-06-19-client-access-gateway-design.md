# Client Access Gateway Design

## Goal

Turn the project from a single Z.AI-oriented proxy into a practical corporate LLM gateway for coding tools. The gateway must support both server-funded provider API keys and client-side subscription/BYOK passthrough for tools such as Codex CLI/App, Claude Code, OpenCode, and Kilo Code.

## Scope

This PR covers the first production-oriented slice:

- Client access to the proxy through LiteLLM virtual keys, plus documented JWT/OIDC deployment guidance.
- Client-side subscription/BYOK passthrough for Codex and Claude Code, where the client keeps the ChatGPT/Claude auth locally and sends it through the proxy to the provider.
- Multi-provider LiteLLM configuration for Z.AI, OpenAI, and Anthropic.
- Client setup docs for Codex CLI/App local tasks, Claude Code, OpenCode CLI/Desktop, and Kilo Code VS Code/CLI.
- Helper scripts and Make targets for creating client virtual keys and smoke-testing supported protocols.
- Existing user examples converted away from `LITELLM_MASTER_KEY`.

This PR does not place shared Codex or Claude Code login/auth files on the proxy as server-side upstream credentials. Server-managed subscription auth files remain out of scope because they would be shared account credentials and are not normal LiteLLM provider API keys.

## Auth Boundaries

There are two separate credential layers.

### Proxy Ingress Credentials

Ingress credentials answer: "Who may use this proxy?"

The default proxy issues LiteLLM virtual keys to users, teams, CI jobs, and local tools. Those keys are sent by clients as bearer tokens or through a client-specific equivalent:

- OpenAI-compatible clients use `Authorization: Bearer <virtual-key>`.
- Codex custom providers use a configured `env_key` such as `RU_LLM_PROXY_TOKEN`.
- Claude Code uses `ANTHROPIC_AUTH_TOKEN=<virtual-key>` against the proxy.
- OpenCode and Kilo Code use their OpenAI-compatible provider API key field, usually sourced from `RU_LLM_PROXY_TOKEN`.

Subscription passthrough clients must authenticate to LiteLLM with the provider-independent header:

```text
x-litellm-api-key: Bearer <LiteLLM virtual key>
```

That leaves `Authorization` and provider-specific auth headers available for upstream credentials:

- Codex uses OpenAI authentication with `requires_openai_auth = true`, so its ChatGPT/API auth is sent as OpenAI auth to the proxy endpoint.
- Claude Code uses Claude account OAuth or Anthropic API-key auth and sends it to the proxy while adding the LiteLLM virtual key through `ANTHROPIC_CUSTOM_HEADERS`.
- OpenAI-compatible BYOK clients may use provider auth headers where supported by LiteLLM header forwarding.

This PR must not mark subscription passthrough complete until live validation proves the current LiteLLM image forwards the provider auth needed by the target client. If standard LiteLLM routing strips a required provider `Authorization` header, the follow-up implementation must use a LiteLLM pass-through route, a sidecar, or a custom adapter rather than pretending the standard model route is enough.

`LITELLM_MASTER_KEY` remains admin-only. It is used for LiteLLM admin APIs such as `/key/generate` and must not appear in normal client examples.

JWT/OIDC is a separate deployment path for the same ingress boundary. It is not enabled by default because it requires an identity provider, JWKS URL, audience policy, and LiteLLM Enterprise features. The PR documents that path but keeps the default runnable config on virtual keys.

### Upstream Provider Credentials

Upstream credentials answer: "How does the proxy call the real provider?"

There are two supported upstream modes.

#### Server-Funded Provider API Keys

The proxy stores provider credentials in `.env` and injects them into LiteLLM:

- `ZAI_API_KEY` for Z.AI / GLM.
- `OPENAI_API_KEY` for OpenAI Platform models and the Responses API.
- `ANTHROPIC_API_KEY` for Anthropic Console API models and Anthropic Messages API.

These credentials never leave the server. Local clients do not receive provider keys.

#### Client-Side Subscription Or BYOK Passthrough

For Codex and Claude Code subscription workflows, the subscription credential stays on the developer workstation. The proxy forwards the provider auth header to the upstream provider while still enforcing LiteLLM virtual key access, usage tracking, budgets, and guardrails.

This mode requires LiteLLM header forwarding:

```yaml
general_settings:
  forward_client_headers_to_llm_api: true
  forward_llm_provider_auth_headers: true
```

The proxy must not log forwarded provider credentials. Documentation and tests should treat provider auth headers as secrets.

## Client Protocols

The gateway must support three client-facing API families.

### OpenAI Chat Completions

Endpoint:

```text
POST /v1/chat/completions
```

Primary clients:

- OpenCode CLI/Desktop.
- Kilo Code VS Code extension and CLI.
- Generic OpenAI SDK clients.

Authentication:

```text
Authorization: Bearer <LiteLLM virtual key>
```

### OpenAI Responses

Endpoint:

```text
POST /v1/responses
```

Primary clients:

- Codex CLI.
- Codex App local tasks when configured with a custom model provider.

Authentication is still a LiteLLM virtual key, but Codex reads it through its configured provider `env_key`.

Codex provider configuration belongs in user-level Codex configuration, not in repository-local project config. The docs should show `~/.codex/config.toml` and, for desktop/local app flows, `~/.codex/.env`.

For ChatGPT subscription passthrough, Codex should use OpenAI authentication against the proxy:

```toml
model_provider = "ru_llm_proxy_chatgpt"
model = "openai-gpt-5.4-mini"

[model_providers.ru_llm_proxy_chatgpt]
name = "ru-llm-proxy via ChatGPT auth"
base_url = "http://localhost:4000/v1"
wire_api = "responses"
requires_openai_auth = true
env_http_headers = { "x-litellm-api-key" = "RU_LLM_PROXY_TOKEN" }
```

In this mode Codex keeps the ChatGPT auth locally. The proxy receives a LiteLLM virtual key in `x-litellm-api-key` and must forward OpenAI auth to the upstream OpenAI-compatible API. Because LiteLLM's general header-forwarding rules strip ordinary `Authorization` headers by default, this path requires live validation. If it fails on the normal `/v1/responses` route, implement a pass-through route or sidecar for Codex subscription traffic.

### Anthropic Messages

Endpoint:

```text
POST /v1/messages
```

Primary clients:

- Claude Code.
- Claude Code Router or similar Anthropic-compatible clients, when pointed at the proxy.

Authentication:

```text
ANTHROPIC_AUTH_TOKEN=<LiteLLM virtual key>
ANTHROPIC_BASE_URL=https://proxy.example.com
```

Level 2 dynamic auth for Claude Code may use `apiKeyHelper`, but only where the client supports it. The static virtual key path remains the baseline.

For Claude subscription passthrough, Claude Code should send the LiteLLM key through custom headers and keep Claude account auth in Claude Code:

```bash
export ANTHROPIC_BASE_URL="https://proxy.example.com"
export ANTHROPIC_MODEL="claude-sonnet-4.6"
export ANTHROPIC_CUSTOM_HEADERS="x-litellm-api-key: Bearer $RU_LLM_PROXY_TOKEN"
claude
```

The user signs into Claude Code with a Claude account subscription on the client machine. Claude Code sends its OAuth provider auth in `Authorization`, and LiteLLM should forward it upstream while authenticating the proxy request with `x-litellm-api-key`. This follows LiteLLM's Claude Code Max subscription tutorial and must be verified with a live Claude Code request against the pinned LiteLLM image.

## Model Naming

Expose stable proxy-facing aliases rather than raw provider names. Initial aliases should be clear enough for users and restrictive enough for LiteLLM virtual key policies.

Recommended initial names:

- `zai-glm-5.1` -> Z.AI GLM model through the existing OpenAI-compatible Z.AI endpoint.
- `openai-gpt-5.4-mini` -> OpenAI fast/general model.
- `openai-gpt-5.5` -> OpenAI higher-capability model.
- `claude-opus-4.8` -> Anthropic Opus class model.
- `claude-sonnet-4.6` -> Anthropic Sonnet class model.
- `claude-haiku-4.5` -> Anthropic Haiku class model.

Keep the existing `glm-5.1` name as a compatibility alias or documented legacy name. New docs should prefer provider-prefixed aliases.

Virtual keys can be restricted to concrete aliases or access groups such as `zai`, `openai`, `anthropic`, `standard`, and `premium`.

## Key Management

Add an admin helper script for virtual key creation. It should:

- Read `LITELLM_MASTER_KEY` from `.env` unless explicitly provided.
- Call LiteLLM `/key/generate`.
- Accept a key alias, model list, optional budget, optional duration, optional RPM/TPM limits, and optional metadata.
- Print the generated client key once.
- Never print upstream provider keys.
- Make it obvious that generated keys are client-facing tokens, not provider API keys.

The Makefile should wrap this script with a `virtual-key-create` target.

## Smoke Tests

Add live smoke coverage for the supported client protocols. These smokes require running services and should not require real user provider keys beyond the configured proxy environment.

Required live checks:

- No token is rejected.
- An invalid token is rejected.
- A generated virtual key can call an allowed model.
- A restricted virtual key cannot call a disallowed model.
- `/v1/chat/completions` works with a virtual key.
- `/v1/responses` works with a virtual key.
- `/v1/messages` works with a virtual key.
- `x-litellm-api-key` is accepted as proxy auth when `Authorization` is reserved for upstream provider auth.
- Subscription/BYOK passthrough documentation examples use `x-litellm-api-key` for proxy auth and never ask users to put `LITELLM_MASTER_KEY` in client config.
- Claude Code subscription passthrough is live-validated against the current LiteLLM image before it is documented as working.
- Codex ChatGPT subscription passthrough is live-validated against the current LiteLLM image; if normal LiteLLM routing strips the required auth, the implementation must switch to a pass-through/sidecar design.

Smoke tests may be skipped with clear messages when a required upstream key is not configured. They should fail for auth regressions when services are running.

## Documentation

Create client-facing docs for:

- Codex CLI and Codex App local tasks.
- Claude Code.
- OpenCode CLI/Desktop.
- Kilo Code VS Code extension and CLI.

Docs must show:

- Which proxy endpoint each client uses.
- Which local config file is edited.
- Which environment variable holds the LiteLLM virtual key.
- Which model aliases are examples, not required defaults.
- That `LITELLM_MASTER_KEY` is not for users.
- That upstream provider keys stay only on the proxy.
- How subscription/BYOK passthrough differs from server-funded provider API keys.
- How Codex ChatGPT auth and Claude account auth remain client-side while provider auth is forwarded through the proxy.
- How JWT/OIDC fits as an enterprise proxy-auth option without mixing it with upstream provider credentials.

Update existing README and examples so normal client requests use `RU_LLM_PROXY_TOKEN` or an equivalent virtual-key variable instead of `LITELLM_MASTER_KEY`.

## Known Limitations

PII guardrail streaming restoration is not yet guaranteed. PII-sensitive examples and smokes should use non-streaming requests unless streaming restoration is explicitly hardened and tested.

Codex App cloud-managed features and hosted integrations are not in scope. The supported Codex App path is local tasks configured to use the custom provider.

Shared server-side subscription credentials are not in scope for this PR. The starting point is client-side passthrough: each developer uses their own Codex/ChatGPT or Claude Code subscription auth locally, and the proxy only forwards the provider auth headers while enforcing LiteLLM access controls.

OpenCode and Kilo Code subscription passthrough is not guaranteed in this PR. They remain supported through OpenAI-compatible proxy virtual keys and provider API-key/BYOK modes unless their clients can supply provider auth and separate `x-litellm-api-key` style proxy auth.
