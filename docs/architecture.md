# Архитектура ru-llm-proxy

## Обзор

```
┌──────────┐     ┌──────────────┐     ┌─────────────────┐     ┌──────────────┐
│  Клиент   │────▶│ LiteLLM Proxy│────▶│ PII Guardrail   │────▶│ LLM Provider │
│ (приложение)│   │  (порт 4000) │     │ Presidio + NER  │     │ (OpenAI, etc)│
│           │◀────│              │◀────│ Mask / Unmask   │◀────│              │
└──────────┘     └──────────────┘     └─────────────────┘     └──────────────┘
                        │
                 ┌──────┴──────┐
                 │ PostgreSQL  │
                 │   + Redis   │
                 └─────────────┘
```

## Поток запроса (детально)

1. **Клиент** отправляет `POST /v1/chat/completions` с текстом, содержащим PII
2. **LiteLLM** перехватывает через `async_pre_call_hook` (RuPIIGuardrail)
3. Текст отправляется на **Presidio Analyzer** (`POST /api/v1/analyze`)
4. Analyzer запускает:
   - **Regex recognizers** — телефоны, email, ИНН, СНИЛС, паспорт, карты, адреса
   - **DeepPavlov NER** (ner_rus_bert) — имена, организации, локации
5. Результат + текст отправляются на **Presidio Anonymizer** (`POST /api/v1/anonymize`)
6. Anonymizer заменяет PII на плейсхолдеры: `+7 903 123 45 67` → `<PHONE_NUMBER>`
7. Маппинг `плейсхолдер → оригинал` сохраняется в **Redis** (TTL 1 час)
8. Обезличенный запрос отправляется к **LLM-провайдеру** (OpenAI/Anthropic/Google)
9. **LiteLLM** перехватывает ответ через `async_post_call_success_hook`
10. Плейсхолдеры в ответе заменяются на оригинальные данные из Redis
11. **Клиент** получает полный ответ с оригинальными PII

## Стек технологий

| Компонент | Версия | Примечание |
|-----------|--------|------------|
| LiteLLM Proxy | latest stable | Docker: `docker.litellm.ai/berriai/litellm:main-stable` |
| Python | 3.12 | Presidio recognizers + NER |
| Presidio Analyzer | 2.2.362+ | PII detection framework |
| Presidio Anonymizer | 2.2.362+ | PII masking/restoration |
| DeepPavlov | 1.0+ | NER model `ner_rus_bert` (PER, LOC, ORG) |
| spaCy | 3.8+ | NLP engine для Presidio (базовый русский) |
| PostgreSQL | 16-alpine | БД LiteLLM |
| Redis | 7-alpine | PII mapping cache + LiteLLM cache |
| Docker | 20.10+ | Контейнеризация |
| Docker Compose | v2 | Оркестрация |

## PII-детекция для русского языка

### Regex-recognizers (точные паттерны)

| Recognizer | Типы | Валидация |
|-----------|------|-----------|
| RuPhoneRecognizer | PHONE_NUMBER | Проверка количества цифр (10-11) |
| RuEmailRecognizer | EMAIL_ADDRESS | Стандартный email regex |
| RuInnRecognizer | RU_INN | Контрольная сумма (10 и 12 цифр) |
| RuSnilsRecognizer | RU_SNILS | Checksum (11 цифр) |
| RuPassportRecognizer | RU_PASSPORT | Валидация региона (01-99) |
| RuBankCardRecognizer | CREDIT_CARD | Luhn algorithm |
| RuAddressRecognizer | RU_ADDRESS | Паттерны ул./д./кв. |

### NER-модель (DeepPavlov ner_rus_bert)

| Entity | Presidio type | Пример |
|--------|--------------|--------|
| PER | PERSON | Иван Иванов |
| LOC | LOCATION | Москва |
| ORG | ORGANIZATION | Сбербанк |

## Дедупликация

Regex + NER могут найти одну и ту же сущность дважды. Анализатор дедуплицирует:
- Перекрывающиеся сущности — остаётся с более высоким score
- Сортировка по позиции в тексте

## Кастомный LiteLLM Guardrail

Файл: `litellm_guardrails/pii_guardrail.py`

- `async_pre_call_hook` — маскирование PII перед отправкой к LLM
- `async_post_call_success_hook` — восстановление PII в ответе
- Маппинг хранится в Redis (TTL 1 час, auto-cleanup)
- Fail-open: при ошибке Presidio запрос проходит без маскировки
