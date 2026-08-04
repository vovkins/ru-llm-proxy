# OpenCode CLI и Desktop

OpenCode использует пользовательского провайдера, совместимого с OpenAI API.
Правила ключей и общие проверки: [README.md](README.md).

## Настройка

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
        "glm-5.2": {"name": "GLM-5.2"},
        "glm-5.1": {"name": "GLM-5.1"}
      }
    }
  }
}
```

Клиент получает только пользовательский ключ LiteLLM. Ключи провайдеров и
`LITELLM_MASTER_KEY` остаются на сервере.

## Проверка

OpenCode вызывает `POST /v1/chat/completions`:

```bash
make client-auth-smoke
```

## Ссылки

- [Провайдеры OpenCode](https://opencode.ai/docs/providers/)
- [Пользовательские ключи LiteLLM](https://docs.litellm.ai/docs/proxy/virtual_keys)
