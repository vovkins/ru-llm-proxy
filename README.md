# ru-llm-proxy

LLM-прокси для маскирования персональных данных (PII) в русскоязычных запросах.

Проект принимает OpenAI-совместимые chat completion запросы через LiteLLM, находит персональные данные в тексте, заменяет их на временные плейсхолдеры перед отправкой внешнему LLM-провайдеру и восстанавливает исходные значения в ответе, если модель вернула эти плейсхолдеры.

Текущее состояние: MVP для self-hosted оценки и дальнейшего hardening. В реализации используются LiteLLM Proxy, кастомные Presidio recognizers для российских форматов PII, опциональный DeepPavlov NER, Redis для обратимых маппингов и PostgreSQL для состояния LiteLLM.

## Возможности

| Область | Текущее поведение |
| --- | --- |
| Gateway API | LiteLLM OpenAI-compatible endpoint `/chat/completions` |
| Модель по умолчанию | `glm-5.1` через Z.ai-compatible OpenAI endpoint |
| Маскирование запроса | LiteLLM custom guardrail в режиме `pre_call` |
| Восстановление ответа | LiteLLM custom guardrail в режиме `post_call` |
| Формат плейсхолдеров | Уникальные в рамках запроса: `<PHONE_NUMBER_1>`, `<PHONE_NUMBER_2>`, `<RU_INN_1>` |
| Хранилище маппингов | Redis key `pii_mapping:<request_id>`, TTL по умолчанию 3600 секунд |
| Поведение при сбоях | `fail_open` по умолчанию, опционально `fail_closed` |
| Локальные тесты | Docker-based unit tests и deterministic guardrail-flow test |
| Live проверка | Smoke test против запущенных сервисов и реального LLM-провайдера |

## Поддерживаемые PII

| Тип данных | Entity | Метод |
| --- | --- | --- |
| Телефон | `PHONE_NUMBER` | Regex + валидация количества цифр |
| Email | `EMAIL_ADDRESS` | Regex |
| ИНН | `RU_INN` | Regex + checksum для 10/12 цифр |
| СНИЛС | `RU_SNILS` | Regex + checksum |
| Паспорт РФ | `RU_PASSPORT` | Regex + проверка региона |
| Банковская карта | `CREDIT_CARD` | Regex + Luhn |
| Адрес | `RU_ADDRESS` | Regex-паттерны российских адресов |
| ФИО | `PERSON` | DeepPavlov `ner_rus_bert`, если модель загружена |
| Организация | `ORGANIZATION` | DeepPavlov `ner_rus_bert`, если модель загружена |
| Локация | `LOCATION` | DeepPavlov `ner_rus_bert`, если модель загружена |

DeepPavlov NER соблюдает параметры Analyzer API: если в запросе указан `entities`, NER запускается только для `PERSON`, `ORGANIZATION` или `LOCATION`; если запрошены только regex-типы вроде `RU_INN`, NER пропускается. Так как DeepPavlov не возвращает per-entity confidence, проект присваивает NER-результатам фиксированный score `0.7` и не запускает NER при `score_threshold > 0.7`.

## Архитектура

```text
Client
  |
  v
LiteLLM Proxy (:4000)
  |
  | pre_call: RuPIIGuardrail
  v
Presidio Analyzer (:5001) -> regex recognizers + optional DeepPavlov NER
  |
  v
Redis mapping store
  |
  v
LLM provider
  |
  | post_call: RuPIIGuardrail
  v
Client receives restored response
```

В Docker Compose также запускается Presidio Anonymizer на порту `5002`. Текущий reversible guardrail не использует его для маскирования, потому что стабильное восстановление требует уникальных плейсхолдеров, которые формируются внутри guardrail. Сервис anonymizer остаётся доступен для прямых API-проверок и будущих интеграций.

Подробности: [docs/architecture.md](docs/architecture.md).

## Требования

| Зависимость | Примечание |
| --- | --- |
| Docker | 20.10+ |
| Docker Compose | v2 |
| RAM | 4 GB рекомендуется для сборки с DeepPavlov |
| Диск | 20 GB рекомендуется, spaCy и DeepPavlov модели скачиваются при build |
| Provider key | `ZAI_API_KEY` для модели `glm-5.1` |

## Быстрый старт

```bash
git clone https://github.com/vovkins/ru-llm-proxy.git
cd ru-llm-proxy

make setup
# Отредактируйте .env и задайте ZAI_API_KEY.

make build
make up
make health
```

Первая сборка может занять заметное время: Dockerfile скачивает DeepPavlov NER модель.

## Команды

| Команда | Назначение |
| --- | --- |
| `make setup` | Создать `.env` из шаблона и сгенерировать локальные ключи |
| `make build` | Собрать Docker images проекта |
| `make up` | Запустить сервисы и выполнить health check |
| `make down` | Остановить сервисы |
| `make restart` | Перезапустить LiteLLM после изменения конфигурации |
| `make logs` | Показать live-логи сервисов |
| `make health` | Проверить LiteLLM, Analyzer, Anonymizer, PostgreSQL и Redis |
| `make test` | Алиас для полного локального unit suite |
| `make test-unit` | Запустить recognizers/NER, guardrail unit tests и deterministic flow |
| `make test-recognizers` | Запустить unit-тесты recognizers и NER helpers |
| `make test-guardrail` | Запустить unit-тесты LiteLLM guardrail |
| `make test-flow` | Запустить deterministic guardrail-flow без внешнего LLM |
| `make test-e2e` | Запустить live smoke test против поднятых сервисов и LLM-провайдера |
| `make clean` | Удалить Docker volumes и локальные images проекта |

## Конфигурация

Секреты и локальные настройки хранятся в `.env`, который создаётся из [.env.example](.env.example).

| Переменная | Значение по умолчанию | Назначение |
| --- | --- | --- |
| `ZAI_API_KEY` | `***` | API key для настроенной модели `glm-5.1` |
| `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, `GOOGLE_API_KEY` | `***` | Опциональные ключи для дополнительных моделей |
| `LITELLM_MASTER_KEY` | генерируется `make setup` | Bearer token для LiteLLM |
| `LITELLM_SALT_KEY` | генерируется `make setup` | LiteLLM encryption salt |
| `LITELLM_DB_URL` | PostgreSQL service URL | Подключение LiteLLM к БД |
| `REDIS_URL` | `redis://redis:6379` | Хранилище PII маппингов |
| `PRESIDIO_ANALYZER_URL` | `http://presidio-analyzer:5001` | Analyzer endpoint для guardrail |
| `PII_GUARDRAIL_FAILURE_MODE` | `fail_open` | `fail_open` или `fail_closed` |
| `PII_MAPPING_TTL_SECONDS` | `3600` | TTL Redis-маппинга |
| `DEEPPAVLOV_NER_MODEL_URL` | DeepPavlov model URL | Build-time URL архива `ner_rus_bert` |
| `DEEPPAVLOV_NER_MODEL_SHA256` | empty | Optional build-time checksum архива модели |
| `DEEPPAVLOV_NER_DOWNLOAD_TIMEOUT_SECONDS` | `120` | Timeout скачивания архива модели |

Модели и guardrails LiteLLM описаны в [litellm-config.yaml](litellm-config.yaml):

```yaml
guardrails:
  - guardrail_name: "ru-pii-mask-pre"
    litellm_params:
      guardrail: litellm_guardrails.pii_guardrail.RuPIIGuardrail
      mode: "pre_call"
      default_on: true
  - guardrail_name: "ru-pii-mask-post"
    litellm_params:
      guardrail: litellm_guardrails.pii_guardrail.RuPIIGuardrail
      mode: "post_call"
      default_on: true
```

## Сборка и зависимости

Python-зависимости Docker-образов вынесены в requirements-файлы:

```text
presidio/requirements-analyzer.txt
presidio/requirements-torchcrf.txt
presidio/requirements-anonymizer.txt
tests/requirements-guardrails.txt
```

Это compatibility constraints, а не полный lockfile с hash-проверкой всех wheels. Они ограничивают major/minor линии зависимостей, используемых текущими Docker images, и делают Dockerfile проще для ревью.

Analyzer image во время `make build` скачивает:

- spaCy model `ru_core_news_sm`;
- DeepPavlov archive `ner_rus_bert_torch_new.tar.gz`.

`presidio/download_model.py` скачивает DeepPavlov archive атомарно, разрешает только `http`/`https` URLs, проверяет SHA-256 при заданном `DEEPPAVLOV_NER_MODEL_SHA256` и распаковывает tar только после проверки путей внутри архива. Если checksum не задан, скрипт печатает рассчитанный SHA-256; после доверенной сборки его можно перенести в `.env`.

## Пример запроса

```bash
export LITELLM_MASTER_KEY=$(grep '^LITELLM_MASTER_KEY=' .env | cut -d= -f2-)

curl http://localhost:4000/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $LITELLM_MASTER_KEY" \
  -d '{
    "model": "glm-5.1",
    "messages": [
      {
        "role": "user",
        "content": "Мой телефон +7 903 123 45 67, ИНН 7707083893"
      }
    ]
  }'
```

К провайдеру уходит текст с плейсхолдерами вроде `<PHONE_NUMBER_1>` и `<RU_INN_1>`. Если ответ провайдера содержит эти плейсхолдеры, post-call hook восстановит исходные значения перед возвратом клиенту.

Больше примеров: [docs/examples.md](docs/examples.md).

## Тестирование

Локальные тесты запускаются через Docker и не устанавливают Python-пакеты в локальное окружение хоста. Make targets передают Docker Compose флаг `--build`, поэтому отсутствующие test images собираются автоматически.

```bash
make test             # alias для make test-unit
make test-unit        # recognizers, NER helpers, guardrail unit tests, deterministic guardrail flow
make test-recognizers
make test-guardrail
make test-flow        # deterministic проверка без внешнего LLM
make test-e2e         # live smoke test; нужны make up и ZAI_API_KEY
```

`make test-flow` проверяет, что PII маскируется до simulated model call и восстанавливается после него. `make test-e2e` считается live smoke test, потому что реальный провайдер может опустить или переформулировать плейсхолдеры.

## Операционные замечания

- `fail_open` используется по умолчанию: при сбое Presidio или Redis запрос остаётся неизменённым, без частичного маскирования.
- `fail_closed` выбрасывает ошибку при сбое Presidio или Redis и не пропускает запрос дальше.
- Восстановление ответа возможно только для плейсхолдеров, которые провайдер вернул в ответе.
- Streaming-запросы могут проходить через LiteLLM, но streaming post-call iterator hook в проекте пока не реализован. Для сценариев, где восстановление PII обязательно, используйте non-streaming ответы.
- DeepPavlov NER опционален во время runtime. Если модель не загрузилась, regex recognizers продолжают работать, а `/api/v1/health` вернёт `ner: "not_loaded"`.
- Повторяющиеся NER-сущности с одинаковым текстом сопоставляются с исходной строкой последовательно, чтобы одинаковые имена получали корректные offsets.

## Структура репозитория

```text
ru-llm-proxy/
├── docker-compose.yml
├── litellm-config.yaml
├── Makefile
├── presidio/
│   ├── analyzer_server.py
│   ├── anonymizer_server.py
│   ├── requirements-*.txt
│   ├── recognizers/
│   ├── ner/
│   └── tests/
├── litellm_guardrails/
│   ├── pii_guardrail.py
│   └── tests/
├── tests/
│   ├── Dockerfile.guardrails
│   ├── requirements-guardrails.txt
│   └── e2e/
└── docs/
    ├── architecture.md
    ├── examples.md
    └── research.md
```

## Лицензия

MIT
