# Client Access Gateway Design

## Goal

Turn the project from a single Z.AI-oriented proxy into a practical corporate LLM gateway for coding tools. The gateway must hold upstream provider credentials itself and expose only proxy-issued client credentials to local tools such as Codex CLI/App, Claude Code, OpenCode, and Kilo Code.

## Scope

This PR covers the first production-oriented slice:

- Client access to the proxy through LiteLLM virtual keys.
- Multi-provider LiteLLM configuration for Z.AI, OpenAI, and Anthropic.
- Client setup docs for Codex CLI/App local tasks, Claude Code, OpenCode CLI/Desktop, and Kilo Code VS Code/CLI.
- Helper scripts and Make targets for creating client virtual keys and smoke-testing supported protocols.
- Existing user examples converted away from `LITELLM_MASTER_KEY`.

This PR does not implement subscription-backed upstream adapters for Codex or Claude Code auth files. That is a separate spike because those auth files are client credentials, not documented server-side LiteLLM upstream credentials.

## Auth Boundaries

There are two separate credential layers.

### Proxy Ingress Credentials

Ingress credentials answer: "Who may use this proxy?"

The proxy issues LiteLLM virtual keys to users, teams, CI jobs, and local tools. Those keys are sent by clients as bearer tokens or through a client-specific equivalent:

- OpenAI-compatible clients use `Authorization: Bearer <virtual-key>`.
- Codex custom providers use a configured `env_key` such as `RU_LLM_PROXY_TOKEN`.
- Claude Code uses `ANTHROPIC_AUTH_TOKEN=<virtual-key>` against the proxy.
- OpenCode and Kilo Code use their OpenAI-compatible provider API key field, usually sourced from `RU_LLM_PROXY_TOKEN`.

`LITELLM_MASTER_KEY` remains admin-only. It is used for LiteLLM admin APIs such as `/key/generate` and must not appear in normal client examples.

### Upstream Provider Credentials

Upstream credentials answer: "How does the proxy call the real provider?"

The proxy stores provider credentials in `.env` and injects them into LiteLLM:

- `ZAI_API_KEY` for Z.AI / GLM.
- `OPENAI_API_KEY` for OpenAI Platform models and the Responses API.
- `ANTHROPIC_API_KEY` for Anthropic Console API models and Anthropic Messages API.

These credentials never leave the server. Local clients do not receive provider keys.

Subscription-backed upstream access through Codex or Claude Code login/auth files is explicitly out of this PR. If pursued, it needs a separate research spike and likely a dedicated adapter or sidecar, because Codex and Claude Code auth files are documented for local client workflows rather than generic LiteLLM upstream authentication.

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

## Model Naming

Expose stable proxy-facing aliases rather than raw provider names. Initial aliases should be clear enough for users and restrictive enough for LiteLLM virtual key policies.

Recommended initial names:

- `zai-glm-5.1` -> Z.AI GLM model through the existing OpenAI-compatible Z.AI endpoint.
- `openai-gpt-4o-mini` -> OpenAI fast/general model.
- `openai-gpt-4o` -> OpenAI higher-capability model.
- `claude-sonnet` -> Anthropic Sonnet class model.
- `claude-haiku` -> Anthropic Haiku class model.

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

Update existing README and examples so normal client requests use `RU_LLM_PROXY_TOKEN` or an equivalent virtual-key variable instead of `LITELLM_MASTER_KEY`.

## Known Limitations

PII guardrail streaming restoration is not yet guaranteed. PII-sensitive examples and smokes should use non-streaming requests unless streaming restoration is explicitly hardened and tested.

Codex App cloud-managed features and hosted integrations are not in scope. The supported Codex App path is local tasks configured to use the custom provider.

Subscription-backed upstream credentials are not in scope for this PR. They need a separate spike that validates legality, provider terms, and technical feasibility before implementation.
