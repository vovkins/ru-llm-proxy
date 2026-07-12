# Kilo Code VS Code / CLI

Kilo Code подключается к прокси как OpenAI-compatible provider.

Поддерживаемый сценарий:

- Kilo Code VS Code extension.
- Kilo Code CLI configuration с той же provider schema.

## Ключи доступа

Используйте LiteLLM virtual key как клиентский токен:

```bash
export RU_LLM_PROXY_TOKEN="sk-..."
```

Настоящие `ZAI_API_KEY`, `ZAI_API_KEY_2`, optional provider keys и `LITELLM_MASTER_KEY` остаются только на proxy host.

Все переменные окружения из этого гайда описаны в [../configuration.md](../configuration.md).

Обычные пользовательские ключи создавайте в LiteLLM Admin UI. CLI helper нужен только как дополнительный DevOps/CI/bootstrap путь с proxy host:

```bash
scripts/create_virtual_key.sh --alias kilo-code-local --models standard,zai --duration 30d
```

## Расширение VS Code

В настройках Kilo Code:

- API Provider: `OpenAI Compatible`
- Base URL: `http://localhost:4000/v1`
- API Key: LiteLLM virtual key или env-backed secret, если ваша установка это поддерживает
- Model: `glm-5.2`, `glm-5.1` или другой разрешённый proxy alias

OpenAI/Anthropic aliases сейчас являются optional provider examples. Добавляйте их из `examples/litellm-config.optional-providers.yaml` только после проверки model IDs на целевом LiteLLM image и конкретной подписке.

## CLI / JSON configuration

Пример конфигурации:

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

## Dynamic auth

В некоторых установках Kilo поддерживает более продвинутые provider/plugin hooks. Считайте это интеграцией второго уровня: hook должен возвращать или добавлять LiteLLM virtual key, а не upstream provider credentials.

## Проверка

Kilo использует:

```text
POST /v1/chat/completions
```

Запустите:

```bash
make client-auth-smoke
```

## Ссылки

- Kilo custom models: https://kilo.ai/docs/code-with-ai/agents/custom-models
- Kilo OpenAI-compatible provider: https://kilo.ai/docs/ai-providers/openai-compatible
- LiteLLM model access groups: https://docs.litellm.ai/docs/proxy/model_access_groups
