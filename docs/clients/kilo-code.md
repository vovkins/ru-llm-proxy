# Kilo Code VS Code / CLI

Kilo Code connects to this proxy as an OpenAI-compatible provider.

Supported scope:

- Kilo Code VS Code extension.
- Kilo Code CLI configuration that uses the same provider schema.

## Credentials

Use a LiteLLM virtual key as the client token:

```bash
export RU_LLM_PROXY_TOKEN="sk-..."
```

The real `ZAI_API_KEY`, `ZAI_API_KEY_2`, optional provider keys, and `LITELLM_MASTER_KEY` stay only on the proxy host.

All environment variables used in this guide are documented in [../configuration.md](../configuration.md).

Create routine user/client keys in LiteLLM Admin UI. The CLI helper is only an optional DevOps/CI/bootstrap path from the proxy host:

```bash
scripts/create_virtual_key.sh --alias kilo-code-local --models standard,zai --duration 30d
```

## VS Code Extension

In Kilo Code settings:

- API Provider: `OpenAI Compatible`
- Base URL: `http://localhost:4000/v1`
- API Key: the LiteLLM virtual key, or an env-backed secret if your setup supports it
- Model: `glm-5.2`, `glm-5.1`, or another allowed proxy alias

OpenAI/Anthropic aliases are optional provider examples now. Add them from `examples/litellm-config.optional-providers.yaml` only after validating model IDs against the target LiteLLM image and provider subscription.

## CLI / JSON Configuration

Example config:

```jsonc
{
  "$schema": "https://app.kilo.ai/config.json",
  "model": "openai-compatible/glm-5.2",
  "provider": {
    "openai-compatible": {
      "options": {
        "apiKey": "{env:RU_LLM_PROXY_TOKEN}",
        "baseURL": "http://localhost:4000/v1",
        "timeout": 300000
      },
      "models": {
        "glm-5.2": {
          "name": "GLM-5.2",
          "tool_call": true,
          "limit": {
            "context": 128000,
            "output": 8192
          }
        },
        "glm-5.1": {
          "name": "GLM-5.1",
          "tool_call": true
        }
      }
    }
  }
}
```

## Dynamic Auth

Kilo supports more advanced provider/plugin hooks in some deployments. Treat those as a second-level integration: the hook should return or attach the LiteLLM virtual key, not upstream provider credentials.

## Smoke Test

Kilo uses:

```text
POST /v1/chat/completions
```

Run:

```bash
make client-auth-smoke
```

## References

- Kilo custom models: https://kilo.ai/docs/code-with-ai/agents/custom-models
- Kilo OpenAI-compatible provider: https://kilo.ai/docs/ai-providers/openai-compatible
- LiteLLM model access groups: https://docs.litellm.ai/docs/proxy/model_access_groups
