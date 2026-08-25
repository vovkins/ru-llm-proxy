# ZCode

ZCode подключается через пользовательского провайдера, совместимого с OpenAI
API. Поддерживается режим `Use API Key`; вход `Continue with Z.ai` или
`Continue with BigModel` не входит в этот сценарий.

## Ключи

```bash
export RU_LLM_PROXY_TOKEN="sk-..."
```

`ZAI_API_KEY`, `ZAI_API_KEY_2` и `LITELLM_MASTER_KEY` остаются на сервере.
ZCode получает только токен прокси. Не кладите `ZAI_API_KEY` или `LITELLM_MASTER_KEY` в настройки ZCode.

## Настройка

Выберите `Use API Key` и пользовательского провайдера OpenAI Compatible:

| Поле ZCode | Локально | В целевом окружении |
| --- | --- | --- |
| OpenAI Base URL | `http://localhost:4000/v1` | `https://<proxy-host>/v1` |
| API Key | `$RU_LLM_PROXY_TOKEN` | Пользовательский ключ LiteLLM |
| Model | `glm-5.2` | `glm-5.2` или разрешённая модель |

`glm-5.1` остаётся дополнительным именем; для новых настроек используйте
`glm-5.2`.

ZCode вызывает маршрут прокси `/v1`, а прокси обращается к Z.AI по двум
серверным ключам из `litellm-config.yaml`. Не указывайте адрес Z.AI и его ключи в
ZCode.

## Проверка

```bash
CHAT_MODEL=glm-5.2 make client-auth-smoke STACK=litellm-presidio
CHAT_MODEL=glm-5.2 make guardrails-smoke STACK=litellm-presidio
```

Частые причины ошибок:

- `401`/`403`: ключ прокси истёк или не разрешает модель;
- ошибка провайдера: на сервере неверен `ZAI_API_KEY` или `ZAI_API_KEY_2`;
- модель не найдена: адрес не заканчивается на `/v1` либо имя отсутствует в
  `litellm-config.yaml`.

Общие правила: [README.md](README.md).

## Ссылки

- [Конфигурация ZCode](https://zcode.z.ai/en/docs/configuration)
- [OpenAI-совместимый API Z.AI](https://docs.z.ai/api-reference/introduction)
- [Пользовательские ключи LiteLLM](https://docs.litellm.ai/docs/proxy/virtual_keys)
