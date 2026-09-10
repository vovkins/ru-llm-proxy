# Подключение клиентов

Все клиенты обращаются к ru-llm-proxy по пользовательскому ключу LiteLLM.
Ключи внешних провайдеров и `LITELLM_MASTER_KEY` остаются на сервере.

## Общие параметры

| Параметр | Значение |
| --- | --- |
| Адрес OpenAI-совместимых клиентов | `http://localhost:4000/v1` |
| Адрес Anthropic-совместимых клиентов | `http://localhost:4000` |
| Пользовательский токен | `RU_LLM_PROXY_TOKEN=sk-...` |
| Модель по умолчанию | `glm-5.2` |
| Дополнительная модель | `glm-5.1` |

Создавайте пользовательские ключи в административном интерфейсе LiteLLM. Для
локальной настройки и автоматизации доступна команда:

```bash
make virtual-key-create STACK=litellm-presidio \
  KEY_ALIAS=local-client MODELS=standard,zai DURATION=30d
```

Права, бюджеты, сроки и ротация описаны в
[административном руководстве](../admin-access.md). Переменные окружения — в
[справочнике конфигурации](../configuration.md).

## Клиенты

| Клиент | Протокол | Статус |
| --- | --- | --- |
| [ZCode](zcode.md) | OpenAI Chat Completions | Поддерживается |
| [OpenCode](opencode.md) | OpenAI Chat Completions | Поддерживается |
| [Kilo Code](kilo-code.md) | OpenAI Chat Completions | Поддерживается |
| [Codex](codex.md) | OpenAI Responses API | Базовый профиль требует добавленной модели; эксперимент `codex-lb` публикует проверенные имена OpenAI |
| [Claude Code](claude-code.md) | Anthropic Messages API | Базовый запрос проверен; полный шлюз не подтверждён |
| [JWT/OIDC](jwt.md) | Входная аутентификация LiteLLM | Дополнительная корпоративная схема |

## Проверка

```bash
make client-auth-smoke STACK=litellm-presidio
make guardrails-smoke STACK=litellm-presidio
```

`client-auth-smoke` проверяет доступные в текущей установке протоколы.
`guardrails-smoke` проверяет маскирование, восстановление и очистку временных
сопоставлений. Для конкретных моделей используйте переменные из руководства
нужного клиента.

## Граница секретов

- Клиент получает только пользовательский ключ LiteLLM или корпоративный JWT.
- `LITELLM_MASTER_KEY` не используется в клиентских приложениях.
- `ZAI_API_KEY`, `OPENAI_API_KEY` и `ANTHROPIC_API_KEY` не копируются на
  пользовательские машины в режиме, где провайдера оплачивает прокси.
- Сквозная передача собственного ключа или подписки является отдельным режимом и
  требует проверки на закреплённой версии LiteLLM.
