# Codex CLI и Codex App

Codex подключается через OpenAI Responses API. Сценарий относится к Codex CLI и
локальным задачам Codex App; облачные задачи не используют локальную
конфигурацию.

## Граница ключей

```bash
export RU_LLM_PROXY_TOKEN="sk-..."
```

Не кладите `OPENAI_API_KEY` или `LITELLM_MASTER_KEY` в локальную конфигурацию
Codex. В обычном режиме они остаются на сервере. Общие правила:
[README.md](README.md).

## Настройка

Добавьте в `~/.codex/config.toml`:

```toml
model_provider = "ru_llm_proxy"
model = "gpt-6-luna"

[model_providers.ru_llm_proxy]
name = "ru-llm-proxy"
base_url = "http://localhost:4000/v1"
env_key = "RU_LLM_PROXY_TOKEN"
wire_api = "responses"
```

Обычный GLM-профиль не содержит OpenAI-модель. Для него добавьте проверенное имя
из
[`examples/litellm-config.optional-providers.yaml`](../../examples/litellm-config.optional-providers.yaml)
и замените `model` в примере. Расширенный состав с `codex-lb` уже публикует
`gpt-6-luna` и другие проверенные имена; порядок запуска приведён в
[`docs/codex-lb.md`](../codex-lb.md).

Для Codex App, который не наследует окружение оболочки, сохраните токен в
`~/.codex/.env` и перезапустите приложение:

```env
RU_LLM_PROXY_TOKEN=sk-...
```

## Подписка ChatGPT

В расширенном составе локальный Codex не передаёт собственную подписку.
Администратор отдельно импортирует OAuth-сессии в `codex-lb`, а пользователь
получает только ключ LiteLLM. Не копируйте пользовательский `auth.json` в
клиентскую конфигурацию прокси и не используйте одну импортированную сессию
одновременно в локальном Codex.

## Проверка

```bash
RESPONSES_MODEL=gpt-6-luna \
  make client-auth-smoke STACK=litellm-presidio-codex-lb
```

## Продолжение диалога

`previous_response_id` работает только с тем же виртуальным ключом и публичным
именем модели, пока доступно состояние прокси и провайдера. Срок новой связи
по умолчанию — час; успешный следующий ход сохраняет наследуемые замены заново.
Старые версии могут удаляться по бюджету хранения.

При `409 pii_history_unavailable` начните новый запрос с полной видимой
историей, **без** прежнего `previous_response_id`. Не подставляйте чужой ключ
и не повторяйте тот же ID бесконечно. `503` допускает повтор позднее; при `413`
сократите объём чувствительных данных. После ошибки SSE незавершённый ответ
не следует считать успешным ходом. Подробности: [архитектура](../architecture.md#продолжение-responses).

## Ссылки

- [Аутентификация Codex](https://developers.openai.com/codex/auth)
- [Конфигурация Codex](https://developers.openai.com/codex/config/)
- [Справочник конфигурации](https://developers.openai.com/codex/config-reference/)
