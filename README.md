# ru-llm-proxy 🛡️

LLM-прокси с санитайзером персональных данных (PII) для русского языка.

Обнаруживает и маскирует чувствительные данные в запросах к LLM перед отправкой провайдеру, а затем восстанавливает оригинальные данные в ответах, если модель вернула плейсхолдеры. Клиент работает с обычным OpenAI-compatible API, а внешний LLM получает обезличенный текст.

## Статус проекта

✅ **MVP работает** — в проекте есть LiteLLM Proxy, Presidio Analyzer, русские PII recognizers, DeepPavlov NER, Redis-маппинг для обратимого восстановления, sticky routing к provider deployments и Docker-based тесты.

⚠️ **Текущие ограничения** — streaming response restoration не реализован; восстановление возможно только для плейсхолдеров, которые провайдер вернул в ответе.

## Что маскируется

| Тип данных | Entity | Метод |
|-----------|--------|-------|
| Телефоны | `PHONE_NUMBER` | Regex + валидация количества цифр |
| Email | `EMAIL_ADDRESS` | Regex |
| ИНН | `RU_INN` | Regex + checksum для 10/12 цифр |
| СНИЛС | `RU_SNILS` | Regex + checksum |
| Паспорт РФ | `RU_PASSPORT` | Regex + проверка региона |
| Банковские карты | `CREDIT_CARD` | Regex + Luhn |
| Адреса | `RU_ADDRESS` | Regex-паттерны российских адресов |
| ФИО | `PERSON` | DeepPavlov `ner_rus_bert`, если модель загружена |
| Организации | `ORGANIZATION` | DeepPavlov `ner_rus_bert`, если модель загружена |
| Города/локации | `LOCATION` | DeepPavlov `ner_rus_bert`, если модель загружена |

DeepPavlov NER соблюдает параметры Analyzer API: если в запросе указан `entities`, NER запускается только для `PERSON`, `ORGANIZATION` или `LOCATION`; если запрошены только regex-типы вроде `RU_INN`, NER пропускается. Так как DeepPavlov не возвращает per-entity confidence, проект присваивает NER-результатам фиксированный score `0.7` и не запускает NER при `score_threshold > 0.7`.

## Архитектура

```text
┌──────────┐     ┌──────────────┐     ┌────────────────────┐     ┌──────────────┐
│  Клиент  │────▶│ LiteLLM Proxy│────▶│  PII Guardrail     │────▶│ LLM Provider │
│          │     │  порт 4000   │     │  mask / unmask     │     │   glm-5.1    │
│          │◀────│              │◀────│                    │◀────│              │
└──────────┘     └──────┬───────┘     └─────────┬──────────┘     └──────────────┘
                        │                       │
                        │                       ▼
                        │              ┌──────────────────┐
                        │              │ Presidio Analyzer│
                        │              │ regex + NER      │
                        │              └──────────────────┘
                        │
                 ┌──────▼──────┐
                 │ PostgreSQL  │
                 │   + Redis   │
                 └─────────────┘
```

### Компоненты

| Компонент | Service | Назначение |
|-----------|---------|------------|
| LiteLLM Proxy | `litellm` | OpenAI-compatible gateway, sticky routing к провайдеру, выполнение guardrails |
| PII Guardrail | `litellm_guardrails/pii_guardrail.py` | Маскирование запросов, Redis-маппинг, восстановление ответов |
| Presidio Analyzer | `presidio-analyzer` | Детекция PII через русские regex recognizers и DeepPavlov NER |
| Redis | `redis` | Временное хранение `placeholder -> original` маппингов и LiteLLM deployment affinity |
| PostgreSQL | `db` | Persistence для LiteLLM |

### Поток запроса

1. Клиент отправляет `POST /chat/completions`.
2. LiteLLM запускает `ru-pii-mask-pre` в режиме `pre_call`.
3. Guardrail отправляет каждое строковое сообщение в Presidio Analyzer.
4. Analyzer возвращает entity spans, entity types и scores.
5. Guardrail строит уникальные плейсхолдеры: `<PHONE_NUMBER_1>`, `<PHONE_NUMBER_2>`, `<RU_INN_1>`.
6. Guardrail сохраняет маппинг в Redis с TTL `PII_MAPPING_TTL_SECONDS`.
7. Только после успешного Redis save исходный message заменяется на masked text.
8. LiteLLM отправляет masked request LLM-провайдеру.
9. LiteLLM запускает `ru-pii-mask-post` в режиме `post_call`.
10. Guardrail восстанавливает `content` и `reasoning_content`, если ответ содержит плейсхолдеры.

Маскирование и восстановление выполняются внутри LiteLLM guardrail. Отдельный сервис анонимизации не используется в текущем request path и удалён из runtime-состава проекта.

## spaCy и DeepPavlov: кто за что отвечает

В проекте используются две разные NLP-модели, они не являются обёртками друг над другом.

| Модель | Где используется | Роль |
|--------|------------------|------|
| `ru_core_news_sm` | Presidio Analyzer / spaCy NLP engine | Токенизация и базовая языковая обработка для Presidio |
| DeepPavlov `ner_rus_bert` | `presidio/ner/deeppavlov_recognizer.py` | NER для `PERSON`, `LOCATION`, `ORGANIZATION` |

Regex recognizers отвечают за структурированные российские PII: телефоны, ИНН, СНИЛС, паспорта, карты, email и адреса. DeepPavlov добавляет NER-сущности поверх этого результата.

Подробности: [docs/architecture.md](docs/architecture.md).

## Требования к серверу

| Параметр | Минимум | Рекомендуется |
|----------|---------|---------------|
| Docker | 20.10+ | 24+ |
| Docker Compose | v2 | v2 |
| RAM | 2 GB | 4 GB+ |
| Диск | 10 GB | 20 GB+ |
| Provider key | `ZAI_API_KEY` | `ZAI_API_KEY`; опционально `ZAI_API_KEY_2` для второго deployment |

Первая сборка может занять заметное время: Dockerfile скачивает spaCy model `ru_core_news_sm` и DeepPavlov archive `ner_rus_bert_torch_new.tar.gz`.

## Быстрый старт

```bash
# 1. Клонировать
git clone https://github.com/vovkins/ru-llm-proxy.git
cd ru-llm-proxy

# 2. Настроить
make setup
# Заполните API-ключ провайдера в .env:
#   ZAI_API_KEY=your-key-here
#   ZAI_API_KEY_2=optional-second-account

# 3. Собрать и запустить
make build
make up

# 4. Проверить
make health
```

## Конфигурация

### .env — секреты и runtime-настройки

Все секреты хранятся в `.env`, который создаётся из [.env.example](.env.example).

```env
ZAI_API_KEY=your-zai-key
ZAI_API_KEY_2=optional-second-zai-key
LITELLM_MASTER_KEY=sk-ru-...    # автогенерируется через make setup
LITELLM_SALT_KEY=...            # автогенерируется через make setup
LITELLM_ROUTING_TEST_KEY=...     # опциональный virtual key для make routing-smoke
UI_USERNAME=admin               # автогенерируется через make setup
UI_PASSWORD=...                 # автогенерируется через make setup
POSTGRES_PASSWORD=...           # автогенерируется через make setup
LITELLM_DB_URL=postgresql://litellm:...@db:5432/litellm
REDIS_URL=redis://redis:6379
PRESIDIO_ANALYZER_URL=http://presidio-analyzer:5001
PII_GUARDRAIL_MODE=mask
PII_GUARDRAIL_FAILURE_MODE=fail_open
PII_MAPPING_TTL_SECONDS=3600
```

`make setup` не перезаписывает уже заданные реальные секреты. Если `.env` уже существует, команда добавит отсутствующие `UI_USERNAME` / `UI_PASSWORD`, опциональные routing-переменные и заменит только placeholder-значения.

Build-time переменные для DeepPavlov:

```env
DEEPPAVLOV_NER_MODEL_URL=http://files.deeppavlov.ai/v1/ner/ner_rus_bert_torch_new.tar.gz
DEEPPAVLOV_NER_MODEL_SHA256=
DEEPPAVLOV_NER_DOWNLOAD_TIMEOUT_SECONDS=120
```

`DEEPPAVLOV_NER_MODEL_SHA256` опционален, но для воспроизводимой и более строгой сборки его стоит заполнить после доверенной загрузки архива.

### PII policy mode

`PII_GUARDRAIL_MODE` управляет штатным поведением после успешной детекции PII:

| Значение | Поведение |
| --- | --- |
| `mask` | Значение по умолчанию. Guardrail маскирует PII, сохраняет Redis mapping и отправляет masked request провайдеру. |
| `block` | Guardrail отклоняет запрос с найденной PII на pre-call этапе. Провайдер не вызывается, Redis mapping не создаётся. |

В block mode клиент получает безопасную `422` ошибку с entity types, но без raw PII, offsets или текста запроса.

`PII_GUARDRAIL_FAILURE_MODE` остаётся отдельной настройкой для инфраструктурных сбоев Presidio/Redis: `fail_open` пропускает запрос дальше, `fail_closed` останавливает его.

### litellm-config.yaml — настройки LiteLLM

Монтируется через volume — можно менять без пересборки.

```yaml
model_list:
  - model_name: glm-5.1
    litellm_params:
      model: openai/glm-5.1
      api_base: https://api.z.ai/api/coding/paas/v4
      api_key: os.environ/ZAI_API_KEY
    model_info:
      id: z-ai-glm-5-1-primary
      base_model: glm-5.1

router_settings:
  redis_url: os.environ/REDIS_URL
  routing_strategy: simple-shuffle
  optional_pre_call_checks:
    - deployment_affinity
  deployment_affinity_ttl_seconds: 86400

guardrails:
  - guardrail_name: "ru-pii-mask-pre"
    litellm_params:
      guardrail: litellm_guardrails.pii_guardrail.RuPIIGuardrail
      mode: "pre_call"
      default_on: true
    guardrail_info:
      description: "Masks Russian PII before the provider request."
      params:
        - name: "stage"
          type: "string"
          description: "pre_call; masks Russian PII before the provider request."
        - name: "policy_mode"
          type: "string"
          description: "PII_GUARDRAIL_MODE: mask preserves reversible masking, block rejects detected PII before provider calls."
  - guardrail_name: "ru-pii-mask-post"
    litellm_params:
      guardrail: litellm_guardrails.pii_guardrail.RuPIIGuardrail
      mode: "post_call"
      default_on: true
    guardrail_info:
      description: "Restores request-scoped placeholders in model responses."
      params:
        - name: "stage"
          type: "string"
          description: "post_call; restores placeholders in model responses."

litellm_settings:
  callbacks:
    - prometheus
  drop_params: true
```

### Добавление другого провайдера

Любой провайдер, поддерживаемый LiteLLM, добавляется через `model_list`:

```yaml
model_list:
  - model_name: my-openai-model
    litellm_params:
      model: openai/gpt-4o
      api_key: os.environ/OPENAI_API_KEY
```

После правки:

```bash
make restart
```

### Sticky routing к provider deployments

Включён LiteLLM `deployment_affinity`: запросы с одним и тем же клиентским LiteLLM key закрепляются за одним healthy deployment внутри model group. Это помогает использовать provider-side кэширование входных токенов, когда для одной модели настроено несколько аккаунтов или провайдеров.

Для корректной работы у каждого deployment должен быть стабильный `model_info.id`. Если добавляете второй аккаунт Z.AI, используйте тот же `model_name: glm-5.1`, другой `api_key` и новый `model_info.id`.

Проверка:

```bash
make routing-smoke
```

Команда делает два live-запроса с одним ключом и сравнивает header `x-litellm-model-id`. Подробности и пример добавления второго deployment: [docs/routing.md](docs/routing.md).

## Make-команды

| Команда | Описание |
|---------|----------|
| `make setup` | Создать `.env`, сгенерировать ключи |
| `make build` | Собрать Docker-образы |
| `make up` | Запустить все сервисы и выполнить health check |
| `make down` | Остановить сервисы |
| `make restart` | Рестарт LiteLLM после изменения конфигурации |
| `make logs` | Логи всех сервисов |
| `make health` | Проверить LiteLLM, Analyzer, PostgreSQL и Redis |
| `make test` | Алиас для `make test-unit` |
| `make test-unit` | Recognizers/NER, guardrail unit tests и deterministic flow |
| `make test-recognizers` | Unit-тесты recognizers и NER helpers |
| `make test-guardrail` | Unit-тесты LiteLLM guardrail |
| `make test-flow` | Deterministic проверка mask/unmask без внешнего LLM |
| `make test-e2e` | Live smoke test против поднятых сервисов и реального LLM |
| `make guardrails-list` | Показать guardrails, зарегистрированные в LiteLLM |
| `make guardrails-smoke` | Live smoke с явным `guardrails` parameter и проверкой response headers |
| `make routing-smoke` | Live smoke sticky routing: один ключ должен попасть в один deployment |
| `make metrics` | Показать первые строки LiteLLM `/metrics` |
| `make monitor-smoke` | Проверить health, guardrails list и `/metrics` |
| `make update-litellm` | Подтянуть новый LiteLLM image и пересоздать только proxy container |
| `make clean` | Удалить volumes и локальные images проекта |

## Admin UI

LiteLLM Admin UI доступен по адресу:

```text
http://localhost:4000/ui
```

Для входа используются `UI_USERNAME` и `UI_PASSWORD` из `.env`. Это отдельные credentials для UI; `LITELLM_MASTER_KEY` остаётся admin API key и не должен выдаваться пользователям.

Через UI можно создавать virtual keys для пользователей, смотреть usage/spend и управлять ключами. Пользователям выдавайте virtual keys, а не `LITELLM_MASTER_KEY`.

## Примеры использования

### Базовый запрос

```bash
export LITELLM_MASTER_KEY=$(grep '^LITELLM_MASTER_KEY=' .env | cut -d= -f2-)

curl http://localhost:4000/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $LITELLM_MASTER_KEY" \
  -d '{
    "model": "glm-5.1",
    "messages": [{"role": "user", "content": "Привет!"}]
  }'
```

### Запрос с PII

```bash
curl http://localhost:4000/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $LITELLM_MASTER_KEY" \
  -d '{
    "model": "glm-5.1",
    "messages": [
      {
        "role": "user",
        "content": "Мой телефон +79031234567, ИНН 7707083893"
      }
    ]
  }'
```

К провайдеру уйдёт текст вида:

```text
Мой телефон <PHONE_NUMBER_1>, ИНН <RU_INN_1>
```

Если ответ провайдера содержит эти плейсхолдеры, post-call hook восстановит исходные значения перед возвратом клиенту.

Больше примеров: [docs/examples.md](docs/examples.md).

## Guardrails UI

Guardrails зарегистрированы в `litellm-config.yaml` и имеют `guardrail_info`, чтобы LiteLLM мог отдавать metadata через `GET /guardrails/list`.

```bash
make guardrails-list
```

Для smoke-проверки применения guardrails к live-запросу:

```bash
make guardrails-smoke
```

LiteLLM UI может показывать список guardrails, но не обязан отображать все произвольные поля `guardrail_info`. Для production monitoring используйте `/metrics`, health checks и structured logs.

## Monitoring

Prometheus включён через `litellm_settings.callbacks: ["prometheus"]`. Метрики доступны на:

```text
http://localhost:4000/metrics
```

Быстрая проверка:

```bash
make metrics
make monitor-smoke
```

Проект добавляет собственные PII guardrail метрики:

- `ru_pii_guardrail_pre_calls_total`
- `ru_pii_guardrail_post_calls_total`
- `ru_pii_guardrail_entities_detected_total`
- `ru_pii_guardrail_blocked_total`
- `ru_pii_guardrail_fail_open_total`
- `ru_pii_guardrail_fail_closed_total`
- `ru_pii_guardrail_analyzer_latency_seconds_*`
- `ru_pii_guardrail_redis_latency_seconds_*`
- `ru_pii_guardrail_mapping_size_*`

Guardrail также пишет structured JSON logs без prompt text и без raw PII. Подробный DevOps guide: [docs/monitoring.md](docs/monitoring.md).

Для routing диагностики используйте `make routing-smoke`, response header `x-litellm-model-id` и LiteLLM deployment metrics (`litellm_deployment_*`). Подробности: [docs/routing.md](docs/routing.md).

## Обновление LiteLLM

LiteLLM запускается из готового image `docker.litellm.ai/berriai/litellm:main-stable`, поэтому для обновления proxy не нужно пересобирать весь проект:

```bash
make update-litellm
```

Команда выполняет `docker compose pull litellm` и пересоздаёт только контейнер `litellm`. В production после staging-проверки лучше закреплять конкретный tag или digest LiteLLM image. Подробный update checklist: [docs/monitoring.md](docs/monitoring.md#обновление-litellm).

## Healthcheck

Docker healthcheck для `ru-llm-proxy` использует LiteLLM endpoint `/health/liveliness`. Это unauthenticated liveness probe, он не делает LLM API calls и не требует `LITELLM_MASTER_KEY`.

```bash
curl http://localhost:4000/health/liveliness
```

`/health` в LiteLLM предназначен для проверки моделей и может требовать авторизацию, поэтому он не используется как container healthcheck.

См. официальную документацию LiteLLM: https://docs.litellm.ai/docs/proxy/health

## Тестирование

Локальные тесты запускаются через Docker и не устанавливают Python-пакеты в локальное окружение хоста.

```bash
make test             # alias для make test-unit
make test-unit        # recognizers, NER helpers, guardrail unit tests, deterministic flow
make test-recognizers
make test-guardrail
make test-flow        # deterministic проверка без внешнего LLM
make test-e2e         # live smoke test; нужны make up и ZAI_API_KEY
make routing-smoke    # live sticky routing smoke; нужны make up и provider key
```

`make test-flow` проверяет, что PII маскируется до simulated model call и восстанавливается после него. `make test-e2e` остаётся live smoke test: реальный провайдер может опустить или переформулировать плейсхолдеры.

## Troubleshooting

**LiteLLM container unhealthy:**

```bash
docker inspect ru-llm-proxy --format '{{json .State.Health}}' | jq
curl http://localhost:4000/health/liveliness
```

**Presidio Analyzer не детектирует PII:**

```bash
curl http://localhost:5001/api/v1/analyze \
  -H "Content-Type: application/json" \
  -d '{"text": "Мой телефон +79031234567", "language": "ru"}' | jq
```

**DeepPavlov модель не загружена:**

```bash
curl http://localhost:5001/api/v1/health | jq
# {"status": "ok", "ner": "loaded"}      — NER доступен
# {"status": "ok", "ner": "not_loaded"}  — regex recognizers продолжают работать
```

**GLM-5.1 возвращает пустой `content`:**

- coding plan может вернуть ответ в `reasoning_content`;
- guardrail восстанавливает плейсхолдеры и в `content`, и в `reasoning_content`.

**Запросы с PII возвращают `422`:**

- проверьте `PII_GUARDRAIL_MODE`;
- при `block` это ожидаемое policy behavior: запрос остановлен до вызова провайдера;
- при `mask` такое поведение не должно происходить из-за найденной PII, смотрите structured logs guardrail.

## Структура проекта

```text
ru-llm-proxy/
├── docker-compose.yml
├── litellm-config.yaml
├── Makefile
├── scripts/
│   └── setup_env.sh
├── presidio/
│   ├── Dockerfile
│   ├── analyzer_server.py
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
    ├── monitoring.md
    ├── routing.md
    └── research.md
```

## Лицензия

MIT
