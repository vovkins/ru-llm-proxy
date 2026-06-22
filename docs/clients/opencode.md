# OpenCode CLI / Desktop

OpenCode connects to this proxy as a custom OpenAI-compatible provider.

Supported scope:

- OpenCode CLI.
- OpenCode Desktop using the same server-side OpenCode configuration.

## Credentials

Use a LiteLLM virtual key as the client token:

```bash
export RU_LLM_PROXY_TOKEN="sk-..."
```

The real `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, and `ZAI_API_KEY` stay only on the proxy host. OpenCode receives only the proxy token.

Create routine user/client keys in LiteLLM Admin UI. The CLI helper is only an optional DevOps/CI/bootstrap path from the proxy host:

```bash
scripts/create_virtual_key.sh --alias opencode-local --models standard,zai,openai --duration 30d
```

## Configuration

Add a provider to `opencode.json`:

```jsonc
{
  "$schema": "https://opencode.ai/config.json",
  "model": "ru-llm-proxy/zai-glm-5.1",
  "provider": {
    "ru-llm-proxy": {
      "npm": "@ai-sdk/openai-compatible",
      "name": "ru-llm-proxy",
      "options": {
        "baseURL": "http://localhost:4000/v1",
        "apiKey": "{env:RU_LLM_PROXY_TOKEN}"
      },
      "models": {
        "zai-glm-5.1": {
          "name": "Z.AI GLM-5.1"
        },
        "openai-gpt-5.4-mini": {
          "name": "OpenAI GPT-5.4 mini"
        },
        "openai-gpt-5.5": {
          "name": "OpenAI GPT-5.5"
        }
      }
    }
  }
}
```

Use `zai-glm-5.1` for Z.AI, or switch to an OpenAI alias when the key allows it.

The `openai-gpt-*` entries are proxy-facing examples. Verify or replace the raw OpenAI model IDs behind those aliases before production use.

## Smoke Test

OpenCode uses the same OpenAI-compatible surface as the chat smoke:

```text
POST /v1/chat/completions
```

Run:

```bash
make client-auth-smoke
```

## References

- OpenCode providers: https://opencode.ai/docs/providers/
- LiteLLM virtual keys: https://docs.litellm.ai/docs/proxy/virtual_keys
