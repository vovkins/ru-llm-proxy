# ru-llm-proxy 🛡️

LLM-прокси с санитайзером персональных данных (PII) для русского языка.

Обнаруживает и маскирует чувствительные данные в запросах к LLM перед отправкой провайдеру, а затем восстанавливает оригинальные данные в ответах, если модель вернула плейсхолдеры. Клиенты работают через OpenAI-compatible и Anthropic-compatible API, а внешний LLM получает обезличенный текст.

## Статус проекта

✅ **MVP работает** — в проекте есть LiteLLM Proxy, Presidio Analyzer, русские PII recognizers, DeepPavlov NER, Redis-маппинг для обратимого восстановления, sticky routing к provider deployments и Docker-based тесты.

⚠️ **Текущие ограничения** — восстановление возможно только для плейсхолдеров, которые провайдер вернул в ответе. Streaming restoration поддерживает текстовые deltas (`content`, `reasoning_content`); streaming tool/function-call argument deltas пока не переписываются.

## Что маскируется

| Тип данных | Entity | Метод |
|-----------|--------|-------|
| Телефоны | `PHONE_NUMBER` | Regex + валидация количества цифр |
| Email | `EMAIL_ADDRESS` | Regex |
| ИНН | `RU_INN` | Regex + checksum для 10/12 цифр; bare INN включён по умолчанию |
| СНИЛС | `RU_SNILS` | Regex + checksum |
| Паспорт РФ | `RU_PASSPORT` | Regex + проверка региона |
| Банковские карты | `CREDIT_CARD` | Regex + Luhn |
| Адреса | `RU_ADDRESS` | Ограниченный regex corpus российских адресов |
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

1. Клиент отправляет запрос в LiteLLM: `POST /v1/chat/completions`, `POST /v1/responses` или `POST /v1/messages`.
2. LiteLLM запускает `ru-pii-mask-pre` в режиме `pre_call`.
3. Guardrail отправляет строковые поля запроса в Presidio Analyzer: `message.content`, Responses API `instructions` / `input` string/list text items, tool-call `arguments`, tool-output `output` string/list text items, text content blocks, `tool_calls[].function.arguments` и `function_call.arguments`.
4. Analyzer возвращает entity spans, entity types и scores.
5. Guardrail строит уникальные плейсхолдеры: `<PHONE_NUMBER_1>`, `<PHONE_NUMBER_2>`, `<RU_INN_1>`.
6. Guardrail сохраняет маппинг в Redis с TTL `PII_MAPPING_TTL_SECONDS`.
7. Только после успешного Redis save исходные строковые поля заменяются на masked text.
8. LiteLLM отправляет masked request LLM-провайдеру.
9. LiteLLM запускает `ru-pii-mask-post` в режиме `post_call`.
10. Guardrail восстанавливает плейсхолдеры в `content`, `reasoning_content`, response content blocks, `tool_calls[].function.arguments` и `function_call.arguments`.

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
| Provider key | `ZAI_API_KEY` | `ZAI_API_KEY`; опционально `ZAI_API_KEY_2` как секрет для второго deployment, `OPENAI_API_KEY`, `ANTHROPIC_API_KEY` |

Первая сборка может занять заметное время: Dockerfile скачивает spaCy model `ru_core_news_sm` и DeepPavlov archive `ner_rus_bert_torch_new.tar.gz`.

## Быстрый старт

```bash
# 1. Клонировать
git clone https://github.com/vovkins/ru-llm-proxy.git
cd ru-llm-proxy

# 2. Настроить
make setup
# Заполните нужные upstream API-ключи в .env:
#   ZAI_API_KEY=your-zai-key
#   ZAI_API_KEY_2=optional-second-zai-account  # сам по себе не включает второй deployment
#   OPENAI_API_KEY=your-openai-key
#   ANTHROPIC_API_KEY=your-anthropic-key

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
ZAI_API_KEY_2=optional-second-zai-key  # только секрет; второй deployment добавляется в litellm-config.yaml
OPENAI_API_KEY=your-openai-key
ANTHROPIC_API_KEY=your-anthropic-key
LITELLM_MASTER_KEY=sk-ru-...    # автогенерируется через make setup
LITELLM_SALT_KEY=...            # автогенерируется через make setup
LITELLM_ROUTING_TEST_KEY=...     # опциональный virtual key для make routing-smoke
RESPONSES_MODEL=...             # опциональный live-validated alias для strict /v1/responses smoke
MESSAGES_MODEL=...              # опциональный live-validated alias для strict /v1/messages smoke
UI_USERNAME=admin               # автогенерируется через make setup
UI_PASSWORD=...                 # автогенерируется через make setup
POSTGRES_PASSWORD=...           # автогенерируется через make setup
LITELLM_DB_URL=postgresql://litellm:...@db:5432/litellm
REDIS_URL=redis://redis:6379
PRESIDIO_ANALYZER_URL=http://presidio-analyzer:5001
PRESIDIO_ANALYZER_WORKERS=1
PRESIDIO_ANALYZER_CONCURRENCY_LIMIT=1
PRESIDIO_ANALYZER_QUEUE_LIMIT=8
PRESIDIO_ANALYZER_QUEUE_TIMEOUT_SECONDS=0.25
PRESIDIO_ANALYZER_DETECT_BARE_INN_BY_CHECKSUM=true
PII_GUARDRAIL_MODE=mask
PII_GUARDRAIL_FAILURE_MODE=fail_open
PII_MAPPING_TTL_SECONDS=3600
```

`make setup` не перезаписывает уже заданные реальные секреты. Если `.env` уже существует, команда добавит отсутствующие `UI_USERNAME` / `UI_PASSWORD`, опциональные routing/client-smoke переменные, Analyzer capacity defaults и заменит только placeholder-значения.

Build-time переменные для DeepPavlov:

```env
DEEPPAVLOV_NER_MODEL_URL=http://files.deeppavlov.ai/v1/ner/ner_rus_bert_torch_new.tar.gz
DEEPPAVLOV_NER_MODEL_SHA256=
DEEPPAVLOV_NER_DOWNLOAD_TIMEOUT_SECONDS=120
```

`DEEPPAVLOV_NER_MODEL_SHA256` опционален, но для воспроизводимой и более строгой сборки его стоит заполнить после доверенной загрузки архива.

Runtime capacity Analyzer:

| Переменная | По умолчанию | Назначение |
| --- | --- | --- |
| `PRESIDIO_ANALYZER_WORKERS` | `1` | Количество uvicorn worker processes. Каждый worker загружает отдельную копию spaCy/DeepPavlov model, поэтому память растёт примерно линейно. |
| `PRESIDIO_ANALYZER_CONCURRENCY_LIMIT` | `1` | Максимум активных Analyzer requests внутри одного worker. Значение `1` безопаснее для DeepPavlov/PyTorch inference. |
| `PRESIDIO_ANALYZER_QUEUE_LIMIT` | `8` | Сколько запросов может ждать свободный Analyzer slot внутри worker. |
| `PRESIDIO_ANALYZER_QUEUE_TIMEOUT_SECONDS` | `0.25` | Сколько ждать slot перед безопасной `503 analyzer_overloaded` ошибкой. |
| `PRESIDIO_ANALYZER_DETECT_BARE_INN_BY_CHECKSUM` | `true` | Детектировать checksum-valid bare INN без контекстного слова при API `score_threshold=0.35`. Если `false`, голый ИНН требует контекст вроде `ИНН` или `налогоплательщик`. |

Эффективный лимит активных model calls: `replicas * PRESIDIO_ANALYZER_WORKERS * PRESIDIO_ANALYZER_CONCURRENCY_LIMIT`. Память оценивайте как `replicas * PRESIDIO_ANALYZER_WORKERS * measured_RSS_per_worker + headroom`.

При перегрузке Analyzer возвращает `503` с reason `queue_full` или `queue_timeout`. LiteLLM guardrail трактует `analyzer_overloaded` как fail-closed override независимо от `PII_GUARDRAIL_FAILURE_MODE`: запрос останавливается, чтобы не отправить raw PII провайдеру. Для PII-sensitive окружений дополнительно используйте `fail_closed` для остальных инфраструктурных сбоев и масштабируйте Analyzer workers/replicas под доступную память.

Recognizer calibration:

- `RU_INN` всегда проходит checksum validation. По умолчанию `PRESIDIO_ANALYZER_DETECT_BARE_INN_BY_CHECKSUM=true`, поэтому checksum-valid bare INN проходит дефолтный Analyzer API `score_threshold=0.35`. Это повышает recall, но может маскировать редкие случайные 10/12-значные последовательности, прошедшие checksum.
- В strict mode (`PRESIDIO_ANALYZER_DETECT_BARE_INN_BY_CHECKSUM=false`) голый ИНН без контекста не проходит `score_threshold=0.35`; для детекции нужен контекст вроде `ИНН`, `налогоплательщик`, `налоговый`.
- `RU_ADDRESS` остаётся ограниченным regex recognizer. Поддерживаются базовые формы вроде `ул. Ленина, д. 10`, `ул Ленина 10`, `Тверская улица, дом 7`, но полноценный разбор индексов, регионов, владений и всех свободных российских адресов вне текущего scope.

### PII policy mode

`PII_GUARDRAIL_MODE` управляет штатным поведением после успешной детекции PII:

| Значение | Поведение |
| --- | --- |
| `mask` | Значение по умолчанию. Guardrail маскирует PII, сохраняет Redis mapping и отправляет masked request провайдеру. |
| `block` | Guardrail отклоняет запрос с найденной PII на pre-call этапе. Провайдер не вызывается, Redis mapping не создаётся. |

В block mode клиент получает безопасную `422` ошибку с entity types, но без raw PII, offsets или текста запроса.

`PII_GUARDRAIL_FAILURE_MODE` остаётся отдельной настройкой для инфраструктурных сбоев Presidio/Redis: `fail_open` пропускает запрос дальше, `fail_closed` останавливает его. Перегрузка Analyzer (`analyzer_overloaded`) всегда обрабатывается как fail-closed.

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
      access_groups: ["zai", "standard"]
  - model_name: zai-glm-5.1
    litellm_params:
      model: openai/glm-5.1
      api_base: https://api.z.ai/api/coding/paas/v4
      api_key: os.environ/ZAI_API_KEY
    model_info:
      id: z-ai-glm-5-1-alias
      base_model: glm-5.1
      access_groups: ["zai", "standard"]
  - model_name: openai-gpt-5.4-mini
    litellm_params:
      model: openai/gpt-5.4-mini
      api_key: os.environ/OPENAI_API_KEY
    model_info:
      id: openai-gpt-5-4-mini-primary
      base_model: gpt-5.4-mini
      access_groups: ["openai", "standard"]
  - model_name: claude-sonnet-4.6
    litellm_params:
      model: anthropic/claude-sonnet-4-6
      api_key: os.environ/ANTHROPIC_API_KEY
    model_info:
      id: anthropic-claude-sonnet-4-6-primary
      base_model: claude-sonnet-4-6
      access_groups: ["anthropic", "standard"]

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
        - name: "request_fields"
          type: "list[string]"
          description: "Masks message.content, Responses API instructions/input string/list text items, tool-call arguments, tool-output output string/list text items, text content blocks, tool_calls[].function.arguments, and function_call.arguments."
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
        - name: "response_fields"
          type: "list[string]"
          description: "Restores placeholders in content, reasoning_content, response content blocks, tool_calls[].function.arguments, and function_call.arguments."

litellm_settings:
  callbacks:
    - prometheus
  drop_params: true
```

OpenAI/Anthropic aliases in this repository are proxy-facing examples. Before production, verify the raw provider model IDs against the current LiteLLM image and your provider account, then update `litellm_params.model` if needed. Default smokes do not call those aliases until `RESPONSES_MODEL` / `MESSAGES_MODEL` are explicitly set.

### Добавление другого провайдера

Любой провайдер, поддерживаемый LiteLLM, добавляется через `model_list`:

```yaml
model_list:
  - model_name: my-openai-model
    litellm_params:
      model: openai/gpt-5.4-mini
      api_key: os.environ/OPENAI_API_KEY
```

После правки:

```bash
make restart
```

### Sticky routing к provider deployments

Включён LiteLLM `deployment_affinity`: запросы с одним и тем же клиентским LiteLLM key закрепляются за одним healthy deployment внутри model group. Это помогает использовать provider-side кэширование входных токенов, когда для одной модели настроено несколько аккаунтов или провайдеров.

Для корректной работы у каждого deployment должен быть стабильный `model_info.id`. Если добавляете второй аккаунт Z.AI, используйте тот же `model_name: glm-5.1`, другой `api_key` и новый `model_info.id`.

`ZAI_API_KEY_2` в `.env` только хранит секрет второго аккаунта. Пока в `litellm-config.yaml` нет второй записи `model_list` с `api_key: os.environ/ZAI_API_KEY_2`, default runtime остаётся single-deployment.

Проверка:

```bash
make routing-smoke
```

Команда делает два live-запроса с одним ключом, падает на HTTP/network errors и сравнивает header `x-litellm-model-id`. Подробности и пример добавления второго deployment: [docs/routing.md](docs/routing.md).

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
| `make test` | Локальный test suite: `test-unit` и Makefile diagnostics regression tests |
| `make test-unit` | Recognizers/NER, guardrail unit tests и deterministic flow |
| `make test-recognizers` | Unit-тесты recognizers и NER helpers |
| `make test-guardrail` | Unit-тесты LiteLLM guardrail |
| `make test-flow` | Deterministic проверка mask/unmask без внешнего LLM |
| `make test-routing-diagnostics` | Static regression tests для `routing-smoke` и `guardrails-smoke` Makefile targets |
| `make test-e2e` | Live smoke test против поднятых сервисов и реального LLM |
| `make virtual-key-create` | DevOps/CI helper: создать LiteLLM virtual key через admin API |
| `make client-auth-smoke` | Проверить client auth и `/v1` протоколы |
| `make guardrails-list` | Показать guardrails, зарегистрированные в LiteLLM |
| `make guardrails-smoke` | Live smoke guardrails: non-streaming, streaming SSE и Redis cleanup |
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

Основной путь управления пользователями и ключами — LiteLLM Admin UI. CLI-helper остаётся вспомогательным путём для DevOps/CI/bootstrap/runbook-сценариев, когда UI недоступен или нужен автоматический short-lived key:

```bash
make virtual-key-create KEY_ALIAS=local-coding MODELS=standard,zai,openai DURATION=30d
```

Полученный `RU_LLM_PROXY_TOKEN` используется в локальных клиентах. Upstream-ключи провайдеров остаются только в `.env` на proxy.

`make test-e2e`, `make client-auth-smoke` и `make guardrails-smoke` могут создавать short-lived virtual keys через `LITELLM_MASTER_KEY` как bootstrap test flow. Этот admin key остаётся внутри proxy/CI окружения; client-facing запросы тестов идут через generated virtual key.

## Клиенты

Поддерживаемые клиентские поверхности:

- Codex CLI / Codex App local tasks: [docs/clients/codex.md](docs/clients/codex.md)
- Claude Code: [docs/clients/claude-code.md](docs/clients/claude-code.md)
- OpenCode CLI / Desktop: [docs/clients/opencode.md](docs/clients/opencode.md)
- Kilo Code VS Code / CLI: [docs/clients/kilo-code.md](docs/clients/kilo-code.md)
- JWT/OIDC proxy auth: [docs/clients/jwt.md](docs/clients/jwt.md)

Базовые endpoint contracts:

- OpenAI Chat Completions: `POST /v1/chat/completions`
- OpenAI Responses: `POST /v1/responses`
- Anthropic Messages: `POST /v1/messages`

Proxy auth по умолчанию основан на LiteLLM virtual keys. JWT/OIDC — отдельный enterprise deployment path: он включается только при наличии IdP/JWKS и не заменяет upstream provider keys.

Есть два режима upstream auth:

- Server-funded: клиент отправляет `Authorization: Bearer $RU_LLM_PROXY_TOKEN`, proxy вызывает провайдера через свои `OPENAI_API_KEY`, `ANTHROPIC_API_KEY` или `ZAI_API_KEY`.
- Client-side BYOK passthrough: клиент отправляет `x-litellm-api-key: Bearer $RU_LLM_PROXY_TOKEN` для доступа к proxy, а поддерживаемый provider-specific header (`x-api-key`, `api-key`, `x-goog-api-key` и аналогичные) форвардится upstream. Этот режим не включён в default config; включайте его только в отдельном окружении после live validation на текущем LiteLLM image.

Codex/ChatGPT и Claude subscription OAuth обычно полагаются на provider `Authorization`. Обычный LiteLLM path не считается подтверждённым для такого passthrough: если live validation покажет, что нужный OAuth `Authorization` не форвардится, нужен отдельный pass-through route, sidecar или custom adapter.

Subscription auth остаётся на клиентской машине. Не кладите общий Codex `auth.json` или Claude credentials на proxy как shared upstream credential.

## Примеры использования

### Базовый запрос

```bash
export RU_LLM_PROXY_TOKEN="sk-..."

curl http://localhost:4000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $RU_LLM_PROXY_TOKEN" \
  -d '{
    "model": "glm-5.1",
    "messages": [{"role": "user", "content": "Привет!"}]
  }'
```

### Запрос с PII

```bash
curl http://localhost:4000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $RU_LLM_PROXY_TOKEN" \
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

Для smoke-проверки применения guardrails к live-запросам:

```bash
make guardrails-smoke
```

Команда предназначена для локального docker-compose окружения: HTTP-запросы идут в
`LITELLM_URL` с `localhost`, `127.0.0.1` или `[::1]`, а Redis cleanup проверяется через
локальный `docker compose exec redis`. Она отправляет non-streaming и streaming
`POST /v1/chat/completions`, проверяет `x-litellm-applied-guardrails`, читает SSE
stream до конца и убеждается, что после завершения stream в Redis не осталось
smoke-owned `pii_mapping:*` ключей с уникальным PII-маркером текущего запуска.

Таймауты live-запросов настраиваются через `CURL_CONNECT_TIMEOUT` и `CURL_MAX_TIME`
по умолчанию 10 и 180 секунд.

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
make test             # test-unit + routing diagnostics regression test
make test-unit        # recognizers, NER helpers, guardrail unit tests, deterministic flow
make test-recognizers
make test-guardrail
make test-flow        # deterministic проверка без внешнего LLM
make test-routing-diagnostics
make test-e2e         # live smoke test; нужны make up и ZAI_API_KEY
make routing-smoke    # live sticky routing smoke; нужны make up и provider key
make client-auth-smoke # live проверка virtual keys и /v1 протоколов
RESPONSES_MODEL=openai-gpt-5.4-mini MESSAGES_MODEL=claude-sonnet-4.6 REQUIRE_ALL_PROTOCOLS=1 make client-auth-smoke
# fail, если нет provider key или live-validated model alias для любого /v1 протокола
```

`make test-flow` проверяет, что PII маскируется до simulated model call и восстанавливается после него. `make test-routing-diagnostics` статически проверяет, что `routing-smoke` ловит HTTP/network failures, использует `/v1/chat/completions` и не печатает proxy token. `make test-e2e` остаётся live smoke test: реальный провайдер может опустить или переформулировать плейсхолдеры.

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
- guardrail восстанавливает плейсхолдеры в `content`, `reasoning_content`, response content blocks и function/tool call arguments.
- для streaming responses guardrail восстанавливает placeholders в `delta.content` и `delta.reasoning_content`, включая placeholders, разорванные между соседними чанками.

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
