# Kilo Code VS Code / CLI

Kilo Code подключается к прокси как провайдер, совместимый с OpenAI API.

Поддерживаемый сценарий:

- расширение Kilo Code для VS Code.
- конфигурация Kilo Code CLI с той же схемой провайдера.

## Ключи доступа

Используйте пользовательский ключ LiteLLM как клиентский токен:

```bash
export RU_LLM_PROXY_TOKEN="sk-..."
```

Настоящие `ZAI_API_KEY`, `ZAI_API_KEY_2`, ключи дополнительных провайдеров и `LITELLM_MASTER_KEY` остаются только на хосте прокси.

Все переменные окружения из этого гайда описаны в [../configuration.md](../configuration.md).

Обычные пользовательские ключи создавайте в административном интерфейсе LiteLLM. Вспомогательный скрипт командной строки нужен только как дополнительный путь для DevOps, CI и первичной настройки с хоста прокси:

```bash
scripts/create_virtual_key.sh --alias kilo-code-local --models standard,zai --duration 30d
```

## Расширение VS Code

В настройках Kilo Code:

- API Provider: `OpenAI Compatible`
- Base URL: `http://localhost:4000/v1`
- API Key: пользовательский ключ LiteLLM или секрет из переменной окружения, если ваша установка это поддерживает
- Model: `glm-5.2`, `glm-5.1` или другое разрешённое имя модели прокси

Имена моделей OpenAI/Anthropic сейчас являются примерами дополнительных провайдеров. Добавляйте их из `examples/litellm-config.optional-providers.yaml` только после проверки идентификаторов моделей на целевом образе LiteLLM и конкретной подписке.

## Конфигурация CLI / JSON

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

## Динамическая авторизация

В некоторых установках Kilo поддерживает более продвинутые обработчики провайдера или плагина. Считайте это интеграцией второго уровня: такой обработчик должен возвращать или добавлять пользовательский ключ LiteLLM, а не учётные данные внешнего провайдера.

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

- Пользовательские модели Kilo: https://kilo.ai/docs/code-with-ai/agents/custom-models
- Kilo: провайдер, совместимый с OpenAI API: https://kilo.ai/docs/ai-providers/openai-compatible
- Группы доступа моделей LiteLLM: https://docs.litellm.ai/docs/proxy/model_access_groups
