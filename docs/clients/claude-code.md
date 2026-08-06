# Claude Code

Конфигурация репозитория проверяет базовую авторизацию Anthropic Messages;
полноценный шлюз Claude Code пока не считается полностью проверенным.

## Что сейчас валидируется

- обычный `POST /v1/messages` без потока;
- пользовательский ключ LiteLLM через `ANTHROPIC_AUTH_TOKEN`;
- модель, совместимая с Anthropic API и добавленная администратором.

Пока не проверены `POST /v1/messages?beta=true`, потоковых SSE-ответов,
пересылка `anthropic-version` и `anthropic-beta`, подсчёт токенов и обнаружение
моделей. Текущая базовая быстрая проверка авторизации Anthropic Messages не проверяет параметр `?beta=true` Claude Code.
Перед промышленным использованием добавьте быструю проверку шлюза Claude Code.

## Граница ключей

```bash
export RU_LLM_PROXY_TOKEN="sk-..."
```

Не кладите `ANTHROPIC_API_KEY` или `LITELLM_MASTER_KEY` в конфигурацию Claude
Code. В обычном режиме ключ провайдера остаётся на сервере. Общие правила:
[README.md](README.md).

## Настройка

```bash
export ANTHROPIC_BASE_URL="http://localhost:4000"
export ANTHROPIC_AUTH_TOKEN="$RU_LLM_PROXY_TOKEN"
export ANTHROPIC_MODEL="anthropic-example-standard"
```

Добавьте проверенную модель из
[`examples/litellm-config.optional-providers.yaml`](../../examples/litellm-config.optional-providers.yaml).

## Собственный API-ключ Anthropic

Сквозная передача клиентского ключа является отдельным режимом и не включена по
умолчанию. Ключ прокси передаётся отдельно от ключа провайдера:

```bash
curl "$ANTHROPIC_BASE_URL/v1/messages" \
  -H "Content-Type: application/json" \
  -H "x-litellm-api-key: Bearer $RU_LLM_PROXY_TOKEN" \
  -H "x-api-key: $ANTHROPIC_BYOK_API_KEY" \
  -d '{"model":"anthropic-example-standard","max_tokens":32,"messages":[{"role":"user","content":"Reply with ok."}]}'
```

## Подписка Claude

Авторизация подписки обычно использует провайдерский `Authorization`, который
обычный маршрут LiteLLM не обязан пересылать. Не копируйте общий файл учётных
данных Claude на прокси. Такой сценарий требует проверки на закреплённом образе и,
если заголовок удаляется, отдельного маршрута или адаптера.

`apiKeyHelper` может получать пользовательский ключ LiteLLM, но не внешний ключ
Anthropic:

```json
{"apiKeyHelper": "/absolute/path/to/print-ru-llm-proxy-token.sh"}
```

## Проверка

```bash
MESSAGES_MODEL=<validated-messages-alias> make client-auth-smoke
```

Полный контракт также требует проверки `POST /v1/messages/count_tokens` и
`GET /v1/models?limit=1000`, если клиент использует эти маршруты.

## Ссылки

- [LLM gateway для Claude Code](https://docs.anthropic.com/en/docs/claude-code/llm-gateway)
- [Настройки Claude Code](https://docs.anthropic.com/en/docs/claude-code/settings#environment-variables)
- [Anthropic Messages API в LiteLLM](https://docs.litellm.ai/docs/anthropic_unified/)
