# ru-llm-proxy 🛡️

LLM-прокси с санитайзером персональных данных (PII) для русского языка.

Обнаруживает и маскирует чувствительные данные в запросах к LLM перед отправкой провайдеру, и восстанавливает оригинальные данные в ответах. Клиент получает полный ответ, но к внешнему LLM уходят только обезличенные тексты.

## Что маскируется

| Тип данных | Метод | Пример |
|-----------|-------|--------|
| Телефоны | Regex + валидация | +7 903 123-45-67 → `[PHONE_NUMBER]` |
| Email | Regex | test@yandex.ru → `[EMAIL_ADDRESS]` |
| ИНН | Regex + контрольная сумма | 7707083893 → `[RU_INN]` |
| СНИЛС | Regex + checksum | 112-233-445 95 → `[RU_SNILS]` |
| Паспорт | Regex + регион | 45 10 123456 → `[RU_PASSPORT]` |
| Банк. карты | Regex + Luhn | 4111 1111 1111 1111 → `[CREDIT_CARD]` |
| Адреса | Regex-паттерны | ул. Ленина, д. 10 → `[RU_ADDRESS]` |
| ФИО | DeepPavlov NER | Иван Иванов → `[PERSON]` |
| Организации | DeepPavlov NER | Сбербанк → `[ORGANIZATION]` |
| Города/локации | DeepPavlov NER | Москва → `[LOCATION]` |

## Архитектура

```
┌──────────┐     ┌──────────────┐     ┌─────────────────┐     ┌──────────────┐
│  Клиент   │────▶│ LiteLLM Proxy│────▶│ PII Guardrail   │────▶│ LLM Provider │
│           │     │  (порт 4000) │     │ Presidio+NER    │     │ (OpenAI, etc)│
│           │     │              │     │ Mask on request  │     │              │
│           │◀────│              │◀────│ Unmask on resp.  │◀────│              │
└──────────┘     └──────────────┘     └─────────────────┘     └──────────────┘
                        │
                 ┌──────┴──────┐
                 │ PostgreSQL  │
                 │   + Redis   │
                 └─────────────┘
```

### Компоненты

| Компонент | Образ | Назначение |
|-----------|-------|------------|
| LiteLLM | `docker.litellm.ai/berriai/litellm:main-stable` | Агрегация LLM, роутинг, guardrails |
| Presidio Analyzer | Custom (Python 3.12) | Детекция PII (regex + NER) |
| Presidio Anonymizer | Custom (Python 3.12) | Маскирование/восстановление |
| DeepPavlov | Внутри Analyzer | NER модель `ner_rus_bert` (PER, LOC, ORG) |
| PostgreSQL | `postgres:16-alpine` | Данные LiteLLM |
| Redis | `redis:7-alpine` | Кэш, маппинг PII |

### Поток запроса

1. Клиент отправляет `POST /v1/chat/completions` с текстом
2. LiteLLM guardrail перехватывает запрос (pre-call)
3. Presidio Analyzer находит PII (regex + DeepPavlov NER)
4. Presidio Anonymizer заменяет PII на плейсхолдеры
5. Маппинг «плейсхолдер → оригинал» сохраняется в Redis
6. Обезличенный запрос отправляется к LLM-провайдеру
7. Guardrail перехватывает ответ (post-call)
8. Плейсхолдеры заменяются на оригинальные данные
9. Клиент получает полный ответ

## Требования к серверу

| Параметр | Минимум | Рекомендуется |
|----------|---------|---------------|
| CPU | 1 vCPU | 2-4 vCPU |
| RAM | 2 GB | 4 GB |
| Диск | 10 GB | 20 GB |
| Docker | 20.10+ | 24+ |
| Docker Compose | v2 | v2 |

## Быстрый старт

```bash
# 1. Клонировать
git clone https://github.com/vovkins/ru-llm-proxy.git
cd ru-llm-proxy

# 2. Настроить
make setup
# Заполните API-ключи в .env:
#   OPENAI_API_KEY=sk-...
#   ANTHROPIC_API_KEY=sk-ant-...
#   GOOGLE_API_KEY=AI...

# 3. Запустить
make build
make up
```

## Конфигурация

### .env — секреты

Все секреты хранятся в `.env` (не коммитится):

```env
OPENAI_API_KEY=sk-...
ANTHROPIC_API_KEY=sk-ant-...
GOOGLE_API_KEY=AI...
LITELLM_MASTER_KEY=sk-...     # автогенерируется при make setup
LITELLM_SALT_KEY=...           # автогенерируется при make setup
POSTGRES_PASSWORD=...          # автогенерируется при make setup
```

### litellm-config.yaml — настройки LiteLLM

Монтируется через volume — можно менять без пересборки:

```yaml
# Добавить нового провайдера:
model_list:
  - model_name: my-model
    litellm_params:
      model: openai/my-model
      api_key: os.environ/MY_API_KEY

# После правки — рестарт:
make restart
```

### Поддерживаемые провайдеры

Любые из 100+ провайдеров LiteLLM: OpenAI, Anthropic, Google, Azure, AWS Bedrock, Groq, DeepSeek, Mistral, xAI и другие.

## Make-команды

| Команда | Описание |
|---------|----------|
| `make setup` | Создать `.env`, сгенерировать ключи |
| `make build` | Собрать Docker-образы |
| `make up` | Запустить все сервисы |
| `make down` | Остановить |
| `make restart` | Рестарт LiteLLM (применить новый конфиг) |
| `make logs` | Логи всех сервисов (live) |
| `make test` | Запустить тесты recognizers |
| `make health` | Проверить статус сервисов |
| `make clean` | Удалить volumes и образы |

## Примеры использования

### Базовый запрос

```bash
curl http://localhost:4000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $LITELLM_MASTER_KEY" \
  -d '{
    "model": "gpt-4o-mini",
    "messages": [{"role": "user", "content": "Привет!"}]
  }'
```

### Запрос с PII

```bash
# Клиент отправляет:
curl http://localhost:4000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $LITELLM_MASTER_KEY" \
  -d '{
    "model": "gpt-4o-mini",
    "messages": [{"role": "user", "content": "Мой телефон +7 903 123 45 67, ИНН 7707083893"}]
  }'

# К LLM уходит: "Мой телефон [PHONE_NUMBER], ИНН [RU_INN]"
# Клиент получает ответ с оригинальными данными
```

### Выбор модели

```bash
# GPT-4o
{"model": "gpt-4o", ...}

# Claude Sonnet
{"model": "claude-sonnet-4-20250514", ...}

# Gemini
{"model": "gemini-2.5-pro", ...}
```

### Fallback

Если OpenAI недоступен — запрос автоматически пойдёт к Claude, затем к Gemini:

```yaml
# litellm-config.yaml
fallbacks:
  - gpt-4o: ["claude-sonnet-4-20250514", "gemini-2.5-pro"]
```

## Добавление нового провайдера

1. Добавить API-ключ в `.env`
2. Добавить модель в `litellm-config.yaml`
3. `make restart`

## Troubleshooting

**LiteLLM не стартует:**
```bash
make logs  # смотреть логи
# Проверить .env — все ключи заполнены?
```

**Presidio не детектирует PII:**
```bash
# Проверить напрямую:
curl http://localhost:5001/api/v1/analyze \
  -d '{"text": "Мой телефон +7 903 123 45 67", "language": "ru"}'
```

**DeepPavlov модель не загружена:**
```bash
# Healthcheck покажет:
curl http://localhost:5001/api/v1/health
# {"status": "ok", "ner": "loaded"} или {"ner": "not_loaded"}
```

**Docker-образ слишком большой:**
- Presidio + DeepPavlov: ~3 GB (нормально, модель 700 MB + PyTorch)

## Лицензия

MIT
