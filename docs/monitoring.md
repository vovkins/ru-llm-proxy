# Мониторинг и эксплуатация

Документ предназначен для команды эксплуатации. Настройки перечислены в
[configuration.md](configuration.md), доказательные проверки — в
[compliance.md](compliance.md).

## Что контролировать

Мониторинг должен отвечать на ключевые вопросы:

- живы ли LiteLLM, Analyzer, Redis и PostgreSQL, а в экспериментальном профиле
  также `codex-lb` и его отдельная база;
- находится ли обязательная NER-модель в состоянии `ready`;
- выполняются ли защитные политики и нет ли безопасных отказов;
- не растут ли очередь и задержка Analyzer;
- стабилен ли выбор провайдера модели;
- защищены ли административные маршруты и аудируются ли действия операторов.

Интерфейс LiteLLM помогает администрировать прокси, но не заменяет Prometheus,
структурированные журналы и синтетические проверки.

## Контуры проверки

| Контур | Команда | Что доказывает |
| --- | --- | --- |
| Безопасность исходящего запроса | `make test-egress-security` | Исходные проверочные значения не попали имитируемому провайдеру; заблокированный запрос не создал исходящий вызов |
| Наблюдаемость | `make test-observability-gates` | События и статусы проверяются отдельно от перехвата исходящего запроса |
| Живой провайдер | `make guardrails-smoke`, `make test-e2e`, `make routing-smoke` | Работает реальный путь LiteLLM; такой тест сам по себе не доказывает отсутствие утечки |

Полная матрица: [docs/compliance.md](compliance.md). Сетевые ограничения:
[docs/egress-controls.md](egress-controls.md) и
[`deploy/kubernetes/egress`](../deploy/kubernetes/egress).

## Состояние сервисов

```bash
make health
```

Команда проверяет опубликованные API LiteLLM и Analyzer. Состояние Redis и баз
смотрите через `docker compose ps`; для экспериментального профиля отдельно
проверьте `curl -fsS http://localhost:2455/health/ready`. В корпоративной ветке
этот запрос проходит через общий Nginx и одновременно проверяет его маршрут к
`codex-lb`.

| Сервис | Проверка | Рабочее состояние |
| --- | --- | --- |
| LiteLLM | `GET /health/liveliness` | HTTP 200 без API-ключа и вызова модели |
| Analyzer | `GET /api/v1/health` | `status=ok`, `ner_state=ready`, `ner_warmed_up=true` |
| PostgreSQL | `pg_isready` | Принимает соединения |
| Redis | `redis-cli ping` | `PONG` |
| `codex-lb` | `GET /health/ready` | HTTP 200; база и обязательные компоненты готовы |
| PostgreSQL `codex-lb` | `pg_isready -U codex_lb -d codex_lb` | Принимает соединения |

`GET /health` LiteLLM может обращаться к моделям и не используется как проверка
живости. Health Analyzer также возвращает ревизию NER и снимок `capacity`.

Перегрузка Analyzer возвращает `503 analyzer_overloaded`. Событие
`pii_guardrail_analyzer_overloaded` и метрика
`ru_pii_guardrail_fail_closed_total{operation="analyzer_overloaded"}` означают,
что запрос остановлен. Перегрузка всегда приводит к fail-closed независимо от
`PII_GUARDRAIL_FAILURE_MODE`; увеличение числа процессов требует отдельного
бюджета памяти для каждого экземпляра BERT.

Ошибка NER защёлкивает `ner_state=failed`; Analyzer возвращает `503
required_ner_unavailable` до перезапуска и не переходит в режим только с
регулярными распознавателями.

## Prometheus

LiteLLM настроен так:

```yaml
litellm_settings:
  callbacks: [prometheus]
  require_auth_for_metrics_endpoint: false
  drop_params: true
```

Маршруты:

- LiteLLM: `http://localhost:4000/metrics`;
- Analyzer: `http://localhost:5001/metrics`;
- `codex-lb`: `http://localhost:9090/metrics` в экспериментальном профиле.

Эти маршруты открыты без клиентского или служебного ключа. В промышленной среде
разрешайте доступ только Prometheus через сервисную сеть, межсетевой экран или
сетевую политику.

```yaml
scrape_configs:
  - job_name: ru-llm-proxy
    static_configs:
      - targets: ["ru-llm-proxy:4000"]
  - job_name: presidio-analyzer
    static_configs:
      - targets: ["presidio-analyzer:5001"]
  - job_name: codex-lb
    static_configs:
      - targets: ["codex-lb:9090"]
```

Последнюю задачу сбора добавляйте только при включённом профиле `codex-lb`.
Prometheus должен быть подключён к сети `codex-lb-proxy`; иначе используйте
опубликованный порт `9090` на внутреннем адресе сервера. К сети базы
`codex-lb-database` Prometheus не подключайте.

Локальная проверка:

```bash
make metrics
make monitor-smoke
```

Эти команды проверяют LiteLLM, защитный слой и Analyzer. Метрики `codex-lb`
проверяйте отдельным запросом к `http://localhost:9090/metrics`.

### LiteLLM и маршрутизация

| Метрика | Назначение |
| --- | --- |
| `litellm_proxy_total_requests_metric_total` | Входящие запросы |
| `litellm_proxy_failed_requests_metric_total` | Ошибки прокси |
| `litellm_deployment_total_requests_total` | Нагрузка по провайдерам моделей |
| `litellm_deployment_success_responses_total` | Успешные ответы провайдеров |
| `litellm_deployment_failure_responses_total` | Ошибки провайдеров |
| `litellm_request_total_latency_metric_*` | Полная задержка прокси |
| `litellm_llm_api_latency_metric_*` | Задержка провайдера |
| `litellm_total_tokens_metric_total` | Токены |
| `litellm_callback_logging_failures_metric_total` | Ошибки наблюдаемости LiteLLM |

`make routing-smoke` отправляет два запроса одним ключом и сравнивает
`x-litellm-model-id`. Для постоянной синтетической проверки используйте отдельный
пользовательский ключ. Рост ошибок по одному провайдеру модели может законно
привести к выбору другого; `model_info.id` должны оставаться стабильными.

### codex-lb

Штатные метрики `codex-lb` дополняют, но не заменяют метрики LiteLLM:

| Метрика | Назначение |
| --- | --- |
| `codex_lb_requests_total` | HTTP-запросы по маршруту и статусу |
| `codex_lb_request_duration_seconds_*` | Задержка API `codex-lb` |
| `codex_lb_upstream_requests_total` | Результаты вызовов по внутреннему идентификатору подписки |
| `codex_lb_upstream_request_duration_seconds_*` | Задержка OpenAI |
| `codex_lb_active_connections` | Активные соединения |
| `codex_lb_rate_limit_hits_total` | Ограничения частоты и квоты |
| `codex_lb_circuit_breaker_state` | Состояние автоматического выключателя зависимостей |
| `codex_lb_accounts_total` | Число подписок по состоянию |

Набор метрик может меняться между выпусками, поэтому после обновления проверяйте
живой `/metrics` и панели. Метка `account_id` является внутренним техническим
идентификатором; не связывайте её с адресом электронной почты в Prometheus.

### Защитный обработчик

| Метрика | Метки | Назначение |
| --- | --- | --- |
| `ru_pii_guardrail_pre_calls_total` | `result` | Итог проверок до провайдера |
| `ru_pii_guardrail_post_calls_total` | `result` | Итог восстановления |
| `ru_pii_guardrail_entities_detected_total` | `entity_type` | Найденные сущности |
| `ru_pii_guardrail_blocked_total` | `entity_type` | Блокировки PII |
| `ru_pii_guardrail_fail_open_total` | `operation` | Небезопасное продолжение после ошибки |
| `ru_pii_guardrail_fail_closed_total` | `operation` | Остановленные после ошибки запросы |
| `ru_pii_guardrail_analyzer_latency_seconds_*` | нет | HTTP-задержка Analyzer |
| `ru_pii_guardrail_redis_latency_seconds_*` | `operation` | Задержка Redis |
| `ru_pii_guardrail_mapping_size_*` | нет | Размер PII-сопоставления |
| `ru_dictionary_substitution_applied_total` | `rule_id` | Словарные подстановки |
| `ru_dictionary_substitution_mapping_size_*` | нет | Размер словарного сопоставления |
| `ru_synthetic_pii_allowlist_hits_total` | `rule_id`, `entity_type` | Разрешённые синтетические PII |
| `ru_regulated_topic_policy_blocked_total` | `category`, `rule_id` | Блокировки AML/CFT / ПОД/ФТ |
| `ru_pre_egress_policy_blocked_total` | `category` | Блокировки исходных конфигураций и журналов |
| `ru_final_payload_leak_check_blocked_total` | `rule_id` | Блокировки финальной проверки |

Метрики появляются после первого подходящего запроса. Они не содержат исходный
текст, значения, смещения или служебные метки.

LiteLLM создаёт отдельные экземпляры защитного обработчика для стадий до и после
вызова провайдера, но все они используют одни процессные метрики. После запроса с
маскированием должны появиться как минимум `ru_pii_guardrail_pre_calls_total`,
`ru_pii_guardrail_post_calls_total` и
`ru_pii_guardrail_analyzer_latency_seconds_count`. Их отсутствие при наличии
трафика означает неисправность регистрации метрик; проверьте журналы запуска на
`Prometheus metric registration conflict`.

### Presidio Analyzer

| Метрика | Метки | Назначение |
| --- | --- | --- |
| `ru_presidio_analyzer_requests_total` | `outcome` | Итог запросов Analyzer |
| `ru_presidio_analyzer_latency_seconds_*` | `outcome` | Полная задержка внутри Analyzer |
| `ru_presidio_analyzer_entities_detected_total` | `entity_type` | Найденные сущности |
| `ru_presidio_analyzer_capacity_rejections_total` | `reason` | Переполнение очереди и таймаут |
| `ru_presidio_analyzer_failures_total` | `reason` | Ограниченные причины ошибок |
| `ru_presidio_analyzer_ner_failures_total` | `phase`, `failure_class` | Отказы обязательной NER |
| `ru_presidio_analyzer_ner_inference_total` | `outcome` | Итог попыток NER |
| `ru_presidio_analyzer_ner_inference_duration_seconds_*` | `outcome` | Время NER без очереди |
| `ru_presidio_analyzer_ner_windows_processed_*` | `outcome` | Полностью обработанные окна |
| `ru_presidio_analyzer_merge_decisions_total` | `reason`, `winner_source`, `loser_source` | Разрешение пересечений детекторов |

`ru_pii_guardrail_analyzer_latency_seconds_*` измеряет весь HTTP-вызов из
LiteLLM, а `ru_presidio_analyzer_latency_seconds_*` — обработку в Analyzer вместе
с ожиданием его локальной очереди. Метки имеют ограниченный набор значений и не
содержат исходные данные.

`window_boundary_unresolved` в `ru_presidio_analyzer_ner_failures_total`
означает безопасный отказ одного запроса после повторной проверки границы;
проверка состояния NER остаётся `ready`. `forward_pass_failed`, ошибки загрузки и
прогрева означают отказ обязательной модели и переводят Analyzer в `unhealthy`.
Дополнительные окна входят в `ru_presidio_analyzer_ner_windows_processed_*`.

## Политики в мониторинге

- Словарные правила при `DICTIONARY_SUBSTITUTIONS_ENABLED=true` загружаются из
  `dictionary-substitutions.default.json`; контролируйте их по
  `dictionary_substitution_applied` и
  `ru_dictionary_substitution_applied_total`. Исходные фразы и замены не
  записываются.
- При `SYNTHETIC_PII_ALLOWLIST_MODE=allow` правила из
  `SYNTHETIC_PII_ALLOWLIST_JSON` фиксируются событием
  `synthetic_pii_allowlist_applied` и метрикой
  `ru_synthetic_pii_allowlist_hits_total`. В промышленном трафике synthetic/test
  исключения требуют явного согласования; исходные значения не записываются.
- При `REGULATED_TOPIC_POLICY_MODE=block` событие
  `regulated_topic_policy_blocked` и метрика
  `ru_regulated_topic_policy_blocked_total` содержат только категории и правила
  AML/CFT / ПОД/ФТ. Политика не является распознавателем персональных данных и
  не пишет исходный запрос.
- При `PRE_EGRESS_POLICY_MODE=block` событие `pre_egress_policy_blocked`
  означает, что исходная полезная нагрузка остановлена до Analyzer и провайдера.
- При `FINAL_PAYLOAD_LEAK_CHECK_MODE=block` событие
  `final_payload_leak_check_blocked` и метрика
  `ru_final_payload_leak_check_blocked_total` фиксируют итоговую блокировку.
  Значения `FINAL_PAYLOAD_LEAK_CHECK_CANARIES` в телеметрию не попадают.

## Рекомендуемые оповещения

Порог задержки уточните после измерений на целевом оборудовании.

```promql
increase(ru_pii_guardrail_fail_open_total[5m]) > 0
increase(ru_pii_guardrail_fail_closed_total[5m]) > 0
increase(ru_pii_guardrail_fail_closed_total{operation="analyzer_overloaded"}[5m]) > 0
sum(rate(ru_pii_guardrail_pre_calls_total{result="error"}[5m])) > 0
sum(rate(ru_presidio_analyzer_requests_total{outcome=~"overload|timeout_or_cancelled|analyzer_error"}[5m])) > 0
increase(ru_presidio_analyzer_ner_failures_total[5m]) > 0
sum(rate(ru_presidio_analyzer_ner_inference_total{outcome=~"failure|unavailable"}[5m])) > 0
histogram_quantile(0.95, sum(rate(ru_presidio_analyzer_latency_seconds_bucket[5m])) by (le)) > 2
sum(rate(litellm_proxy_failed_requests_metric_total[5m])) > 0
```

События политик обычно не являются отказом сервиса, но нужны на панели мониторинга:

```promql
sum(rate(ru_pii_guardrail_blocked_total[5m])) > 0
sum(rate(ru_regulated_topic_policy_blocked_total[5m])) > 0
sum(rate(ru_synthetic_pii_allowlist_hits_total[5m])) > 0
sum(rate(ru_pre_egress_policy_blocked_total[5m])) > 0
sum(rate(ru_final_payload_leak_check_blocked_total[5m])) > 0
```

## Структурированные журналы

Основной контракт решения на уровне шлюза — `gateway_guardrail_audit`. Он
содержит `request_id`, `model`, `status`, `latency_ms`, `guardrail_mode`,
`call_type`, `policy_mode`, `policy_result`, `redaction_count`, `entity_counts`,
а при ошибке — `block_reason`, `error_code` и ограниченные правила/категории.

Analyzer пишет `presidio_analyzer_request` с `outcome`, `latency_ms`,
`entity_count`, `entity_counts`, `language`, `score_threshold`, `ner` и
`capacity`. Жизненный цикл модели отражают `presidio_ner_startup_begin`,
`presidio_ner_startup_ready`, `presidio_ner_startup_failed`; каждая попытка NER
пишет `presidio_ner_inference` с `outcome`, `duration_ms` и `windows_processed`.

| Событие | Уровень | Назначение |
| --- | --- | --- |
| `gateway_guardrail_audit` | `INFO` | Одно безопасное решение до вызова модели |
| `pii_guardrail_masked`, `pii_guardrail_blocked` | `INFO` | Маскирование или блокировка PII |
| `dictionary_substitution_applied` | `INFO` | Словарная замена |
| `synthetic_pii_allowlist_applied` | `INFO` | Исключение синтетического значения |
| `regulated_topic_policy_blocked` | `INFO` | Блокировка регулируемой темы |
| `pre_egress_policy_blocked` | `INFO` | Блокировка конфигурации или журнала |
| `final_payload_leak_check_blocked` | `INFO` | Блокировка итоговой нагрузки |
| `pii_guardrail_restored`, `pii_guardrail_stream_restored` | `INFO` | Восстановление ответа |
| `pii_guardrail_unsupported_response` | `WARNING` | Неизвестный непотоковый формат ответа; сопоставление удаляется без восстановления |
| `pii_guardrail_cleanup_failed` | `WARNING` | Redis не удалил сопоставление; запись ограничена настроенным TTL |
| `pii_guardrail_failed_open`, `pii_guardrail_failed_closed` | `ERROR` | Ошибка зависимости |
| `pii_guardrail_analyzer_overloaded` | `ERROR` | Перегрузка Analyzer |
| `presidio_analyzer_request`, `presidio_ner_inference` | `INFO` | Обработка в Analyzer |
| `presidio_ner_startup_failed` | `CRITICAL` | Модель не готова |

Во всех событиях запрещены исходный запрос, найденные значения, смещения,
API-ключи, токены и непрозрачное состояние Responses API из
`reasoning/compaction.encrypted_content`. `request_id` не используйте как метку
Prometheus.

Аудит клиентских запросов и аудит административных действий ведутся отдельно.
Для действий администраторов собирайте доступные LiteLLM audit logs, журналы
SSO/обратного прокси для `/ui` и Admin API, штатные журналы административных
действий `codex-lb`, а также историю GitOps. Границы и роли описаны в
[admin-access.md](admin-access.md).

## Guardrails Monitor и проверка обработчиков

`guardrail_info` возвращается через `GET /guardrails/list`; интерфейс LiteLLM
может не показывать его произвольные поля. Для фактического мониторинга
используйте метрики и журналы.

Параметр `entities` в ответе содержит полный контракт типов, которые умеет
обрабатывать Analyzer. Это описание возможностей, а не статистика конкретных
запросов.

```bash
make guardrails-list
make guardrails-smoke
```

`guardrails-smoke` предназначен для локального Docker Compose. Он проверяет
обычный и потоковый запрос, `x-litellm-applied-guardrails`, SSE до `[DONE]` и
отсутствие принадлежащих проверке Redis-сопоставлений после ответа. Очистка
проверяется через `docker compose exec -T redis`; таймауты задают
`CURL_CONNECT_TIMEOUT` и `CURL_MAX_TIME`.

## Клиенты зависимостей

Защитный обработчик переиспользует Redis- и HTTPX-клиенты в пределах процесса и
цикла событий. Рост задержки может означать как медленную зависимость, так и
насыщение пула. Настройки `PII_GUARDRAIL_REDIS_MAX_CONNECTIONS`,
`PII_GUARDRAIL_ANALYZER_MAX_CONNECTIONS` и остальные лимиты перечислены в
[configuration.md](configuration.md). `close_guardrail_dependency_clients()`
закрывает пулы во встраиваемом запуске.

## Исходящие соединения

Собирайте решения CNI, шлюза исходящего трафика или firewall для:

- новых внешних FQDN из `litellm`;
- обращений `codex-lb` к разрешённым конечным точкам OpenAI и неожиданных
  направлений из этого контейнера;
- попыток интернет-доступа из Analyzer, Redis или PostgreSQL;
- изменения DNS-направлений после обновления LiteLLM или провайдера.

Сетевые журналы должны содержать только сервис, адрес назначения, порт и решение
политики. Регламент: [docs/egress-controls.md](egress-controls.md); шаблоны:
[`deploy/kubernetes/egress`](../deploy/kubernetes/egress).

## Обновление LiteLLM

LiteLLM запускается из готового образа, поэтому весь проект пересобирать не
нужно:

```bash
make update-litellm
```

В промышленной среде после стендовой проверки фиксируйте тег или digest образа.
Перед обновлением сохраните PostgreSQL, где находятся пользователи, ключи,
бюджеты и статистика.

Минимальный проверочный список обновления:

1. Зафиксировать текущий digest: `docker compose images litellm`.
2. Сделать резервную копию PostgreSQL.
3. Выполнить `make update-litellm`.
4. Проверить `make health`.
5. Проверить `make guardrails-list`.
6. Проверить `make guardrails-smoke` в локальном окружении Docker Compose.
7. Проверить `make test-final-leak-proxy`.
8. Проверить `make routing-smoke`.
9. Выполнить `make monitor-smoke` и запрос с PII.
10. Если есть регрессия, вернуть прежний тег или digest и пересоздать `litellm`.

Изменения `presidio/Dockerfile` или зависимостей Analyzer требуют `make build`;
изменения `.env` или `litellm-config.yaml` — пересоздания соответствующего
сервиса.

`codex-lb` закреплён отдельно и не обновляется командой `make update-litellm`.
Его обновление, резервное копирование и откат описаны в
[codex-lb.md](codex-lb.md#обновление).

## Ссылки

- [Prometheus в LiteLLM](https://docs.litellm.ai/docs/proxy/prometheus)
- [Маршрутизация LiteLLM](https://docs.litellm.ai/docs/proxy/load_balancing)
- [Защитные обработчики LiteLLM](https://docs.litellm.ai/docs/proxy/guardrails/custom_guardrail)
- [Журналирование LiteLLM](https://docs.litellm.ai/docs/proxy/logging)
- [Наблюдаемость codex-lb](https://soju06.github.io/codex-lb/deployment/kubernetes/#observability)
