# Примеры API-запросов

Все запросы идут через LiteLLM Proxy (OpenAI-совместимый API).

## Настройка

```bash
# Установить мастер-ключ из .env
export LITELLM_MASTER_KEY=$(grep LITELLM_MASTER_KEY .env | cut -d= -f2)
export API_URL="http://localhost:4000"
```

## 1. Простой запрос (без PII)

```bash
curl -s $API_URL/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $LITELLM_MASTER_KEY" \
  -d '{
    "model": "gpt-4o-mini",
    "messages": [{"role": "user", "content": "Расскажи сказку"}]
  }' | jq '.choices[0].message.content'
```

Текст без PII проходит как есть — no masking.

## 2. Запрос с телефоном

```bash
curl -s $API_URL/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $LITELLM_MASTER_KEY" \
  -d '{
    "model": "gpt-4o-mini",
    "messages": [{"role": "user", "content": "Позвони мне на номер +7 903 123 45 67"}]
  }' | jq '.choices[0].message.content'
```

**Что происходит:**
- К LLM уходит: `"Позвони мне на номер <PHONE_NUMBER_1>"`
- Ответ LLM содержит плейсхолдер
- Клиент получает: `"Я не могу позвонить на номер +7 903 123 45 67..."`

## 3. Запрос с ИНН и СНИЛС

```bash
curl -s $API_URL/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $LITELLM_MASTER_KEY" \
  -d '{
    "model": "gpt-4o-mini",
    "messages": [{"role": "user", "content": "Мой ИНН 7707083893 и СНИЛС 112-233-445 95"}]
  }' | jq '.choices[0].message.content'
```

## 4. Запрос с ФИО (NER)

```bash
curl -s $API_URL/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $LITELLM_MASTER_KEY" \
  -d '{
    "model": "gpt-4o-mini",
    "messages": [{"role": "user", "content": "Иван Иванов из Москвы работает в Сбербанке"}]
  }' | jq '.choices[0].message.content'
```

**DeepPavlov NER обнаружит:** Иван Иванов → PERSON, Москвы → LOCATION, Сбербанке → ORGANIZATION

## 5. Комплексный запрос (много PII)

```bash
curl -s $API_URL/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $LITELLM_MASTER_KEY" \
  -d '{
    "model": "gpt-4o-mini",
    "messages": [{"role": "user", "content": "Клиент Петров Иван Сергеевич, телефон +7 903 123 45 67, email: petrov@mail.ru, ИНН 500100732259, проживает по адресу ул. Ленина, д. 10, кв. 5"}]
  }' | jq '.choices[0].message.content'
```

Все PII будут маскированы перед отправкой и восстановлены в ответе.

## 6. Разные модели

```bash
# GPT-4o
curl -s $API_URL/v1/chat/completions \
  -H "Authorization: Bearer $LITELLM_MASTER_KEY" \
  -H "Content-Type: application/json" \
  -d '{"model": "gpt-4o", "messages": [{"role": "user", "content": "Привет"}]}'

# Claude Sonnet 4
curl -s $API_URL/v1/chat/completions \
  -H "Authorization: Bearer $LITELLM_MASTER_KEY" \
  -H "Content-Type: application/json" \
  -d '{"model": "claude-sonnet-4-20250514", "messages": [{"role": "user", "content": "Привет"}]}'

# Gemini 2.5 Pro
curl -s $API_URL/v1/chat/completions \
  -H "Authorization: Bearer $LITELLM_MASTER_KEY" \
  -H "Content-Type: application/json" \
  -d '{"model": "gemini-2.5-pro", "messages": [{"role": "user", "content": "Привет"}]}'
```

## 7. Streaming

```bash
curl -s $API_URL/v1/chat/completions \
  -H "Authorization: Bearer $LITELLM_MASTER_KEY" \
  -H "Content-Type: application/json" \
  -d '{"model": "gpt-4o-mini", "messages": [{"role": "user", "content": "Привет, я Иван Иванов"}], "stream": true}'
```

## 8. Прямая проверка Presidio

```bash
# Analyzer — что найдено
curl -s http://localhost:5001/api/v1/analyze \
  -H "Content-Type: application/json" \
  -d '{"text": "Мой телефон +7 903 123 45 67 и ИНН 7707083893", "language": "ru"}' | jq

# Anonymizer — что замаскировано
curl -s http://localhost:5002/api/v1/anonymize \
  -H "Content-Type: application/json" \
  -d '{
    "text": "Мой телефон +7 903 123 45 67 и ИНН 7707083893",
    "entities": [
      {"entity_type": "PHONE_NUMBER", "start": 12, "end": 29, "score": 0.85},
      {"entity_type": "RU_INN", "start": 33, "end": 43, "score": 0.9}
    ],
    "operators": {"PHONE_NUMBER": "replace", "RU_INN": "replace"}
  }' | jq
```
