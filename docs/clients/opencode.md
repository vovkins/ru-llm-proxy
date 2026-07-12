# OpenCode CLI / Desktop

OpenCode подключается к прокси как custom OpenAI-compatible provider.

Поддерживаемый сценарий:

- OpenCode CLI.
- OpenCode Desktop с той же server-side конфигурацией OpenCode.

## Ключи доступа

Используйте LiteLLM virtual key как клиентский токен:

```bash
export RU_LLM_PROXY_TOKEN="sk-..."
```

Настоящие `ZAI_API_KEY`, `ZAI_API_KEY_2`, optional provider keys и `LITELLM_MASTER_KEY` остаются только на proxy host. OpenCode получает только proxy token.

Все переменные окружения из этого гайда описаны в [../configuration.md](../configuration.md).

Обычные пользовательские ключи создавайте в LiteLLM Admin UI. CLI helper нужен только как дополнительный DevOps/CI/bootstrap путь с proxy host:

```bash
scripts/create_virtual_key.sh --alias opencode-local --models standard,zai --duration 30d
```

## Настройка

Добавьте provider в `opencode.json`:

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

Для новых настроек используйте `glm-5.2`. Алиас `glm-5.1` остаётся доступным
как дополнительная модель, если выданный ключ разрешает её использовать.

OpenAI/Anthropic aliases сейчас являются optional provider examples. Добавляйте их из `examples/litellm-config.optional-providers.yaml` только после проверки model IDs на целевом LiteLLM image и конкретной подписке.

## Проверка

OpenCode использует ту же OpenAI-compatible поверхность, что и chat smoke:

```text
POST /v1/chat/completions
```

Запустите:

```bash
make client-auth-smoke
```

## Ссылки

- OpenCode providers: https://opencode.ai/docs/providers/
- LiteLLM virtual keys: https://docs.litellm.ai/docs/proxy/virtual_keys
