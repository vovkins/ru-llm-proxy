# Codex CLI и Codex App

Codex подключается к ru-llm-proxy через OpenAI Responses API. Инструкция
относится к Codex CLI и локальным задачам Codex App; облачные задачи не
используют локальную конфигурацию.

## Предварительная настройка

Администратор должен применить
[пул ChatGPT OAuth](../configuration.md#локальные-профили-openai-oauth) и выдать
пользователю ключ LiteLLM с доступом к одной или нескольким моделям:

| Модель | Назначение |
| --- | --- |
| `gpt-5.6-sol` | Основная модель с приоритетом качества |
| `gpt-5.6-terra` | Баланс качества и потребления ресурсов |
| `gpt-5.6-luna` | Быстрые запросы и регулярные проверки |

Общий алиас `gpt-5.6` не используется. OAuth-сессии находятся только на сервере;
пользователь не получает файлы `auth.json`, токены подписок или
`LITELLM_MASTER_KEY`.

## Настройка клиента

```bash
export RU_LLM_PROXY_TOKEN="sk-..."
```

Не кладите `OPENAI_API_KEY` или `LITELLM_MASTER_KEY` в конфигурацию Codex:
клиенту нужен только выданный администратором пользовательский ключ прокси.

Добавьте в `~/.codex/config.toml`:

```toml
model_provider = "ru_llm_proxy"
model = "gpt-5.6-sol"

[model_providers.ru_llm_proxy]
name = "ru-llm-proxy"
base_url = "http://localhost:4000/v1"
env_key = "RU_LLM_PROXY_TOKEN"
wire_api = "responses"
```

Для Codex App, который не наследует окружение оболочки, сохраните только
пользовательский ключ прокси в `~/.codex/.env` и перезапустите приложение:

```env
RU_LLM_PROXY_TOKEN=sk-...
```

## Продолжение диалога

При `store: false` Codex может возвращать в следующем запросе зашифрованное
состояние рассуждения модели в поле `encrypted_content`. Прокси считает это поле
непрозрачным: не анализирует, не маскирует, не восстанавливает и не записывает в
журналы. Открытый текст каждого хода проходит обычные защитные проверки.

Используйте один пользовательский ключ LiteLLM на протяжении задачи. Его
привязка к OAuth-подписке действует сразу для Sol, Terra и Luna и продлевается
после каждого успешного запроса. Проверка двух реальных подписок подтвердила
обычное и потоковое продолжение диалога, в том числе с маскированием нового
открытого текста.

## Проверка

```bash
RESPONSES_MODEL=gpt-5.6-luna make client-auth-smoke
```

Для проверки качества выберите `gpt-5.6-sol`; для повторяемой диагностики
используйте `gpt-5.6-luna`.

## Ссылки

- [Конфигурация Codex](https://developers.openai.com/codex/config/)
- [Справочник конфигурации](https://developers.openai.com/codex/config-reference/)
- [Responses API](https://developers.openai.com/api/reference/cli/resources/responses/methods/create)
- [Семейство GPT-5.6](https://developers.openai.com/api/docs/guides/latest-model)
