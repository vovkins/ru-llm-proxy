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

## Прямая проверка Anonymizer

Anonymizer service доступен для прямых вызовов. Reversible LiteLLM guardrail path его не использует.

```bash
curl -s http://localhost:5002/api/v1/anonymize \
  -H "Content-Type: application/json" \
  -d '{
    "text": "Мой телефон +79031234567",
    "entities": [
      {
        "entity_type": "PHONE_NUMBER",
        "start": 12,
        "end": 24,
        "score": 0.85
      }
    ],
    "operators": {
      "PHONE_NUMBER": "replace"
    }
  }' | jq
```

## Добавление моделей

По умолчанию настроена только модель `glm-5.1`. Чтобы использовать другого провайдера, добавьте модель в `litellm-config.yaml`, добавьте нужный API key в `.env` и перезапустите LiteLLM:

```yaml
model_list:
  - model_name: my-openai-model
    litellm_params:
      model: openai/gpt-4o
      api_key: os.environ/OPENAI_API_KEY
```

```bash
make restart
```

## Streaming

LiteLLM может принимать streaming requests, но в проекте пока не реализовано streaming response restoration. Не используйте `stream: true` для сценариев, где восстановление PII в ответе обязательно.
