# Monitoring and Operations

Документ описывает, как мониторить `ru-llm-proxy` в production-like окружении и как обновлять LiteLLM без полной пересборки проекта.

## Цели мониторинга

Мониторинг должен отвечать на пять вопросов:

- жив ли LiteLLM proxy и принимает ли он запросы;
- доступен ли Presidio Analyzer и загружен ли DeepPavlov NER;
- доступны ли Redis и PostgreSQL;
- применяются ли PII guardrails и сколько PII они маскируют;
- стабильно ли клиенты попадают в свои provider deployments при sticky routing;
- есть ли fail-open/fail-closed события, при которых PII-защита работает нештатно.

LiteLLM Admin UI полезен для операционных действий, ключей, usage/spend и просмотра логов. Он не должен быть единственным источником observability для guardrails.

## Health Checks

Host-side проверка:

```bash
make health
```

Что проверяется:

| Target | Endpoint / command | Назначение |
| --- | --- | --- |
| LiteLLM | `GET /health/liveliness` | Process liveness без API token и без реального LLM call |
| Presidio Analyzer | `GET /api/v1/health` | Доступность analyzer и статус NER |
| PostgreSQL | `pg_isready` | Доступность LiteLLM database |
| Redis | `redis-cli ping` | Доступность temporary mapping store для PII и LiteLLM routing affinity |

`GET /health` у LiteLLM не используется для Docker healthcheck, потому что этот endpoint предназначен для model health monitoring и может делать реальные LLM API calls.

## Prometheus

В `litellm-config.yaml` включён Prometheus callback:

```yaml
litellm_settings:
  callbacks:
    - prometheus
  drop_params: true
```

После перезапуска LiteLLM метрики доступны на:

```text
http://localhost:4000/metrics
```

Локальная проверка:

```bash
make metrics
make monitor-smoke
```

Пример scrape config для Prometheus внутри той же Docker/network-инфраструктуры:

```yaml
scrape_configs:
  - job_name: ru-llm-proxy
    metrics_path: /metrics
    static_configs:
      - targets:
          - ru-llm-proxy:4000
```

Если Prometheus работает снаружи Docker Compose host, используйте опубликованный адрес proxy, например `host.example.com:4000`.

Текущий compose запускает один LiteLLM process. Если в production вы включите несколько workers, настройте `PROMETHEUS_MULTIPROC_DIR` для корректной агрегации Prometheus client метрик между worker-процессами.

## LiteLLM Metrics

LiteLLM отдаёт стандартные метрики proxy, provider calls, latency, token usage, spend, virtual keys и callback failures. Основные семейства метрик для dashboard:

| Metric | Что показывает |
| --- | --- |
| `litellm_proxy_total_requests_metric_total` | Входящие запросы к proxy |
| `litellm_proxy_failed_requests_metric_total` | Ошибки на уровне proxy |
| `litellm_deployment_total_requests_total` | Вызовы LLM provider deployment |
| `litellm_deployment_success_responses_total` | Успешные ответы provider |
| `litellm_deployment_failure_responses_total` | Ошибки provider |
| `litellm_request_total_latency_metric_*` | End-to-end latency proxy request |
| `litellm_llm_api_latency_metric_*` | Latency внешнего LLM API |
| `litellm_total_tokens_metric_total` | Token usage |
| `litellm_callback_logging_failures_metric_total` | Ошибки доставки observability callbacks |

Точный набор labels зависит от версии LiteLLM и настроек virtual keys / teams.

## Routing Observability

Sticky routing включён через LiteLLM Router `deployment_affinity`. Он сохраняет в Redis mapping между хэшем клиентского LiteLLM key и `model_info.id` provider deployment.

Проверка вручную:

```bash
make routing-smoke
```

Команда делает два запроса одним ключом и сравнивает response header `x-litellm-model-id`. Для проверки пользовательского virtual key задайте `LITELLM_ROUTING_TEST_KEY` в `.env`; иначе используется `LITELLM_MASTER_KEY`.

Что отдавать в мониторинг DevOps-команде:

- response header `x-litellm-model-id` полезен для ad-hoc диагностики и synthetic checks;
- `litellm_deployment_total_requests_total`, `litellm_deployment_success_responses_total` и `litellm_deployment_failure_responses_total` показывают нагрузку и ошибки по deployments;
- `model_info.id` должен быть стабильным, иначе старые affinity mappings и dashboard labels потеряют смысл;
- Redis должен мониториться как dependency не только PII guardrail, но и routing affinity.

Если в одной model group несколько deployments, synthetic check может запускаться с отдельным virtual key и проверять, что два последовательных запроса получают один и тот же `x-litellm-model-id`. При падении pinned deployment LiteLLM имеет право выбрать другой healthy deployment, поэтому alert должен учитывать состояние provider deployments.

## PII Guardrail Metrics

Проект добавляет собственные низкокардинальные метрики. Они не содержат пользовательский текст, request id, PII или raw placeholders.

| Metric | Type | Labels | Назначение |
| --- | --- | --- | --- |
| `ru_pii_guardrail_pre_calls_total` | Counter | `result` | Итог pre-call: `masked`, `blocked`, `pre_egress_policy_blocked`, `final_payload_leak_check_blocked`, `clean`, `skipped`, `error` |
| `ru_pii_guardrail_post_calls_total` | Counter | `result` | Итог post-call: `restored`, `no_placeholders`, `no_mapping`, `skipped`, `unsupported_response`, `error` |
| `ru_pii_guardrail_entities_detected_total` | Counter | `entity_type` | Количество замаскированных сущностей по типам |
| `ru_pii_guardrail_blocked_total` | Counter | `entity_type` | Количество заблокированных сущностей по типам в `PII_GUARDRAIL_MODE=block` |
| `ru_pre_egress_policy_blocked_total` | Counter | `category` | Количество config/log payload blocks по bounded categories |
| `ru_final_payload_leak_check_blocked_total` | Counter | `rule_id` | Количество final provider-bound leak-check blocks по bounded rule ids |
| `ru_pii_guardrail_fail_open_total` | Counter | `operation` | Ошибки, после которых запрос продолжен в режиме `fail_open` |
| `ru_pii_guardrail_fail_closed_total` | Counter | `operation` | Ошибки, после которых запрос остановлен в режиме `fail_closed` |
| `ru_pii_guardrail_analyzer_latency_seconds_*` | Histogram | none | Latency вызовов Presidio Analyzer |
| `ru_pii_guardrail_redis_latency_seconds_*` | Histogram | `operation` | Latency Redis операций `save`, `load`, `delete` |
| `ru_pii_guardrail_mapping_size_*` | Histogram | none | Количество placeholder mappings на masked request |

PII guardrail метрики появятся в `/metrics` после первого запроса, который прошёл через guardrail.

## Guardrail Dependency Client Limits

LiteLLM guardrail переиспользует Redis и Analyzer HTTP clients между pre-call и post-call guardrail instances внутри одного процесса/event loop. Для мониторинга это означает, что рост latency в `ru_pii_guardrail_analyzer_latency_seconds_*` или `ru_pii_guardrail_redis_latency_seconds_*` может быть связан не только с самим Analyzer/Redis, но и с ожиданием свободного connection в shared client pool.

Основные ручки:

| Переменная | По умолчанию | Что смотреть |
| --- | --- | --- |
| `PII_GUARDRAIL_REDIS_MAX_CONNECTIONS` | `20` | Redis pool saturation, рост `ru_pii_guardrail_redis_latency_seconds_*`, Redis server connection count. |
| `PII_GUARDRAIL_REDIS_SOCKET_CONNECT_TIMEOUT_SECONDS` | `1.0` | Fail-open/fail-closed события `mapping_save`, `mapping_load`, `mapping_delete` при сетевых проблемах Redis. |
| `PII_GUARDRAIL_REDIS_SOCKET_TIMEOUT_SECONDS` | `2.0` | Redis operation timeout и рост Redis latency histogram. |
| `PII_GUARDRAIL_ANALYZER_TIMEOUT_SECONDS` | `30.0` | Долгие Analyzer calls, `ru_pii_guardrail_analyzer_latency_seconds_*`, fail-open/fail-closed `masking`. |
| `PII_GUARDRAIL_ANALYZER_CONNECT_TIMEOUT_SECONDS` | `5.0` | Ошибки подключения к Analyzer container/service. |
| `PII_GUARDRAIL_ANALYZER_MAX_CONNECTIONS` | `20` | HTTP client pool saturation к Analyzer на один процесс/event loop LiteLLM. |
| `PII_GUARDRAIL_ANALYZER_MAX_KEEPALIVE_CONNECTIONS` | `10` | Стабильность keep-alive reuse при регулярной нагрузке. |

Эти лимиты не заменяют Analyzer capacity limiter. Если `analyzer_overloaded` растёт, сначала смотрите `PRESIDIO_ANALYZER_*` capacity и memory budget модели; если latency растёт без `analyzer_overloaded`, проверяйте pool limits, сеть и Redis/Analyzer service health.

Для graceful teardown shared clients закрываются через `close_guardrail_dependency_clients()`. Если guardrail запускается вне стандартного Docker Compose процесса или в test harness, teardown должен вызывать этот helper, чтобы HTTPX transports и Redis connection pools не оставались открытыми после очистки caches.

## Recommended Alerts

Базовые alert conditions:

```promql
increase(ru_pii_guardrail_fail_open_total[5m]) > 0
```

Есть fail-open событие: Presidio или Redis не сработали, а запрос был пропущен дальше.

```promql
increase(ru_pii_guardrail_fail_closed_total[5m]) > 0
```

Есть fail-closed событие: запрос был остановлен guardrail.

```promql
increase(ru_pii_guardrail_fail_closed_total{operation="analyzer_overloaded"}[5m]) > 0
```

Analyzer capacity limiter отклонил запрос, и guardrail остановил его как fail-closed override.

```promql
sum(rate(ru_pii_guardrail_pre_calls_total{result="error"}[5m])) > 0
```

Ошибки pre-call обработки.

```promql
sum(rate(ru_pii_guardrail_pre_calls_total{result="blocked"}[5m])) > 0
```

Block mode отклоняет запросы с PII до вызова провайдера. Это ожидаемое policy event, но его стоит мониторить как security telemetry.

```promql
histogram_quantile(0.95, sum(rate(ru_pii_guardrail_analyzer_latency_seconds_bucket[5m])) by (le)) > 2
```

Presidio Analyzer p95 latency выше 2 секунд.

```promql
sum(rate(litellm_proxy_failed_requests_metric_total[5m])) > 0
```

Ошибки на уровне LiteLLM proxy.

Для Analyzer health отдельно проверьте `GET /api/v1/health`. Поле `ner` показывает загрузку DeepPavlov: если оно равно `not_loaded`, regex recognizers продолжают работать, но `PERSON`, `LOCATION` и `ORGANIZATION` через DeepPavlov NER не детектируются. Поле `capacity` показывает process-local limiter: `active`, `waiting`, `concurrency_limit`, `queue_limit` и `queue_timeout_seconds`.

Analyzer overload возвращает `503` с `detail.code=analyzer_overloaded` и reason `queue_full` или `queue_timeout`. Для LiteLLM guardrail это fail-closed override независимо от `PII_GUARDRAIL_FAILURE_MODE`: запрос останавливается, чтобы перегрузка Analyzer не отправила raw PII провайдеру. Если `waiting` часто приближается к `queue_limit`, увеличивайте replicas/workers только с учётом памяти: каждый uvicorn worker загружает отдельную spaCy/DeepPavlov model instance.

## Logs

Guardrail пишет structured JSON logs без prompt text и без raw PII.
Поле `request_id` в событиях guardrail — server-generated PII mapping id из `metadata.pii_request_id`, а не клиентский `metadata.request_id`.
При `PRE_EGRESS_POLICY_MODE=block` событие `pre_egress_policy_blocked` фиксирует блокировку config/log payload до Analyzer/provider egress. В логах остаются только bounded categories, rule ids и counts; raw payload, snippets, offsets и secret values не пишутся.
При `FINAL_PAYLOAD_LEAK_CHECK_MODE=block` событие `final_payload_leak_check_blocked` фиксирует deterministic leak marker в уже provider-bound тексте после proxy-side mutation и до provider call. В логах остаются только bounded rule ids и counts; raw matched values, prompt snippets, offsets, provider keys и mapping contents не пишутся.

Основные события:

| Event | Уровень | Поля |
| --- | --- | --- |
| `pii_guardrail_masked` | `INFO` | `request_id`, `masked_count`, `entity_counts`, `mapping_ttl_seconds` |
| `pii_guardrail_blocked` | `INFO` | `request_id`, `entity_types`, `entity_counts` |
| `pre_egress_policy_blocked` | `INFO` | `request_id`, `categories`, `rules`, `category_counts`, `finding_count` |
| `final_payload_leak_check_blocked` | `INFO` | `request_id`, `rules`, `rule_counts`, `finding_count` |
| `pii_guardrail_restored` | `INFO` | `request_id`, `mapping_size`, `restored_fields` |
| `pii_guardrail_stream_restored` | `INFO` | `request_id`, `mapping_size`, `restored_fields` |
| `pii_guardrail_no_mapping` | `INFO` | `request_id` |
| `pii_guardrail_failed_open` | `ERROR` | `operation`, `failure_mode`, `error_type` |
| `pii_guardrail_failed_closed` | `ERROR` | `operation`, `failure_mode`, `error_type` |
| `pii_guardrail_analyzer_overloaded` | `ERROR` | `failure_mode`, `reason` |
| `pii_guardrail_cleanup_failed` | `WARNING` | `request_id`, `error_type` |
| `pii_guardrail_unsupported_response` | `WARNING` | `request_id`, `response_type`, `mapping_size` |

DevOps-рекомендации:

- собирать stdout/stderr всех контейнеров через штатный log collector;
- парсить JSON logs guardrail как отдельный источник security telemetry;
- не включать debug-логи внешнего LLM provider в production без отдельного privacy review;
- хранить `request_id` как guardrail mapping id, но не использовать его как Prometheus label.

## Guardrails UI

`guardrail_info` в LiteLLM config возвращается через `GET /guardrails/list`. Текущий LiteLLM UI может показывать список guardrails, но не обязан отображать все произвольные поля `guardrail_info`.

Для проверки registration metadata используйте:

```bash
make guardrails-list
```

Для проверки фактического применения guardrails используйте:

```bash
make guardrails-smoke
```

Smoke предназначен для локального docker-compose окружения (`make up`): Redis cleanup
проверяется через `docker compose exec -T redis`, а `LITELLM_URL` должен указывать на
`localhost`, `127.0.0.1` или `[::1]`. Он выполняет non-streaming и streaming
`/v1/chat/completions`, проверяет `x-litellm-applied-guardrails`, SSE events до
`[DONE]` и отсутствие smoke-owned Redis mappings с тестовой PII после завершения
streaming response. Таймауты можно переопределить через `CURL_CONNECT_TIMEOUT` и
`CURL_MAX_TIME`. Это canary на то, что текущий LiteLLM image по-прежнему dispatch-ит
`async_post_call_streaming_iterator_hook`.

Для production monitoring используйте Prometheus metrics и structured logs, а UI рассматривайте как вспомогательный административный инструмент.

## Обновление LiteLLM

В текущем `docker-compose.yml` LiteLLM запускается из готового image:

```yaml
image: docker.litellm.ai/berriai/litellm:main-stable
```

Поэтому для обновления LiteLLM не нужно пересобирать весь проект. Достаточно подтянуть новый image и пересоздать только proxy container:

```bash
make update-litellm
```

Эквивалентные команды:

```bash
docker compose pull litellm
docker compose up -d --force-recreate --no-deps litellm
```

Когда что делать:

| Изменение | Действие |
| --- | --- |
| Новый LiteLLM image | `make update-litellm` |
| Изменился `litellm-config.yaml` | `make restart` |
| Изменился `litellm_guardrails/*.py` | `make restart` |
| Изменился `.env` для LiteLLM | `docker compose up -d --force-recreate --no-deps litellm` |
| Изменились `PRESIDIO_ANALYZER_*` runtime limits | `docker compose up -d --force-recreate --no-deps presidio-analyzer` |
| Изменился `presidio/Dockerfile` или analyzer dependencies | `make build`, затем `make up` |

Production-рекомендация: после staging-проверки фиксируйте конкретный LiteLLM tag или image digest вместо долгого использования плавающего `main-stable`. Перед обновлением сделайте backup PostgreSQL volume/database, потому что в PostgreSQL хранится состояние LiteLLM: virtual keys, users, budgets и usage/spend data.

Минимальный update checklist:

1. Зафиксировать текущий image digest: `docker compose images litellm`.
2. Сделать backup PostgreSQL.
3. Выполнить `make update-litellm`.
4. Проверить `make health`.
5. Проверить `make guardrails-list`.
6. Проверить `make guardrails-smoke` в локальном docker-compose окружении, чтобы поймать drift non-streaming/streaming guardrail hooks.
7. Проверить `make test-final-leak-proxy` в локальном docker-compose окружении, чтобы поймать drift pre-call hook order и provider non-egress для final leak-check.
8. Проверить `make routing-smoke`.
9. Выполнить PII smoke request и проверить `make monitor-smoke`.
9. Если есть regression, откатить image tag/digest в `docker-compose.yml` и пересоздать `litellm`.

## References

- [LiteLLM Prometheus metrics](https://docs.litellm.ai/docs/proxy/prometheus)
- [LiteLLM load balancing](https://docs.litellm.ai/docs/proxy/load_balancing)
- [LiteLLM Guardrails quick start](https://docs.litellm.ai/docs/proxy/guardrails/quick_start)
- [LiteLLM Custom Guardrail](https://docs.litellm.ai/docs/proxy/guardrails/custom_guardrail)
- [LiteLLM Logging](https://docs.litellm.ai/docs/proxy/logging)
