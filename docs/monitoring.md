# Мониторинг и эксплуатация

Документ описывает, как мониторить `ru-llm-proxy` в окружении, близком к промышленному, и как обновлять LiteLLM без полной пересборки проекта.

## Цели мониторинга

Мониторинг должен отвечать на ключевые вопросы:

- жив ли LiteLLM-прокси и принимает ли он запросы;
- доступен ли Presidio Analyzer и загружен ли DeepPavlov NER;
- доступны ли Redis и PostgreSQL;
- применяются ли защитные слои персональных данных и сколько сущностей они маскируют;
- применяются ли правила списка разрешённых синтетических персональных данных и не используются ли они вне ожидаемых тестовых контуров;
- включена ли политика регулируемых тем для AML/CFT / ПОД/ФТ и какие ограниченные идентификаторы правил она блокирует;
- стабильно ли клиенты попадают к своим провайдерам моделей при закреплении маршрута;
- защищён ли административный интерфейс/API отдельной операторской границей и аудируются ли административные действия;
- есть ли fail-open/fail-closed события, при которых PII-защита работает нештатно.

Административный интерфейс LiteLLM полезен для операционных действий, ключей, просмотра использования, расходов и журналов. Он не должен быть единственным источником наблюдаемости для защитных слоёв.

Модель административного доступа, граница SSO/reverse-proxy, ротация и
break-glass процедуры описаны отдельно: [admin-access.md](admin-access.md).
Сгруппированный справочник переменных окружения и промышленных значений по умолчанию:
[configuration.md](configuration.md).

## Контуры доказательной проверки

Подтверждения безопасности и наблюдаемости проверяются разными контурами:

| Контур | Команда | Назначение |
| --- | --- | --- |
| Безопасность исходящего запроса | `make test-egress-security` | Имитация провайдера и захват отправки: исходные тестовые значения не должны попасть в полезную нагрузку перед провайдером, а заблокированные запросы, включая `regulated_topic_policy_blocked`, не должны создавать запрос к провайдеру. |
| Наблюдаемость | `make test-observability-gates` | Лёгкие проверки связки аудита/журналирования и подтверждение, что контуры быстрых проверок не смешивают статус наблюдаемости со статусом исходящего запроса. |
| Проверка с живым провайдером | `make guardrails-smoke`, `make test-e2e`, `make routing-smoke` | Проверка реального потока LiteLLM/провайдера; быстрый запрос к живому провайдеру не доказывает отсутствие утечки, потому что полезная нагрузка внешнему провайдеру не захватывается. |

Подробная карта трассируемости для проверок и ручных артефактов: [docs/compliance.md](compliance.md).
Промышленные исходящие сетевые ограничения по принципу «запрещено всё, кроме явно разрешённого» и шаблоны Kubernetes/Cilium описаны
отдельно: [docs/egress-controls.md](egress-controls.md) и
[deploy/kubernetes/egress](../deploy/kubernetes/egress).

## Проверки здоровья

Проверка со стороны хоста:

```bash
make health
```

Что проверяется:

| Цель | Маршрут / команда | Назначение |
| --- | --- | --- |
| LiteLLM | `GET /health/liveliness` | Живость процесса без API-токена и без реального вызова LLM |
| Presidio Analyzer | `GET /api/v1/health` | Доступность analyzer и статус NER |
| PostgreSQL | `pg_isready` | Доступность базы данных LiteLLM |
| Redis | `redis-cli ping` | Доступность временного хранилища сопоставлений персональных данных и закрепления маршрутов LiteLLM |

`GET /health` у LiteLLM не используется для проверки состояния Docker, потому что этот маршрут предназначен для проверки моделей и может делать реальные вызовы LLM API.

## Prometheus

В `litellm-config.yaml` включён Prometheus callback:

```yaml
litellm_settings:
  callbacks:
    - prometheus
  require_auth_for_metrics_endpoint: false
  drop_params: true
```

После перезапуска LiteLLM метрики доступны на:

```text
http://localhost:4000/metrics
```

В конфигурации проекта по умолчанию маршрут LiteLLM `/metrics` открыт без
прикладной авторизации, чтобы Prometheus мог собирать метрики без LiteLLM API
ключа. В промышленной среде доступ к нему нужно ограничивать сетевыми средствами:
закрытой сервисной сетью, ingress/network policy, firewall или отдельным
маршрутом сбора только для Prometheus. Метрики не должны содержать исходные
запросы или значения секретов, но могут включать служебные метки вроде имени
модели, имени пользовательского ключа и хэша ключа, поэтому маршрут не должен быть
доступен из публичной сети.

Presidio Analyzer отдает собственные низкокардинальные метрики отдельно:

```text
http://localhost:5001/metrics
```

Локальная проверка:

```bash
make metrics
make monitor-smoke
```

Пример конфигурации сбора для Prometheus внутри той же Docker/сетевой инфраструктуры:

```yaml
scrape_configs:
  - job_name: ru-llm-proxy
    metrics_path: /metrics
    static_configs:
      - targets:
          - ru-llm-proxy:4000
  - job_name: presidio-analyzer
    metrics_path: /metrics
    static_configs:
      - targets:
          - presidio-analyzer:5001
```

Если Prometheus работает снаружи хоста Docker Compose, используйте опубликованный адрес прокси, например `host.example.com:4000`.

Текущий Compose запускает один процесс LiteLLM. Если в промышленной среде вы включите несколько рабочих процессов, настройте `PROMETHEUS_MULTIPROC_DIR` для корректной агрегации метрик Prometheus-клиента между рабочими процессами.

## Метрики LiteLLM

LiteLLM отдаёт стандартные метрики прокси, вызовов провайдера, задержек, использования токенов, расходов, пользовательских ключей и ошибок обратных вызовов. Основные семейства метрик для дашборда:

| Метрика | Что показывает |
| --- | --- |
| `litellm_proxy_total_requests_metric_total` | Входящие запросы к прокси |
| `litellm_proxy_failed_requests_metric_total` | Ошибки на уровне прокси |
| `litellm_deployment_total_requests_total` | Вызовы провайдера модели |
| `litellm_deployment_success_responses_total` | Успешные ответы провайдера |
| `litellm_deployment_failure_responses_total` | Ошибки провайдера |
| `litellm_request_total_latency_metric_*` | Полная задержка запроса через прокси |
| `litellm_llm_api_latency_metric_*` | Задержка внешнего LLM API |
| `litellm_total_tokens_metric_total` | Использование токенов |
| `litellm_callback_logging_failures_metric_total` | Ошибки доставки обратных вызовов наблюдаемости |

Точный набор меток зависит от версии LiteLLM и настроек пользовательских ключей / команд.

## Наблюдаемость маршрутизации

Закрепление маршрута включено через LiteLLM Router `deployment_affinity`. Он сохраняет в Redis сопоставление между хэшем клиентского ключа LiteLLM и `model_info.id` провайдера модели.

Проверка вручную:

```bash
make routing-smoke
```

Команда делает два запроса одним ключом и сравнивает заголовок ответа `x-litellm-model-id`. Для проверки пользовательского ключа задайте `LITELLM_ROUTING_TEST_KEY` в `.env`; иначе используется `LITELLM_MASTER_KEY`.

Что отдавать в мониторинг DevOps-команде:

- заголовок ответа `x-litellm-model-id` полезен для разовой диагностики и синтетических проверок;
- `litellm_deployment_total_requests_total`, `litellm_deployment_success_responses_total` и `litellm_deployment_failure_responses_total` показывают нагрузку и ошибки по провайдерам моделей;
- `model_info.id` должен быть стабильным, иначе старые сопоставления закрепления и метки дашбордов потеряют смысл;
- Redis должен мониториться как зависимость не только защитного слоя персональных данных, но и закрепления маршрутов.

Если в одной группе моделей несколько провайдеров моделей, синтетическая проверка может запускаться с отдельным пользовательским ключом и проверять, что два последовательных запроса получают один и тот же `x-litellm-model-id`. При недоступности закреплённого провайдера модели LiteLLM имеет право выбрать другого доступного провайдера модели, поэтому алерт должен учитывать состояние провайдеров моделей.

## Метрики защитного слоя персональных данных

Проект добавляет собственные низкокардинальные метрики. Они не содержат пользовательский текст, идентификатор запроса, персональные данные, исходный найденный текст или исходные плейсхолдеры.

Словарные подстановки включены по умолчанию через `DICTIONARY_SUBSTITUTIONS_ENABLED=true` и файл `dictionary-substitutions.default.json`. Если оператор подменяет `DICTIONARY_SUBSTITUTIONS_FILE` или задаёт `DICTIONARY_SUBSTITUTIONS_JSON`, мониторинг должен отслеживать всплески `dictionary_substitution_failed_closed`, `ru_dictionary_substitution_applied_total` по новым `rule_id` и отсутствие исходных значений/замен в журналах.

| Метрика | Тип | Метки | Назначение |
| --- | --- | --- | --- |
| `ru_pii_guardrail_pre_calls_total` | Counter | `result` | Итог проверки до вызова модели: `masked`, `blocked`, `regulated_topic_policy_blocked`, `pre_egress_policy_blocked`, `final_payload_leak_check_blocked`, `clean`, `skipped`, `error` |
| `ru_pii_guardrail_post_calls_total` | Counter | `result` | Итог восстановления после ответа: `restored`, `no_placeholders`, `no_mapping`, `skipped`, `unsupported_response`, `error` |
| `ru_pii_guardrail_entities_detected_total` | Counter | `entity_type` | Количество замаскированных сущностей по типам |
| `ru_pii_guardrail_blocked_total` | Counter | `entity_type` | Количество заблокированных сущностей по типам в `PII_GUARDRAIL_MODE=block` |
| `ru_regulated_topic_policy_blocked_total` | Counter | `category`, `rule_id` | Количество блокировок регулируемых тем AML/CFT / ПОД/ФТ по ограниченным категориям и идентификаторам правил |
| `ru_synthetic_pii_allowlist_hits_total` | Counter | `rule_id`, `entity_type` | Количество срабатываний списка разрешённых синтетических персональных данных по ограниченному идентификатору правила и типу сущности; исходные разрешённые значения не являются метками |
| `ru_dictionary_substitution_applied_total` | Counter | `rule_id` | Количество применённых словарных подстановок по ограниченному идентификатору правила; исходные значения/замены не являются метками |
| `ru_pre_egress_policy_blocked_total` | Counter | `category` | Количество блокировок конфигураций/журналов по ограниченным категориям |
| `ru_final_payload_leak_check_blocked_total` | Counter | `rule_id` | Количество блокировок финальной проверки утечки перед провайдером по ограниченным идентификаторам правил |
| `ru_pii_guardrail_fail_open_total` | Counter | `operation` | Ошибки, после которых запрос продолжен в режиме `fail_open` |
| `ru_pii_guardrail_fail_closed_total` | Counter | `operation` | Ошибки, после которых запрос остановлен в режиме `fail_closed` |
| `ru_pii_guardrail_analyzer_latency_seconds_*` | Histogram | нет | Задержка вызовов Presidio Analyzer |
| `ru_pii_guardrail_redis_latency_seconds_*` | Histogram | `operation` | Задержка Redis-операций `save`, `load`, `delete` |
| `ru_pii_guardrail_mapping_size_*` | Histogram | нет | Количество сопоставлений плейсхолдеров на маскированный запрос |
| `ru_dictionary_substitution_mapping_size_*` | Histogram | нет | Количество обратимых словарных сопоставлений на запрос с подстановками |

Метрики защитного слоя персональных данных и словарных подстановок появятся в `/metrics` после первого запроса, который прошёл через защитный слой.

## Метрики Presidio Analyzer

Метрики Analyzer появляются на `http://localhost:5001/metrics` после первых
`POST /api/v1/analyze` запросов. Они не содержат исходный входной текст,
исходные значения сущностей, смещения, API-ключи или токены прокси.

| Метрика | Тип | Метки | Назначение |
| --- | --- | --- | --- |
| `ru_presidio_analyzer_requests_total` | Counter | `outcome` | Итог запроса Analyzer: `success`, `no_entities`, `overload`, `timeout_or_cancelled`, `analyzer_error` |
| `ru_presidio_analyzer_latency_seconds_*` | Histogram | `outcome` | Задержка запроса Analyzer по безопасному результату |
| `ru_presidio_analyzer_entities_detected_total` | Counter | `entity_type` | Количество найденных сущностей по типам без исходных значений |
| `ru_presidio_analyzer_capacity_rejections_total` | Counter | `reason` | Отказы по ёмкости: `queue_full`, `queue_timeout` |
| `ru_presidio_analyzer_failures_total` | Counter | `reason` | Отмены/ошибки по ограниченной причине, например `cancelled` или класс исключения |

Эти метрики дополняют guardrail-side
`ru_pii_guardrail_analyzer_latency_seconds_*`: метрика защитного слоя измеряет HTTP-вызов
из LiteLLM к Analyzer, а `ru_presidio_analyzer_latency_seconds_*` измеряет
обработку внутри сервиса Analyzer вместе с ожиданием свободного места.

## Лимиты клиентов зависимостей защитного слоя

Защитный слой LiteLLM переиспользует Redis и HTTP-клиенты Analyzer между экземплярами до вызова модели и после ответа внутри одного процесса/цикла событий. Для мониторинга это означает, что рост задержки в `ru_pii_guardrail_analyzer_latency_seconds_*` или `ru_pii_guardrail_redis_latency_seconds_*` может быть связан не только с самим Analyzer/Redis, но и с ожиданием свободного соединения в общем пуле клиентов.

Основные ручки:

| Переменная | По умолчанию | Что смотреть |
| --- | --- | --- |
| `PII_GUARDRAIL_REDIS_MAX_CONNECTIONS` | `20` | Насыщение пула Redis, рост `ru_pii_guardrail_redis_latency_seconds_*`, количество соединений на сервере Redis. |
| `PII_GUARDRAIL_REDIS_SOCKET_CONNECT_TIMEOUT_SECONDS` | `1.0` | События fail-open/fail-closed `mapping_save`, `mapping_load`, `mapping_delete` при сетевых проблемах Redis. |
| `PII_GUARDRAIL_REDIS_SOCKET_TIMEOUT_SECONDS` | `2.0` | Таймаут Redis-операций и рост гистограммы задержки Redis. |
| `PII_GUARDRAIL_ANALYZER_TIMEOUT_SECONDS` | `30.0` | Долгие вызовы Analyzer, `ru_pii_guardrail_analyzer_latency_seconds_*`, fail-open/fail-closed `masking`. |
| `PII_GUARDRAIL_ANALYZER_CONNECT_TIMEOUT_SECONDS` | `5.0` | Ошибки подключения к контейнеру/сервису Analyzer. |
| `PII_GUARDRAIL_ANALYZER_MAX_CONNECTIONS` | `20` | Насыщение пула HTTP-клиента к Analyzer на один процесс/цикл событий LiteLLM. |
| `PII_GUARDRAIL_ANALYZER_MAX_KEEPALIVE_CONNECTIONS` | `10` | Стабильность повторного использования постоянных соединений при регулярной нагрузке. |

Эти лимиты не заменяют ограничитель ёмкости Analyzer. Если `analyzer_overloaded` растёт, сначала смотрите ёмкость `PRESIDIO_ANALYZER_*` и бюджет памяти модели; если задержка растёт без `analyzer_overloaded`, проверяйте лимиты пулов, сеть и состояние Redis/Analyzer.

Для аккуратного завершения общие клиенты закрываются через `close_guardrail_dependency_clients()`. Если защитный слой запускается вне стандартного процесса Docker Compose или в тестовой обвязке, завершение должно вызывать этот помощник, чтобы транспорты HTTPX и пулы соединений Redis не оставались открытыми после очистки кэшей.

## Рекомендуемые алерты

Базовые условия алертов:

```promql
increase(ru_pii_guardrail_fail_open_total[5m]) > 0
```

Есть событие fail-open: Presidio или Redis не сработали, а запрос был пропущен дальше.

```promql
increase(ru_pii_guardrail_fail_closed_total[5m]) > 0
```

Есть событие fail-closed: запрос был остановлен защитным слоем.

```promql
increase(ru_pii_guardrail_fail_closed_total{operation="analyzer_overloaded"}[5m]) > 0
```

Ограничитель ёмкости Analyzer отклонил запрос, и защитный слой остановил его как принудительный fail-closed.

```promql
sum(rate(ru_pii_guardrail_pre_calls_total{result="error"}[5m])) > 0
```

Ошибки обработки до вызова модели.

```promql
sum(rate(ru_pii_guardrail_pre_calls_total{result="blocked"}[5m])) > 0
```

Режим блокировки отклоняет запросы с персональными данными до вызова провайдера. Это ожидаемое событие политики, но его стоит мониторить как телеметрию безопасности.

```promql
sum(rate(ru_regulated_topic_policy_blocked_total[5m])) > 0
```

Политика регулируемых тем блокирует AML/CFT / ПОД/ФТ или похожие внутренние комплаенс-темы до Analyzer и выхода к провайдеру. Это ожидаемое событие политики при `REGULATED_TOPIC_POLICY_MODE=block`, но его стоит мониторить отдельно от персональных данных и блокировок конфигураций/журналов.

```promql
sum(rate(ru_synthetic_pii_allowlist_hits_total[5m])) > 0
```

Список разрешённых синтетических персональных данных применяется к запросам. Это ожидаемо для быстрых проверок, демонстраций и тестовых контуров, но в промышленном трафике должно быть явно согласовано: список разрешённых значений не предназначен для пропуска реальных персональных данных.

```promql
histogram_quantile(0.95, sum(rate(ru_pii_guardrail_analyzer_latency_seconds_bucket[5m])) by (le)) > 2
```

Задержка p95 Presidio Analyzer выше 2 секунд.

```promql
sum(rate(ru_presidio_analyzer_requests_total{outcome=~"overload|timeout_or_cancelled|analyzer_error"}[5m])) > 0
```

Analyzer фиксирует перегрузку, отмену или внутреннюю ошибку на стороне сервиса.

```promql
histogram_quantile(0.95, sum(rate(ru_presidio_analyzer_latency_seconds_bucket[5m])) by (le)) > 2
```

Задержка p95 сервиса Analyzer выше 2 секунд.

```promql
sum(rate(litellm_proxy_failed_requests_metric_total[5m])) > 0
```

Ошибки на уровне LiteLLM-прокси.

Для состояния Analyzer отдельно проверьте `GET /api/v1/health`. Промышленное значение по умолчанию — `DEEPPAVLOV_NER_REQUIRED=true`: если DeepPavlov не загрузился, Analyzer не должен стартовать, а проверка состояния не должна проходить. Поле `ner` должно быть `loaded`; `not_loaded` означает потерю DeepPavlov-детекции `PERSON`, `LOCATION` и `ORGANIZATION`. Только при явном `DEEPPAVLOV_NER_REQUIRED=false` сервис может ответить `status=degraded`, `ner=not_loaded`, `ner_required=false`. Поле `capacity` показывает локальный для процесса ограничитель: `active`, `waiting`, `concurrency_limit`, `queue_limit` и `queue_timeout_seconds`.

Перегрузка Analyzer возвращает `503` с `detail.code=analyzer_overloaded` и причиной `queue_full` или `queue_timeout`. Для защитного слоя LiteLLM это принудительный fail-closed независимо от `PII_GUARDRAIL_FAILURE_MODE`: запрос останавливается, чтобы перегрузка Analyzer не отправила исходные персональные данные провайдеру. Если `waiting` часто приближается к `queue_limit`, увеличивайте реплики/рабочие процессы только с учётом памяти: каждый рабочий процесс uvicorn загружает отдельный экземпляр spaCy/DeepPavlov.

## Журналы

Защитный слой пишет структурированные JSON-журналы без текста запроса и без исходных персональных данных.
Поле `request_id` в событиях маскирования, блокировки и восстановления — серверный идентификатор сопоставления персональных данных из `metadata.pii_request_id`, а не клиентский `metadata.request_id`.
Для мониторинга на уровне шлюза используйте событие `gateway_guardrail_audit`: оно
пишется один раз на решение до вызова модели и содержит общий безопасный контракт для
дашбордов и алертов: `request_id`, `model`, `status`, `latency_ms`,
`guardrail_mode`, `call_type`, `policy_mode`, `policy_result`,
`redaction_count`, `entity_counts`, а для блокировок/ошибок также
`block_reason`, `error_code`, ограниченные `categories`/`rules`/`actions` и счётчики.
При `REGULATED_TOPIC_POLICY_MODE=block` событие `regulated_topic_policy_blocked` фиксирует блокировку уверенно распознанных тем AML/CFT / ПОД/ФТ, санкционных проверок, мониторинга транзакций, подозрительной активности или обхода комплаенс-процедур до Analyzer и выхода к провайдеру. Этот слой не является распознавателем персональных данных и не создаёт сопоставление Redis. В журналах остаются только ограниченные категории, идентификаторы правил, действие `block` и счётчики; исходный запрос, исходный найденный текст, фрагменты и смещения не пишутся.
При `PRE_EGRESS_POLICY_MODE=block` событие `pre_egress_policy_blocked` фиксирует блокировку конфигурации/журнала до Analyzer и выхода к провайдеру. Для этого события сопоставление Redis и `metadata.pii_request_id` не создаются, поэтому `request_id` является только серверным корреляционным идентификатором. В журналах остаются только ограниченные категории, идентификаторы правил и счётчики; исходная полезная нагрузка, фрагменты, смещения и значения секретов не пишутся.
При `FINAL_PAYLOAD_LEAK_CHECK_MODE=block` событие `final_payload_leak_check_blocked`
фиксирует детерминированный маркер утечки в уже подготовленном для провайдера тексте после изменений на стороне прокси
и до вызова провайдера. Детерминированные контрольные маркеры конкретной установки задаются
через `FINAL_PAYLOAD_LEAK_CHECK_CANARIES`; в журналах остаются только ограниченные идентификаторы правил
и счётчики. Исходные найденные значения, фрагменты запроса, смещения, ключи провайдеров и содержимое сопоставлений не пишутся.
При `SYNTHETIC_PII_ALLOWLIST_MODE=allow` правила из `SYNTHETIC_PII_ALLOWLIST_JSON` могут вычитать только явно заданные фрагменты синтетических персональных данных из результатов Analyzer. Событие `synthetic_pii_allowlist_applied` фиксирует только ограниченный `rule_id`, тип сущности и счётчики. `gateway_guardrail_audit` дополнительно получает необязательные поля `synthetic_allowlist_rules`, `synthetic_allowlist_entity_counts`, `synthetic_allowlist_rule_counts` и `synthetic_allowlist_hit_count`. Исходные разрешённые значения, фрагменты запроса и смещения не пишутся.

Presidio Analyzer пишет отдельное структурированное JSON-событие
`presidio_analyzer_request` на каждый запрос `/api/v1/analyze`. Событие содержит
`event_id`, `outcome`, `latency_ms`, `entity_count`, `entity_counts`, `language`,
`score_threshold`, `ner`, `capacity` и необязательное `failure_reason`. Оно не содержит
исходный входной текст, исходные значения сущностей, смещения, API-ключи или токены прокси.

Основные события:

| Событие | Уровень | Поля |
| --- | --- | --- |
| `presidio_analyzer_request` | `INFO` | `event_id`, `outcome`, `latency_ms`, `entity_count`, `entity_counts`, `language`, `score_threshold`, `ner`, `capacity`, необязательное `failure_reason` |
| `gateway_guardrail_audit` | `INFO` | `request_id`, `model`, `status`, `latency_ms`, `guardrail_mode`, `call_type`, `policy_mode`, `regulated_topic_policy_mode`, `dictionary_substitutions_enabled`, `policy_result`, `redaction_count`, `entity_counts`, необязательные `dictionary_substitution_rules`, `dictionary_substitution_rule_counts`, `dictionary_substitution_count`, `block_reason`, `error_code`, `categories`, `rules`, `actions`, `category_counts`, `rule_counts`, `failure_operation`, `error_type` |
| `pii_guardrail_masked` | `INFO` | `request_id`, `masked_count`, `entity_counts`, `mapping_ttl_seconds` |
| `dictionary_substitution_applied` | `INFO` | `request_id`, `rules`, `rule_counts`, `substitution_count`, `mapping_size`, `mapping_ttl_seconds` |
| `pii_guardrail_blocked` | `INFO` | `request_id`, `entity_types`, `entity_counts` |
| `synthetic_pii_allowlist_applied` | `INFO` | `request_id`, `rules`, `entity_counts`, `rule_counts`, `hit_count` |
| `regulated_topic_policy_blocked` | `INFO` | `request_id`, `categories`, `rules`, `actions`, `category_counts`, `rule_counts`, `finding_count` |
| `pre_egress_policy_blocked` | `INFO` | `request_id`, `categories`, `rules`, `category_counts`, `finding_count` |
| `final_payload_leak_check_blocked` | `INFO` | `request_id`, `rules`, `rule_counts`, `finding_count` |
| `pii_guardrail_restored` | `INFO` | `request_id`, `mapping_size`, `restored_fields` |
| `pii_guardrail_stream_restored` | `INFO` | `request_id`, `mapping_size`, `restored_fields` |
| `pii_guardrail_no_mapping` | `INFO` | `request_id` |
| `pii_guardrail_failed_open` | `ERROR` | `operation`, `failure_mode`, `error_type` |
| `pii_guardrail_failed_closed` | `ERROR` | `operation`, `failure_mode`, `error_type` |
| `dictionary_substitution_failed_open` | `ERROR` | `operation`, `failure_mode`, `error_type` |
| `dictionary_substitution_failed_closed` | `ERROR` | `operation`, `failure_mode`, `error_type` |
| `dictionary_substitution_config_invalid` | `ERROR` | `failure_mode`, `error_type` |
| `pii_guardrail_analyzer_overloaded` | `ERROR` | `failure_mode`, `reason` |
| `pii_guardrail_cleanup_failed` | `WARNING` | `request_id`, `error_type` |
| `pii_guardrail_unsupported_response` | `WARNING` | `request_id`, `response_type`, `mapping_size` |

DevOps-рекомендации:

- собирать stdout/stderr всех контейнеров через штатный сборщик журналов;
- разбирать `gateway_guardrail_audit` как основной независимый от поставщика источник телеметрии безопасности;
- использовать `policy_result`, `status`, `error_code` и ограниченные `rules`/`categories` для дашбордов; исходный запрос и персональные данные в журналах отсутствуют намеренно;
- собирать аудит административных действий отдельно от аудита защитного слоя на запросах: журналы аудита LiteLLM, где они доступны, журналы reverse-proxy/SSO для `/ui` и административных API-маршрутов, журналы CI первичной настройки с замаскированными секретами и GitOps/тикеты для изменений конфигурации, моделей и провайдеров;
- не включать отладочные журналы внешнего LLM-провайдера в промышленной среде без отдельного анализа приватности;
- хранить `request_id` как идентификатор сопоставления защитного слоя, но не использовать его как метку Prometheus.

## Интерфейс Guardrails

`guardrail_info` в конфигурации LiteLLM возвращается через `GET /guardrails/list`. Текущий интерфейс LiteLLM может показывать список защитных слоёв, но не обязан отображать все произвольные поля `guardrail_info`.

Не открывайте полный административный интерфейс только ради мониторинга защитных слоёв. В промышленной среде
защищайте `/ui` и административные API-маршруты через операторскую границу из
[admin-access.md](admin-access.md) или задайте `DISABLE_ADMIN_UI=True` для
установок только с API.

Для проверки регистрационных метаданных используйте:

```bash
make guardrails-list
```

Для проверки фактического применения защитных слоёв используйте:

```bash
make guardrails-smoke
```

Быстрая проверка предназначена для локального окружения Docker Compose (`make up`): очистка Redis
проверяется через `docker compose exec -T redis`, а `LITELLM_URL` должен указывать на
`localhost`, `127.0.0.1` или `[::1]`. Она выполняет обычный и потоковый
`/v1/chat/completions`, проверяет `x-litellm-applied-guardrails`, SSE-события до
`[DONE]` и отсутствие принадлежащих проверке Redis-сопоставлений с тестовыми персональными данными после завершения
потокового ответа. Таймауты можно переопределить через `CURL_CONNECT_TIMEOUT` и
`CURL_MAX_TIME`. Это контрольная проверка, что текущий образ LiteLLM по-прежнему вызывает
`async_post_call_streaming_iterator_hook`.

Для промышленного мониторинга используйте метрики Prometheus и структурированные журналы, а интерфейс рассматривайте как вспомогательный административный инструмент.

## Промышленные ограничения исходящих соединений

Проверки исходящего запроса на уровне приложения должны дополняться инфраструктурным
сетевым слоем «запрещено всё, кроме явно разрешённого». DevOps-команде нужно мониторить не только `/metrics` и структурированные журналы,
но и сетевые события CNI, шлюза исходящего трафика или firewall:

- разрешенные FQDN провайдеров из `litellm-config.yaml`: `api.z.ai`, `api.openai.com`,
  `api.anthropic.com` и явно заданные хосты `api_base`;
- запрещённые исходящие соединения из `litellm` к новым внешним FQDN;
- любые попытки исходящего интернет-доступа из `presidio-analyzer`, Redis или PostgreSQL;
- изменение DNS-запросов при обновлениях LiteLLM/провайдеров;
- соответствие промышленных манифестов шаблонам из `deploy/kubernetes/egress`.

Сетевые журналы не должны содержать текст запроса или исходные персональные данные; для корреляции используйте
ограниченные метки сервиса/установки, FQDN назначения, порт и решение политики. Подробный
регламент и проверочный список: [docs/egress-controls.md](egress-controls.md).

## Обновление LiteLLM

В текущем `docker-compose.yml` LiteLLM запускается из готового образа:

```yaml
image: docker.litellm.ai/berriai/litellm:main-stable
```

Поэтому для обновления LiteLLM не нужно пересобирать весь проект. Достаточно подтянуть новый образ и пересоздать только контейнер прокси:

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
| Новый образ LiteLLM | `make update-litellm` |
| Изменился `litellm-config.yaml` | `make restart` |
| Изменился `litellm_guardrails/*.py` | `make restart` |
| Изменился `.env` для LiteLLM | `docker compose up -d --force-recreate --no-deps litellm` |
| Изменились лимиты запуска `PRESIDIO_ANALYZER_*` | `docker compose up -d --force-recreate --no-deps presidio-analyzer` |
| Изменился `presidio/Dockerfile` или зависимости Analyzer | `make build`, затем `make up` |

Рекомендация для промышленной среды: после проверки на стенде фиксируйте конкретный тег LiteLLM или идентификатор образа (`digest`) вместо долгого использования плавающего `main-stable`. Перед обновлением сделайте резервную копию тома/базы PostgreSQL, потому что в PostgreSQL хранится состояние LiteLLM: пользовательские ключи, пользователи, бюджеты, использование и расходы.

Минимальный проверочный список обновления:

1. Зафиксировать текущий идентификатор образа (`digest`): `docker compose images litellm`.
2. Сделать резервную копию PostgreSQL.
3. Выполнить `make update-litellm`.
4. Проверить `make health`.
5. Проверить `make guardrails-list`.
6. Проверить `make guardrails-smoke` в локальном окружении Docker Compose, чтобы поймать изменения в обычных и потоковых обработчиках защитного слоя.
7. Проверить `make test-final-leak-proxy` в локальном окружении Docker Compose, чтобы поймать изменения порядка обработчиков до вызова модели и отсутствие выхода к провайдеру при финальной проверке утечки.
8. Проверить `make routing-smoke`.
9. Выполнить быстрый запрос с персональными данными и проверить `make monitor-smoke`.
10. Если есть регрессия, откатить тег или идентификатор образа в `docker-compose.yml` и пересоздать `litellm`.

## Ссылки

- [LiteLLM Prometheus metrics](https://docs.litellm.ai/docs/proxy/prometheus)
- [Балансировка нагрузки в LiteLLM](https://docs.litellm.ai/docs/proxy/load_balancing)
- [LiteLLM Guardrails quick start](https://docs.litellm.ai/docs/proxy/guardrails/quick_start)
- [LiteLLM Custom Guardrail](https://docs.litellm.ai/docs/proxy/guardrails/custom_guardrail)
- [LiteLLM Logging](https://docs.litellm.ai/docs/proxy/logging)
