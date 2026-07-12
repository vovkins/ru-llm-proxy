# ZCode

ZCode подключается к прокси как клиент, совместимый с OpenAI API, в режиме API Key.

Поддерживаемый сценарий:

- ZCode настроен через `Use API Key`.
- В ZCode выбран пользовательский провайдер, совместимый с OpenAI API, с OpenAI Base URL и API key.
- Доступ к Z.AI/GLM оплачивается серверными ключами ru-llm-proxy.

Что не входит в этот гайд:

- вход в аккаунт ZCode через `Continue with Z.ai` или `Continue with BigModel`.
- хранение общих учётных данных аккаунта/сессии ZCode на прокси;
- сквозная передача подписки или авторизации аккаунта для ZCode;
- прямой вход в аккаунт ZCode или учётные данные внешнего Z.AI внутри ZCode, если запросы должны идти через ru-llm-proxy.

## Ключи доступа

Используйте пользовательский ключ LiteLLM как клиентский токен ZCode:

```bash
export RU_LLM_PROXY_TOKEN="sk-..."
```

Настоящие `ZAI_API_KEY`, `ZAI_API_KEY_2`, ключи GLM Coding Plan, ключи дополнительных провайдеров и `LITELLM_MASTER_KEY` остаются только на хосте прокси. ZCode получает только токен прокси. Не кладите `ZAI_API_KEY` или `LITELLM_MASTER_KEY` в настройки ZCode.

Все переменные окружения из этого гайда описаны в [../configuration.md](../configuration.md).

Обычные пользовательские ключи создавайте в административном интерфейсе LiteLLM. Вспомогательный скрипт командной строки нужен только как дополнительный путь для DevOps, CI и первичной настройки с хоста прокси:

```bash
scripts/create_virtual_key.sh --alias zcode-local --models standard,zai --duration 30d
```

Помощник печатает `RU_LLM_PROXY_TOKEN=...`; это значение нужно вставить в поле API Key в ZCode.

## Настройка ZCode

При первом запуске выберите `Use API Key`. Если ZCode уже настроен, откройте настройки модели/провайдера и добавьте пользовательского провайдера, совместимого с OpenAI API.

Используйте такие значения:

| Поле ZCode | Локальное значение | Значение в целевом окружении |
| --- | --- | --- |
| OpenAI Base URL | `http://localhost:4000/v1` | `https://<proxy-host>/v1` |
| API Key | `$RU_LLM_PROXY_TOKEN` | Пользовательский ключ LiteLLM, выданный прокси |
| Model | `glm-5.2` | `glm-5.2` или другое разрешённое имя модели прокси |

Дополнительный алиас `glm-5.1` также доступен в текущей конфигурации репозитория для установок, которым нужна предыдущая версия модели. Для новых настроек и smoke-проверок используйте `glm-5.2`.

Пример локальной конфигурации:

```text
Provider: OpenAI Compatible / Custom
OpenAI Base URL: http://localhost:4000/v1
API Key: $RU_LLM_PROXY_TOKEN
Model: glm-5.2
```

С такой настройкой запросы ZCode проходят тот же путь, что и запросы других клиентов: авторизация по пользовательскому ключу, защитные слои, маскирование/восстановление персональных данных, словарные подстановки, маршрутизация, бюджеты, журналы аудита и метрики.

## Настройка Z.AI на стороне прокси

Ключ внешнего провайдера Z.AI должен находиться в окружении прокси:

```env
ZAI_API_KEY=...
ZAI_API_KEY_2=...
```

Записи Z.AI по умолчанию в `litellm-config.yaml` используют конечную точку GLM Coding Plan,
совместимую с OpenAI API:

```yaml
model_list:
  - model_name: glm-5.2
    litellm_params:
      model: openai/glm-5.2
      api_base: https://api.z.ai/api/coding/paas/v4
      api_key: os.environ/ZAI_API_KEY
    model_info:
      id: glm-5-2-zai-coding-primary
  - model_name: glm-5.2
    litellm_params:
      model: openai/glm-5.2
      api_base: https://api.z.ai/api/coding/paas/v4
      api_key: os.environ/ZAI_API_KEY_2
    model_info:
      id: glm-5-2-zai-coding-secondary
```

Не настраивайте эту внешнюю конечную точку или внешний API-ключ напрямую в ZCode, если ZCode должен работать через ru-llm-proxy. ZCode вызывает маршрут прокси `/v1`, а уже прокси вызывает Z.AI.

## Дополнительные GLM-модели

`glm-5.1` доступна как дополнительное имя модели прокси. Используйте его только там,
где клиенту или установке намеренно нужна предыдущая версия модели:

```bash
CHAT_MODEL=glm-5.1 make client-auth-smoke
```

Для новых настроек оставляйте `glm-5.2` моделью по умолчанию.

## Проверка

Когда прокси запущен и пользовательский ключ создан, проверьте путь чата, совместимый с OpenAI API:

```bash
CHAT_MODEL=glm-5.2 make client-auth-smoke
```

Для проверки поведения защитного слоя:

```bash
CHAT_MODEL=glm-5.2 make guardrails-smoke
```

Частые ошибки:

- `401` или `403`: пользовательский ключ LiteLLM отсутствует, истёк или не имеет доступа к выбранной группе моделей.
- Ошибка авторизации провайдера: `ZAI_API_KEY` или `ZAI_API_KEY_2` отсутствует или некорректен на хосте прокси.
- Ошибка соединения или отсутствующей модели: ZCode Base URL не заканчивается на `/v1` или имя модели отсутствует в `litellm-config.yaml`.

## Ссылки

- Конфигурация ZCode: https://zcode.z.ai/en/docs/configuration
- Аутентификация Z.AI API и доступ в стиле OpenAI SDK: https://docs.z.ai/api-reference/introduction
- Инструментальная конечная точка Z.AI GLM Coding Plan: https://docs.z.ai/devpack/tool/crush
- Пользовательские ключи LiteLLM: https://docs.litellm.ai/docs/proxy/virtual_keys
- Провайдер LiteLLM, совместимый с OpenAI API: https://docs.litellm.ai/docs/providers/openai_compatible
