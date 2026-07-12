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

The real `ZAI_API_KEY`, `ZAI_API_KEY_2`, optional provider keys, and `LITELLM_MASTER_KEY` stay only on the proxy host. OpenCode receives only the proxy token.

All environment variables used in this guide are documented in [../configuration.md](../configuration.md).

Create routine user/client keys in LiteLLM Admin UI. The CLI helper is only an optional DevOps/CI/bootstrap path from the proxy host:

```bash
scripts/create_virtual_key.sh --alias opencode-local --models standard,zai --duration 30d
```

## Configuration

Add a provider to `opencode.json`:

```jsonc
{
  "$schema": "https://opencode.ai/config.json",
  "model": "ru-llm-proxy/glm-5.2",
  "provider": {
    "ru-llm-proxy": {
      "npm": "@ai-sdk/openai-compatible",
      "name": "ru-llm-proxy",
      "options": {
        "baseURL": "http://localhost:4000/v1",
        "apiKey": "{env:RU_LLM_PROXY_TOKEN}"
      },
      "models": {
        "glm-5.2": {
          "name": "GLM-5.2"
        },
        "glm-5.1": {
          "name": "GLM-5.1"
        }
      }
    }
  }
}
```

Use `glm-5.2` for new setup. `glm-5.1` remains available as an additional alias when the key allows it.

OpenAI/Anthropic aliases are optional provider examples now. Add them from `examples/litellm-config.optional-providers.yaml` only after validating model IDs against the target LiteLLM image and provider subscription.

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
