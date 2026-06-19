# Codex CLI / Codex App

Codex connects to this proxy through the OpenAI Responses API.

Supported scope:

- Codex CLI.
- Codex App local tasks that use local Codex configuration.

Not covered here:

- Codex cloud tasks and hosted integrations.
- Passing ChatGPT/Codex login files to the proxy as upstream credentials.

## Credentials

Use a LiteLLM virtual key as the client token:

```bash
export RU_LLM_PROXY_TOKEN="sk-..."
```

The real `OPENAI_API_KEY` stays only on the proxy host in server-funded mode. Do not put `OPENAI_API_KEY` or `LITELLM_MASTER_KEY` into local Codex client config.

Create routine user/client keys in LiteLLM Admin UI. The CLI helper is only an optional DevOps/CI/bootstrap path from the proxy host:

```bash
make virtual-key-create MODELS=openai,standard KEY_ALIAS=codex-local
```

If you call the script directly, pass the same values as flags:

```bash
scripts/create_virtual_key.sh --alias codex-local --models openai,standard --duration 30d
```

## Server-Funded Configuration

Use this mode when the proxy should pay with its server-side `OPENAI_API_KEY`. Add a custom provider to `~/.codex/config.toml`:

```toml
model_provider = "ru_llm_proxy"
model = "openai-gpt-5.4-mini"

[model_providers.ru_llm_proxy]
name = "ru-llm-proxy"
base_url = "http://localhost:4000/v1"
env_key = "RU_LLM_PROXY_TOKEN"
wire_api = "responses"
```

Use a higher-capability model when the issued key allows it:

```toml
model = "openai-gpt-5.5"
```

The `openai-gpt-*` names here are proxy-facing aliases. Verify the raw OpenAI model ID behind each alias against the current LiteLLM image and your provider account before using it in production.

## ChatGPT Subscription Passthrough

Use this mode when Codex should use the local user's ChatGPT/Codex subscription while still routing through the proxy for guardrails, tracking, and proxy access control.

This is an opt-in deployment mode. It requires LiteLLM client/provider auth header forwarding to be enabled in a dedicated environment and live-validated with the pinned LiteLLM image before it is marked production-ready. The default repo config does not enable header forwarding.

First sign in locally with Codex:

```bash
codex login
```

Then configure a provider that uses OpenAI authentication and sends the LiteLLM virtual key in a separate proxy-auth header:

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

In this mode:

- Codex keeps `~/.codex/auth.json` or OS credential-store auth on the client machine.
- The proxy authenticates the client with `x-litellm-api-key`.
- OpenAI/ChatGPT auth remains in the provider auth path and must be forwarded by the proxy.

Do not copy a shared Codex `auth.json` onto the proxy for all users. If live validation shows the normal LiteLLM `/v1/responses` route strips the required provider `Authorization` header, this mode needs a pass-through route or sidecar before it can be marked production-ready.

## Codex App Local Tasks

For local app workflows, keep the provider config in `~/.codex/config.toml`. Put the token in `~/.codex/.env` if the app process does not inherit your shell environment:

```env
RU_LLM_PROXY_TOKEN=sk-...
```

Restart Codex App after changing `~/.codex/.env`.

## Smoke Test

From the proxy repo:

```bash
make client-auth-smoke
```

The Codex-specific part checks `POST /v1/responses` only when `OPENAI_API_KEY` is configured and `RESPONSES_MODEL` is set to a live-validated proxy alias:

```bash
RESPONSES_MODEL=openai-gpt-5.4-mini make client-auth-smoke
```

## References

- Codex authentication: https://developers.openai.com/codex/auth
- Codex configuration: https://developers.openai.com/codex/config/
- Codex config reference: https://developers.openai.com/codex/config-reference/
- LiteLLM forward client headers: https://docs.litellm.ai/docs/proxy/forward_client_headers
- LiteLLM virtual keys: https://docs.litellm.ai/docs/proxy/virtual_keys
