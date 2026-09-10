# Kilo Code для VS Code и CLI

Kilo Code подключается как клиент, совместимый с OpenAI API. Общие правила
ключей: [README.md](README.md).

## VS Code

Укажите в настройках:

| Поле | Значение |
| --- | --- |
| API Provider | `OpenAI Compatible` |
| Base URL | `http://localhost:4000/v1` |
| API Key | Пользовательский ключ LiteLLM |
| Model | `glm-5.2` |

## CLI

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
        "glm-5.2": {"name": "GLM-5.2", "tool_call": true},
        "glm-5.1": {"name": "GLM-5.1", "tool_call": true}
      }
    }
  }
}
```

Внешние ключи и `LITELLM_MASTER_KEY` не передавайте клиенту.

## Проверка

```bash
make client-auth-smoke STACK=litellm-presidio
```

## Ссылки

- [Пользовательские модели Kilo](https://kilo.ai/docs/code-with-ai/agents/custom-models)
- [OpenAI-совместимый провайдер Kilo](https://kilo.ai/docs/ai-providers/openai-compatible)
