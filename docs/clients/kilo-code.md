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

The real `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, and `ZAI_API_KEY` stay only on the proxy host.

Create routine user/client keys in LiteLLM Admin UI. The CLI helper is only an optional DevOps/CI/bootstrap path from the proxy host:

```bash
scripts/create_virtual_key.sh --alias kilo-code-local --models standard,zai,openai --duration 30d
```

## VS Code Extension

In Kilo Code settings:

- API Provider: `OpenAI Compatible`
- Base URL: `http://localhost:4000/v1`
- API Key: the LiteLLM virtual key, or an env-backed secret if your setup supports it
- Model: `zai-glm-5.1`, `openai-gpt-5.4-mini`, or another allowed proxy alias

Verify or replace the raw OpenAI model IDs behind `openai-gpt-*` aliases before production use. They are proxy-facing examples until live-validated with your provider account and LiteLLM image.

## CLI / JSON Configuration

Example config:

```jsonc
{
  "$schema": "https://app.kilo.ai/config.json",
  "model": "openai-compatible/zai-glm-5.1",
  "provider": {
    "openai-compatible": {
      "options": {
        "apiKey": "{env:RU_LLM_PROXY_TOKEN}",
        "baseURL": "http://localhost:4000/v1",
        "timeout": 300000
      },
      "models": {
        "zai-glm-5.1": {
          "name": "Z.AI GLM-5.1",
          "tool_call": true,
          "limit": {
            "context": 128000,
            "output": 8192
          }
        },
        "openai-gpt-5.4-mini": {
          "name": "OpenAI GPT-5.4 mini",
          "tool_call": true
        },
        "openai-gpt-5.5": {
          "name": "OpenAI GPT-5.5",
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
