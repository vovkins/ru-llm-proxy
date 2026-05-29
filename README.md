# ru-llm-proxy 🛡️

LLM-прокси с санитайзером персональных данных (PII) для русского языка.

Обнаруживает и маскирует чувствительные данные в запросах к LLM перед отправкой провайдеру, и восстанавливает оригинальные данные в ответах. Клиент получает полный ответ, но к внешнему LLM уходят только обезличенные тексты.

## Статус проекта

✅ **MVP работает** — все компоненты запущены, PII маскируется, E2E тесты проходят.

## Что маскируется

| Тип данных | Метод | Пример |
|-----------|-------|--------|
| Телефоны | Regex + валидация | +7 903 123-45-67 → `<PHONE_NUMBER>` |
| Email | Regex | test@yandex.ru → `<EMAIL_ADDRESS>` |
| ИНН | Regex + контрольная сумма | 7707083893 → `<RU_INN>` |
| СНИЛС | Regex + checksum | 112-233-445 95 → `<RU_SNILS>` |
| Паспорт | Regex + регион | 45 10 123456 → `<RU_PASSPORT>` |
| Банк. карты | Regex + Luhn | 4111 1111 1111 1111 → `<CREDIT_CARD>` |
| Адреса | Regex-паттерны | ул. Ленина, д. 10 → `<RU_ADDRESS>` |
| ФИО | DeepPavlov NER | Иван Иванов → `<PERSON>` |
| Организации | DeepPavlov NER | Сбербанк → `<ORGANIZATION>` |
| Города/локации | DeepPavlov NER | Москва → `<LOCATION>` |

## Архитектура

```
┌──────────┐     ┌──────────────┐     ┌─────────────────┐     ┌──────────────┐
│  Клиент   │────▶│ LiteLLM Proxy│────▶│ PII Guardrail   │────▶│ LLM Provider │
│           │     │  (порт 4000) │     │ Presidio+NER    │     │  (GLM-5.1)   │
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
| Presidio Analyzer | Custom (Python 3.11) | Детекция PII (regex + NER) |
| Presidio Anonymizer | Custom (Python 3.11) | Маскирование/восстановление |
| DeepPavlov | Внутри Analyzer | NER модель `ner_rus_bert` (PER, LOC, ORG) |
| PostgreSQL | `postgres:16-alpine` | Данные LiteLLM |
| Redis | `redis:7-alpine` | Кэш, маппинг PII |

### Поток запроса

1. Клиент отправляет `POST /chat/completions` с текстом
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
# Заполните API-ключ в .env:
#   ZAI_API_KEY=your-key-here

# 3. Запустить
make build
make up

# 4. Проверить
make health
make test-e2e
```

## Конфигурация

### .env — секреты

Все секреты хранятся в `.env` (не коммитится):

```env
ZAI_API_KEY=your-zai-key          # Z.ai API ключ (GLM-5.1)
LITELLM_MASTER_KEY=sk-...         # автогенерируется при make setup
LITELLM_SALT_KEY=...              # автогенерируется при make setup
POSTGRES_PASSWORD=...             # автогенерируется при make setup
```

### litellm-config.yaml — настройки LiteLLM

Монтируется через volume — можно менять без пересборки. Текущая конфигурация:

```yaml
model_list:
  - model_name: glm-5.1
    litellm_params:
      model: openai/glm-5.1
      api_base: https://api.z.ai/api/coding/paas/v4  # Z.ai coding plan endpoint
      api_key: os.environ/ZAI_API_KEY

guardrails:
  - guardrail_name: "ru-pii-mask"
    litellm_params:
      guardrail: litellm_guardrails.pii_guardrail.RuPIIGuardrail
      mode: "pre_call"
      default_on: true
```

### Добавление другого провайдера

Любой из 100+ провайдеров LiteLLM (OpenAI, Anthropic, Google, Azure, AWS Bedrock, Groq, DeepSeek и т.д.):

```yaml
model_list:
  - model_name: my-model
    litellm_params:
      model: openai/gpt-4o
      api_key: os.environ/MY_API_KEY
```

После правки — `make restart`.

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
| `make test-unit` | Запустить все unit-тесты |
| `make test-e2e` | End-to-end тест (нужны запущенные сервисы) |
| `make health` | Проверить статус сервисов |
| `make clean` | Удалить volumes и образы |

## Примеры использования

### Базовый запрос

```bash
curl http://localhost:4000/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $LITELLM_MASTER_KEY" \
  -d '{
    "model": "glm-5.1",
    "messages": [{"role": "user", "content": "Привет!"}]
  }'
```

### Запрос с PII (автоматическое маскирование)

```bash
# Клиент отправляет:
curl http://localhost:4000/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $LITELLM_MASTER_KEY" \
  -d '{
    "model": "glm-5.1",
    "messages": [{"role": "user", "content": "Мой телефон +7 903 123 45 67, ИНН 7707083893"}]
  }'

# К LLM уходит: "Мой телефон <PHONE_NUMBER>, ИНН <RU_INN>"
# Клиент получает ответ с оригинальными данными восстановленными
```

## Тестирование

### Unit-тесты

```bash
make test       # Recognizers + NER (17 + 8 тестов)
make test-unit  # Все unit-тесты включая guardrail
```

### End-to-end тесты

```bash
make test-e2e   # Требует запущенных сервисов
                # Проверяет: health → PII detection → LLM call → guardrail masking
```

## Troubleshooting

**LiteLLM не стартует:**
```bash
make logs  # смотреть логи
# Проверить .env — ZAI_API_KEY заполнен?
```

**Presidio не детектирует PII:**
```bash
curl http://localhost:5001/api/v1/analyze \
  -H "Content-Type: application/json" \
  -d '{"text": "Мой телефон +7 903 123 45 67", "language": "ru"}'
```

**DeepPavlov модель не загружена:**
```bash
curl http://localhost:5001/api/v1/health
# {"status": "ok", "ner": "loaded"} — нормально
# {"status": "ok", "ner": "not_loaded"} — NER недоступен (regex-recognizers работают)
```

**GLM-5.1 возвращает пустой content:**
- Coding plan использует reasoning mode — ответ в `reasoning_content`
- Guardrail автоматически демаскирует оба поля

## Структура проекта

```
ru-llm-proxy/
├── docker-compose.yml          # 5 сервисов
├── litellm-config.yaml         # LiteLLM конфигурация
├── presidio/
│   ├── Dockerfile              # Multi-stage: analyzer + anonymizer
│   ├── analyzer_server.py      # Presidio Analyzer REST API
│   ├── anonymizer_server.py    # Presidio Anonymizer REST API
│   ├── recognizers/            # 8 русских PII recognizers
│   ├── ner/                    # DeepPavlov NER интеграция
│   └── tests/                  # Unit-тесты (recognizers + NER)
├── litellm_guardrails/
│   ├── pii_guardrail.py        # PII маскирование/демаскирование
│   └── tests/                  # Unit-тесты guardrail
├── tests/
│   └── e2e/                    # End-to-end тесты
├── Makefile                    # Команды управления
├── RESEARCH.md                 # Исследование и обоснование выбора стека
└── README.md
```

## Лицензия

MIT
