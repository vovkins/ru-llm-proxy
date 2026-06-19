# Примеры API

Все примеры соответствуют текущей конфигурации репозитория: LiteLLM на `localhost:4000`, базовая Z.AI модель `zai-glm-5.1`, OpenAI aliases `openai-gpt-5.4-mini` / `openai-gpt-5.5` и Anthropic aliases `claude-haiku-4.5` / `claude-sonnet-4.6` / `claude-opus-4.8`.

## Окружение

```bash
export API_URL="http://localhost:4000"
export RU_LLM_PROXY_TOKEN="sk-..."
```

Создайте `RU_LLM_PROXY_TOKEN` через Admin UI или CLI helper:

```bash
make virtual-key-create KEY_ALIAS=local-examples MODELS=standard,zai,openai,anthropic DURATION=30d
```

`LITELLM_MASTER_KEY` используется только для admin-операций, например создания virtual keys и просмотра списка guardrails.

## Режимы авторизации

Server-funded режим использует proxy token как обычный bearer token. Proxy сам вызывает провайдера через серверные `OPENAI_API_KEY`, `ANTHROPIC_API_KEY` или `ZAI_API_KEY`:

```bash
curl -s "$API_URL/v1/chat/completions" \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $RU_LLM_PROXY_TOKEN" \
  -d '{"model":"zai-glm-5.1","messages":[{"role":"user","content":"Привет"}]}'
```

Subscription/BYOK passthrough режим разделяет proxy auth и provider auth. Proxy token передаётся в `x-litellm-api-key`, а `Authorization` или provider-specific header остаётся для upstream:

```bash
curl -s "$API_URL/v1/messages" \
  -H "Content-Type: application/json" \
  -H "x-litellm-api-key: Bearer $RU_LLM_PROXY_TOKEN" \
  -H "Authorization: Bearer $CLAUDE_OAUTH_TOKEN" \
  -d '{
    "model": "claude-sonnet-4.6",
    "max_tokens": 80,
    "messages": [{"role": "user", "content": "Привет"}]
  }'
```

На практике Codex и Claude Code сами управляют subscription auth на клиентской машине; см. [clients/codex.md](clients/codex.md) и [clients/claude-code.md](clients/claude-code.md). Не кладите общий Codex `auth.json` или Claude credentials на proxy.

## Chat completion без PII

```bash
curl -s "$API_URL/v1/chat/completions" \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $RU_LLM_PROXY_TOKEN" \
  -d '{
    "model": "zai-glm-5.1",
    "messages": [
      {
        "role": "user",
        "content": "Скажи короткое приветствие на русском"
      }
    ],
    "max_tokens": 80
  }' | jq '.choices[0].message'
```

## Chat completion с PII

```bash
curl -s "$API_URL/v1/chat/completions" \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $RU_LLM_PROXY_TOKEN" \
  -d '{
    "model": "zai-glm-5.1",
    "messages": [
      {
        "role": "user",
        "content": "Клиент Иванов Иван, телефон +79031234567, ИНН 7707083893. Составь краткую справку."
      }
    ],
    "max_tokens": 120
  }' | jq '.choices[0].message'
```

Перед вызовом провайдера guardrail отправляет masked text примерно такого вида:

```text
Клиент <PERSON_1>, телефон <PHONE_NUMBER_1>, ИНН <RU_INN_1>. Составь краткую справку.
```

Если ответ провайдера содержит эти плейсхолдеры, post-call hook восстановит исходные значения перед возвратом клиенту.

## Несколько значений одного типа

```bash
curl -s "$API_URL/v1/chat/completions" \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $RU_LLM_PROXY_TOKEN" \
  -d '{
    "model": "zai-glm-5.1",
    "messages": [
      {
        "role": "user",
        "content": "Основной телефон +79031234567, резервный телефон 89031234567."
      }
    ],
    "max_tokens": 80
  }' | jq '.choices[0].message'
```

Провайдер получает разные плейсхолдеры:

```text
Основной телефон <PHONE_NUMBER_1>, резервный телефон <PHONE_NUMBER_2>.
```

## OpenAI Responses API

Codex CLI/App local tasks используют Responses API:

```bash
curl -s "$API_URL/v1/responses" \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $RU_LLM_PROXY_TOKEN" \
  -d '{
    "model": "openai-gpt-5.4-mini",
    "input": "Скажи короткое приветствие на русском",
    "max_output_tokens": 80
  }' | jq
```

## Anthropic Messages API

Claude Code использует Anthropic-compatible Messages API:

```bash
curl -s "$API_URL/v1/messages" \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $RU_LLM_PROXY_TOKEN" \
  -d '{
    "model": "claude-sonnet-4.6",
    "max_tokens": 80,
    "messages": [
      {
        "role": "user",
        "content": "Скажи короткое приветствие на русском"
      }
    ]
  }' | jq
```

## Прямая проверка Analyzer

```bash
curl -s http://localhost:5001/api/v1/analyze \
  -H "Content-Type: application/json" \
  -d '{
    "text": "Мой телефон +79031234567 и ИНН 7707083893",
    "language": "ru"
  }' | jq
```

Ожидаемые entity types: `PHONE_NUMBER` и `RU_INN`.

## Фильтрация Analyzer по entity types

Analyzer API поддерживает стандартный Presidio-параметр `entities`. Regex recognizers и DeepPavlov NER соблюдают этот список одинаково: если запрошен только `RU_INN`, NER-типы `PERSON`, `LOCATION` и `ORGANIZATION` не вычисляются.

```bash
curl -s http://localhost:5001/api/v1/analyze \
  -H "Content-Type: application/json" \
  -d '{
    "text": "Иван Иванов из Москвы, ИНН 7707083893",
    "language": "ru",
    "entities": ["RU_INN"]
  }' | jq
```

Чтобы получить только NER-сущности, явно запросите соответствующие типы:

```bash
curl -s http://localhost:5001/api/v1/analyze \
  -H "Content-Type: application/json" \
  -d '{
    "text": "Иван Иванов работает в Газпроме",
    "language": "ru",
    "entities": ["PERSON", "ORGANIZATION"],
    "score_threshold": 0.7
  }' | jq
```

NER-результаты имеют фиксированный score `0.7`; при `score_threshold` выше `0.7` DeepPavlov NER не запускается.

## Health Checks

LiteLLM liveness endpoint не требует `LITELLM_MASTER_KEY` и используется для Docker healthcheck контейнера `ru-llm-proxy`:

```bash
curl -s http://localhost:4000/health/liveliness
```

`/health` у LiteLLM предназначен для проверки моделей и может делать реальные LLM API calls, поэтому для liveness/readiness лучше использовать специализированные endpoints.

## Health Analyzer

```bash
curl -s http://localhost:5001/api/v1/health | jq
```

NER status возвращается отдельно:

```json
{"status":"ok","ner":"loaded"}
```

Если модель не загрузилась, сервис продолжит работать для regex recognizers:

```json
{"status":"ok","ner":"not_loaded"}
```

## Guardrails

Список guardrails, зарегистрированных в LiteLLM:

```bash
curl -s "$API_URL/guardrails/list" \
  -H "Authorization: Bearer $LITELLM_MASTER_KEY" | jq
```

Live-запрос с явным `guardrails` parameter:

```bash
curl -s -D /tmp/ru-llm-proxy-headers "$API_URL/v1/chat/completions" \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $RU_LLM_PROXY_TOKEN" \
  -d '{
    "model": "zai-glm-5.1",
    "guardrails": ["ru-pii-mask-pre", "ru-pii-mask-post"],
    "messages": [
      {
        "role": "user",
        "content": "Клиент Иванов Иван, телефон +79031234567"
      }
    ],
    "max_tokens": 80
  }' | jq '.choices[0].message'

grep -i '^x-litellm-applied-guardrails:' /tmp/ru-llm-proxy-headers
```

То же самое через Makefile:

```bash
make guardrails-list
make guardrails-smoke
```

## Sticky routing

Если за моделью настроено несколько deployments, LiteLLM должен удерживать один клиентский ключ на одном healthy deployment. Для быстрой проверки:

```bash
make routing-smoke
```

Команда отправляет два live-запроса одним ключом и сравнивает header `x-litellm-model-id`.

Если хотите проверять не master key, а пользовательский virtual key, задайте его в `.env`:

```env
LITELLM_ROUTING_TEST_KEY=sk-...
```

Подробности настройки нескольких аккаунтов одной модели: [routing.md](routing.md).

## Metrics

LiteLLM и PII guardrail метрики доступны через Prometheus endpoint:

```bash
curl -s "$API_URL/metrics" | grep -E '^(litellm_|ru_pii_guardrail_)' | head
```

То же самое через Makefile:

```bash
make metrics
make monitor-smoke
```

PII guardrail метрики `ru_pii_guardrail_*` появятся после первого запроса, который прошёл через guardrail. Метрики не содержат raw PII или текст пользовательского запроса.

## Клиентские гайды

- Codex CLI / Codex App local tasks: [clients/codex.md](clients/codex.md)
- Claude Code: [clients/claude-code.md](clients/claude-code.md)
- OpenCode CLI / Desktop: [clients/opencode.md](clients/opencode.md)
- Kilo Code VS Code / CLI: [clients/kilo-code.md](clients/kilo-code.md)
- JWT/OIDC proxy auth: [clients/jwt.md](clients/jwt.md)

## Добавление моделей

По умолчанию настроены provider-prefixed aliases для Z.AI, OpenAI и Anthropic. Чтобы добавить ещё один провайдер, добавьте модель в `litellm-config.yaml`, добавьте нужный API key в `.env` и перезапустите LiteLLM:

```yaml
model_list:
  - model_name: my-openai-model
    litellm_params:
      model: openai/gpt-5.4-mini
      api_key: os.environ/OPENAI_API_KEY
    model_info:
      id: openai-gpt-4o-primary
      base_model: gpt-4o
```

```bash
make restart
```

Если это второй deployment той же публичной модели, оставьте прежний `model_name`, но задайте новый `model_info.id`. Так LiteLLM сможет корректно хранить sticky affinity.

## Streaming

LiteLLM может принимать streaming requests, но в проекте пока не реализовано streaming response restoration. Не используйте `stream: true` для сценариев, где восстановление PII в ответе обязательно.
