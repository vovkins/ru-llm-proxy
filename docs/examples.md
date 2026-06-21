# Примеры API

Все примеры соответствуют текущей конфигурации репозитория: LiteLLM на `localhost:4000`, модель `glm-5.1`.

## Окружение

```bash
export API_URL="http://localhost:4000"
export LITELLM_MASTER_KEY=$(grep '^LITELLM_MASTER_KEY=' .env | cut -d= -f2-)
```

## Chat completion без PII

```bash
curl -s "$API_URL/chat/completions" \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $LITELLM_MASTER_KEY" \
  -d '{
    "model": "glm-5.1",
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
curl -s "$API_URL/chat/completions" \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $LITELLM_MASTER_KEY" \
  -d '{
    "model": "glm-5.1",
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
curl -s "$API_URL/chat/completions" \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $LITELLM_MASTER_KEY" \
  -d '{
    "model": "glm-5.1",
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
curl -s -D /tmp/ru-llm-proxy-headers "$API_URL/chat/completions" \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $LITELLM_MASTER_KEY" \
  -d '{
    "model": "glm-5.1",
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

## PII block mode

По умолчанию guardrail работает в reversible masking mode:

```env
PII_GUARDRAIL_MODE=mask
```

Чтобы отклонять запросы с найденной PII до вызова провайдера, задайте block mode и перезапустите LiteLLM:

```env
PII_GUARDRAIL_MODE=block
```

```bash
make restart
```

В block mode запрос с PII возвращает `422` и безопасный error body. Ответ содержит только типы сущностей:

```json
{
  "error": {
    "message": "Request contains personal data and was blocked by PII policy.",
    "type": "pii_detected",
    "code": "pii_blocked",
    "details": {
      "entities": ["PHONE_NUMBER"]
    }
  }
}
```

Raw PII, offsets и исходный текст в error body не возвращаются. Clean-запросы продолжают идти к провайдеру.

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

## Добавление моделей

По умолчанию настроена только модель `glm-5.1`. Чтобы использовать другого провайдера, добавьте модель в `litellm-config.yaml`, добавьте нужный API key в `.env` и перезапустите LiteLLM:

```yaml
model_list:
  - model_name: my-openai-model
    litellm_params:
      model: openai/gpt-4o
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
