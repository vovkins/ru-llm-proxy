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
model = "openai-example-standard"

[model_providers.ru_llm_proxy]
name = "ru-llm-proxy"
base_url = "http://localhost:4000/v1"
env_key = "RU_LLM_PROXY_TOKEN"
wire_api = "responses"
```

Активная конфигурация не содержит OpenAI-модель. Добавьте проверенный алиас из
[`examples/litellm-config.optional-providers.yaml`](../../examples/litellm-config.optional-providers.yaml).

Для Codex App, который не наследует окружение оболочки, сохраните токен в
`~/.codex/.env` и перезапустите приложение:

```env
RU_LLM_PROXY_TOKEN=sk-...
```

## Подписка ChatGPT

Сквозная передача локальной подписки не является готовым промышленным режимом.
Авторизация подписки использует провайдерский `Authorization`, который обычный
маршрут LiteLLM не обязан пересылать. Не копируйте общий `~/.codex/auth.json` на
прокси. Перед использованием нужен отдельный сквозной маршрут или доказанная
проверка закреплённой версии LiteLLM.

## Проверка

```bash
RESPONSES_MODEL=<validated-responses-alias> make client-auth-smoke
```

## Ссылки

- [Аутентификация Codex](https://developers.openai.com/codex/auth)
- [Конфигурация Codex](https://developers.openai.com/codex/config/)
- [Справочник конфигурации](https://developers.openai.com/codex/config-reference/)
