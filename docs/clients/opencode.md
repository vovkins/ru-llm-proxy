# OpenCode CLI / Desktop

OpenCode подключается к прокси как пользовательский провайдер, совместимый с OpenAI API.

Поддерживаемый сценарий:

- OpenCode CLI.
- OpenCode Desktop с той же серверной конфигурацией OpenCode.

## Ключи доступа

Используйте пользовательский ключ LiteLLM как клиентский токен:

```bash
export RU_LLM_PROXY_TOKEN="sk-..."
```

Настоящие `ZAI_API_KEY`, `ZAI_API_KEY_2`, ключи дополнительных провайдеров и `LITELLM_MASTER_KEY` остаются только на хосте прокси. OpenCode получает только токен прокси.

Все переменные окружения из этого гайда описаны в [../configuration.md](../configuration.md).

Обычные пользовательские ключи создавайте в административном интерфейсе LiteLLM. Вспомогательный скрипт командной строки нужен только как дополнительный путь для DevOps, CI и первичной настройки с хоста прокси:

```bash
scripts/create_virtual_key.sh --alias opencode-local --models standard,zai --duration 30d
```

## Настройка

Добавьте провайдера в `opencode.json`:

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

Имена моделей OpenAI/Anthropic сейчас являются примерами дополнительных провайдеров. Добавляйте их из `examples/litellm-config.optional-providers.yaml` только после проверки идентификаторов моделей на целевом образе LiteLLM и конкретной подписке.

## Проверка

OpenCode использует ту же поверхность, совместимую с OpenAI API, что и быстрая проверка чата:

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
