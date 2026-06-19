# Архитектура

Документ описывает текущую реализацию `ru-llm-proxy`.

## Компоненты

| Компонент | Service | Ответственность |
| --- | --- | --- |
| LiteLLM Proxy | `litellm` | OpenAI-compatible gateway, sticky routing к provider deployments, выполнение guardrails |
| PII Guardrail | `litellm_guardrails/pii_guardrail.py` | Маскирование запросов, Redis-маппинг, восстановление ответов |
| Presidio Analyzer | `presidio-analyzer` | Детекция PII через русские regex recognizers и опциональный DeepPavlov NER |
| Redis | `redis` | Временное хранение обратимых placeholder mappings и LiteLLM deployment affinity |
| PostgreSQL | `db` | Persistence для LiteLLM |

## Поток запроса

```text
1. Клиент отправляет POST /chat/completions в LiteLLM.
2. LiteLLM запускает ru-pii-mask-pre в режиме pre_call.
3. Guardrail отправляет каждое string message в Presidio Analyzer.
4. Analyzer возвращает entity spans, entity types и scores.
5. Guardrail строит request-scoped placeholders в порядке исходного текста.
6. Guardrail сохраняет placeholder -> original mappings в Redis.
7. Только после успешного Redis save содержимое message заменяется на masked text.
8. LiteLLM отправляет masked request настроенному LLM-провайдеру.
9. LiteLLM запускает ru-pii-mask-post в режиме post_call.
10. Guardrail загружает Redis mapping и заменяет placeholders в response content.
11. Redis mapping удаляется после post-call обработки.
```

Пример трансформации:

```text
Input:       Мой телефон +79031234567, ИНН 7707083893
To provider: Мой телефон <PHONE_NUMBER_1>, ИНН <RU_INN_1>
Mapping:     <PHONE_NUMBER_1> -> +79031234567
             <RU_INN_1>       -> 7707083893
```

## Конфигурация Guardrail

В LiteLLM настроены два guardrail entry, потому что pre-call и post-call hooks выполняются через разные modes:

```yaml
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
```

`async_pre_call_hook` маскирует запросы. `async_post_call_success_hook` восстанавливает `content` и, если поле присутствует, `reasoning_content`.

`guardrail_info` добавляет metadata для LiteLLM API. Регистрацию и metadata можно проверить через `GET /guardrails/list` или `make guardrails-list`. LiteLLM UI может показывать список guardrails, но не обязан отображать все произвольные поля `guardrail_info`.

## Guardrails UI и наблюдаемость

`default_on: true` включает guardrail для обычных запросов, но для диагностики полезно явно передавать request parameter:

```json
{
  "guardrails": ["ru-pii-mask-pre", "ru-pii-mask-post"]
}
```

`make guardrails-smoke` отправляет live request с этим параметром и печатает header `x-litellm-applied-guardrails`, если LiteLLM вернул его.

Guardrails Monitor в LiteLLM UI опирается на события/traces, которые LiteLLM пишет через свою logging/observability подсистему. В текущем проекте primary monitoring path — Prometheus `/metrics`, health checks и structured logs guardrail.

В `litellm_settings` включён Prometheus callback:

```yaml
litellm_settings:
  callbacks:
    - prometheus
  drop_params: true
```

Проект добавляет собственные метрики `ru_pii_guardrail_*` для pre-call/post-call outcomes, entity counts, fail-open/fail-closed событий, Presidio latency, Redis latency и mapping size. Structured logs guardrail пишутся в JSON без prompt text и без raw PII. Подробный DevOps guide: [monitoring.md](monitoring.md).

References:

- https://docs.litellm.ai/docs/proxy/guardrails/quick_start
- https://docs.litellm.ai/docs/proxy/guardrails/custom_guardrail
- https://docs.litellm.ai/docs/proxy/prometheus

## Provider routing и sticky affinity

В `litellm-config.yaml` включена Router pre-call проверка `deployment_affinity`:

```yaml
router_settings:
  redis_url: os.environ/REDIS_URL
  routing_strategy: simple-shuffle
  optional_pre_call_checks:
    - deployment_affinity
  deployment_affinity_ttl_seconds: 86400
```

LiteLLM использует `user_api_key_hash` из request metadata и сохраняет в Redis привязку этого клиента к конкретному `model_info.id`. При следующих запросах того же ключа Router старается выбрать тот же healthy deployment. Если deployment недоступен или mapping отсутствует/истёк, LiteLLM возвращается к обычной стратегии выбора и обновляет привязку.

Это помогает provider-side кэшированию входных токенов, когда в одной model group настроено несколько аккаунтов или провайдеров. Для стабильной работы у каждого deployment должен быть постоянный `model_info.id`:

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
```

Подробности, пример второго аккаунта и smoke-проверка описаны в [routing.md](routing.md).

## Семантика плейсхолдеров

Плейсхолдеры уникальны в рамках одного запроса и группируются по entity type:

```text
<PERSON_1>
<PHONE_NUMBER_1>
<PHONE_NUMBER_2>
<RU_INN_1>
```

Счётчик request-scoped, а не message-scoped. Если один запрос содержит два user messages с телефонами, второй телефон получит `<PHONE_NUMBER_2>`.

`presidio/analyzer_server.py` дедуплицирует пересекающиеся результаты analyzer/NER. Guardrail дополнительно пропускает некорректные spans и оставшиеся пересечения во время построения замен.

## Failure Modes

Guardrail поддерживает два режима через `PII_GUARDRAIL_FAILURE_MODE`.

| Mode | Поведение |
| --- | --- |
| `fail_open` | По умолчанию. При сбоях Presidio/Redis запрос остаётся неизменённым. Если Redis save не удался, guardrail не применяет частичную маскировку. |
| `fail_closed` | При сбоях Presidio/Redis выбрасывается ошибка, запрос не продолжается. |

TTL Redis-маппингов задаётся через `PII_MAPPING_TTL_SECONDS`, значение по умолчанию `3600`.

## Analyzer

Analyzer service — FastAPI приложение в `presidio/analyzer_server.py`.

LiteLLM guardrail использует Analyzer в основном request path. На `pre_call` guardrail отправляет каждое строковое сообщение в `POST /api/v1/analyze`, получает spans и строит обратимые плейсхолдеры самостоятельно. Без `presidio-analyzer` автоматическая PII-детекция в запросах не работает.

Источники детекции:

| Источник | Entity types |
| --- | --- |
| Regex recognizers | `PHONE_NUMBER`, `EMAIL_ADDRESS`, `RU_INN`, `RU_SNILS`, `RU_PASSPORT`, `CREDIT_CARD`, `RU_ADDRESS` |
| DeepPavlov NER | `PERSON`, `LOCATION`, `ORGANIZATION` |

### NLP Stack

Analyzer использует две разные NLP-составляющие:

| Компонент | Где подключается | Назначение |
| --- | --- | --- |
| spaCy `ru_core_news_sm` | `NlpEngineProvider` в `presidio/analyzer_server.py` | NLP backend для Presidio Analyzer: токенизация и базовая языковая обработка |
| DeepPavlov `ner_rus_bert` | `presidio/ner/deeppavlov_recognizer.py` | Отдельная BERT-based NER модель для `PERSON`, `LOCATION`, `ORGANIZATION` |

`ru_core_news_sm` не является обёрткой над `ner_rus_bert`, и `ner_rus_bert` не заменяет spaCy backend. Regex recognizers работают через Presidio Analyzer, а DeepPavlov NER запускается дополнительно и затем объединяется с результатами analyzer.

DeepPavlov загружается на startup. Если модель не загрузилась, analyzer остаётся доступен, regex recognizers продолжают работать, а `/api/v1/health` возвращает `ner: "not_loaded"`.

NER запускается только когда он может повлиять на ответ Analyzer:

- если `entities` не указан, NER добавляет `PERSON`, `LOCATION` и `ORGANIZATION`;
- если `entities` указан, NER добавляет только пересечение запрошенного списка с `PERSON`, `LOCATION`, `ORGANIZATION`;
- если `score_threshold > 0.7`, NER пропускается, потому что DeepPavlov не отдаёт per-entity confidence, а проект присваивает NER spans фиксированный score `0.7`.

Offsets для NER spans вычисляются по исходному тексту после объединения BIO-тегов. Для повторяющихся сущностей с одинаковым текстом поиск идёт последовательно от конца предыдущего найденного span, поэтому одинаковые значения получают разные позиции.

## Health Checks

LiteLLM container healthcheck использует `GET /health/liveliness`, а не `GET /health`.

Причины:

- `/health/liveliness` — unauthenticated liveness probe, предназначенный для проверки, что proxy process жив;
- `/health` предназначен для model health monitoring и делает реальные LLM API calls;
- официальный LiteLLM image не обязан содержать `curl`, поэтому Docker healthcheck запускает Python stdlib `urllib.request` внутри контейнера.

Host-side `make health` также проверяет LiteLLM через `/health/liveliness`, чтобы не требовать `LITELLM_MASTER_KEY` для обычной проверки статуса сервисов.

Reference: https://docs.litellm.ai/docs/proxy/health

## Admin UI

LiteLLM Admin UI доступен на `/ui`. Для входа используются `UI_USERNAME` и `UI_PASSWORD`, которые `make setup` генерирует в `.env` отдельно от `LITELLM_MASTER_KEY`.

`LITELLM_MASTER_KEY` остаётся admin API key для автоматизации и не должен выдаваться обычным пользователям. Пользовательский доступ оформляется через LiteLLM virtual keys.

Reference: https://docs.litellm.ai/docs/proxy/ui

## Границы данных

Ожидаемая privacy boundary:

- Presidio Analyzer и Redis работают внутри Docker Compose network.
- Внешний LLM-провайдер получает masked prompt text.
- Redis временно хранит исходные PII для post-call восстановления.
- Redis также хранит LiteLLM deployment affinity mapping по хэшу клиентского ключа и `model_info.id`; raw API key в mapping не сохраняется.
- PostgreSQL хранит состояние LiteLLM; текущий guardrail mapping там не сохраняется.

## Сборка и зависимости

Presidio Analyzer image собирается из target `analyzer` в `presidio/Dockerfile`.

Dependency constraints вынесены из Dockerfile:

| Файл | Использование |
| --- | --- |
| `presidio/requirements-analyzer.txt` | Analyzer runtime, Presidio, spaCy, torch, transformers, pytest |
| `presidio/requirements-torchcrf.txt` | `pytorch-crf`, устанавливается после `torch` |
| `tests/requirements-guardrails.txt` | Test-only зависимости guardrail test runner |

DeepPavlov устанавливается отдельно с `--no-deps`, потому что его transitive pins тянут старые версии зависимостей, неподходящие для текущего Python 3.11 образа. Совместимые runtime dependencies задаются явно в requirements-файлах.

Analyzer build скачивает DeepPavlov model archive через `presidio/download_model.py`. Скрипт:

- скачивает archive во временный файл и атомарно переименовывает его после успешной загрузки;
- разрешает только `http`/`https` URLs;
- считает SHA-256;
- проверяет SHA-256, если задан `DEEPPAVLOV_NER_MODEL_SHA256`;
- отклоняет absolute paths, path traversal и link entries при распаковке tar;
- распаковывает модель во временную директорию и затем атомарно переносит ожидаемый model directory.

Build args пробрасываются из `.env` через `docker-compose.yml`:

```env
DEEPPAVLOV_NER_MODEL_URL=http://files.deeppavlov.ai/v1/ner/ner_rus_bert_torch_new.tar.gz
DEEPPAVLOV_NER_MODEL_SHA256=
DEEPPAVLOV_NER_DOWNLOAD_TIMEOUT_SECONDS=120
```

## Текущие ограничения

- Восстановление ответа работает только для плейсхолдеров, которые провайдер вернул.
- Streaming post-call restoration не реализован. Для сценариев с обязательным восстановлением используйте non-streaming calls.
- DeepPavlov span alignment основан на поиске token text в исходной строке и может пропускать сущности, если модель токенизировала их в форме, которой нет в исходной строке.
- Requirements-файлы задают compatibility constraints, но это ещё не полный lockfile с hash-проверкой всех Python wheels.
