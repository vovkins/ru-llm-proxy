# ZCode

ZCode подключается к прокси как OpenAI-compatible клиент в режиме API Key.

Поддерживаемый сценарий:

- ZCode настроен через `Use API Key`.
- В ZCode выбран custom/OpenAI-compatible provider с OpenAI Base URL и API key.
- Доступ к Z.AI/GLM оплачивается серверными ключами ru-llm-proxy.

Что не входит в этот гайд:

- ZCode account login through `Continue with Z.ai` or `Continue with BigModel`.
- хранение общих ZCode account/session credentials на прокси;
- subscription или account-auth passthrough для ZCode;
- прямой ZCode account login или upstream Z.AI credentials внутри ZCode, если запросы должны идти через ru-llm-proxy.

## Ключи доступа

Используйте LiteLLM virtual key как клиентский токен ZCode:

```bash
export RU_LLM_PROXY_TOKEN="sk-..."
```

Настоящие `ZAI_API_KEY`, `ZAI_API_KEY_2`, GLM Coding Plan keys, optional provider keys и `LITELLM_MASTER_KEY` остаются только на proxy host. ZCode получает только proxy token. Не кладите `ZAI_API_KEY` или `LITELLM_MASTER_KEY` в настройки ZCode.

Все переменные окружения из этого гайда описаны в [../configuration.md](../configuration.md).

Обычные пользовательские ключи создавайте в LiteLLM Admin UI. CLI helper нужен только как дополнительный DevOps/CI/bootstrap путь с proxy host:

```bash
scripts/create_virtual_key.sh --alias zcode-local --models standard,zai --duration 30d
```

Helper печатает `RU_LLM_PROXY_TOKEN=...`; это значение нужно вставить в поле API Key в ZCode.

## Настройка ZCode

При первом запуске выберите `Use API Key`. Если ZCode уже настроен, откройте настройки модели/провайдера и добавьте OpenAI-compatible custom provider.

Используйте такие значения:

| ZCode field | Local value | Deployed value |
| --- | --- | --- |
| OpenAI Base URL | `http://localhost:4000/v1` | `https://<proxy-host>/v1` |
| API Key | `$RU_LLM_PROXY_TOKEN` | LiteLLM virtual key, выданный прокси |
| Model | `glm-5.2` | `glm-5.2` или другой разрешённый proxy alias |

Дополнительный алиас `glm-5.1` также доступен в текущей конфигурации репозитория для установок, которым нужна предыдущая версия модели. Для новых настроек и smoke-проверок используйте `glm-5.2`.

Пример локальной конфигурации:

```text
Provider: OpenAI Compatible / Custom
OpenAI Base URL: http://localhost:4000/v1
API Key: $RU_LLM_PROXY_TOKEN
Model: glm-5.2
```

С такой настройкой запросы ZCode проходят тот же путь, что и запросы других клиентов: virtual-key auth, guardrails, PII masking/restoration, dictionary substitutions, routing, budgets, audit logs и metrics.

## Настройка Z.AI на стороне прокси

Upstream-ключ Z.AI должен находиться в окружении прокси:

```env
ZAI_API_KEY=...
ZAI_API_KEY_2=...
```

Дефолтные Z.AI entries в `litellm-config.yaml` используют GLM Coding Plan
OpenAI-compatible endpoint:

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

Не настраивайте этот upstream endpoint или upstream API key напрямую в ZCode, если ZCode должен работать через ru-llm-proxy. ZCode вызывает proxy endpoint `/v1`, а уже прокси вызывает Z.AI.

## Дополнительные GLM-модели

`glm-5.1` доступна как дополнительный proxy alias. Используйте его только там,
где клиенту или установке намеренно нужна предыдущая версия модели:

```bash
CHAT_MODEL=glm-5.1 make client-auth-smoke
```

Для новых настроек оставляйте `glm-5.2` моделью по умолчанию.

## Проверка

Когда proxy запущен и virtual key создан, проверьте OpenAI-compatible chat path:

```bash
CHAT_MODEL=glm-5.2 make client-auth-smoke
```

Для проверки поведения guardrail:

```bash
CHAT_MODEL=glm-5.2 make guardrails-smoke
```

Частые ошибки:

- `401` or `403`: the LiteLLM virtual key is missing, expired, or not allowed to use the selected model group.
- Provider authentication error: `ZAI_API_KEY` or `ZAI_API_KEY_2` is missing or invalid on the proxy host.
- Connection or model-not-found error: the ZCode Base URL does not end with `/v1`, or the model alias is not present in `litellm-config.yaml`.

## Ссылки

- ZCode configuration: https://zcode.z.ai/en/docs/configuration
- Z.AI API authentication and OpenAI SDK style access: https://docs.z.ai/api-reference/introduction
- Z.AI GLM Coding Plan tooling endpoint: https://docs.z.ai/devpack/tool/crush
- LiteLLM virtual keys: https://docs.litellm.ai/docs/proxy/virtual_keys
- LiteLLM OpenAI-compatible provider: https://docs.litellm.ai/docs/providers/openai_compatible
