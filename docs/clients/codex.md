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

The real `OPENAI_API_KEY` stays only on the proxy host. Do not put `OPENAI_API_KEY` or `LITELLM_MASTER_KEY` into local Codex client config.

Create a client key from the proxy host:

```bash
make virtual-key-create MODELS=openai,standard KEY_ALIAS=codex-local
```

If you call the script directly, pass the same values as flags:

```bash
scripts/create_virtual_key.sh --alias codex-local --models openai,standard --duration 30d
```

## CLI Configuration

Add a custom provider to `~/.codex/config.toml`:

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

The Codex-specific part checks `POST /v1/responses` when `OPENAI_API_KEY` is configured on the proxy.

## References

- Codex configuration: https://developers.openai.com/codex/config/
- Codex config reference: https://developers.openai.com/codex/config-reference/
- LiteLLM virtual keys: https://docs.litellm.ai/docs/proxy/virtual_keys
