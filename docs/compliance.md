# Проверка соответствия требованиям

Документ связывает требования безопасности с воспроизводимыми проверками и
ручными подтверждениями. Он не заменяет [мониторинг](monitoring.md) и
[справочник конфигурации](configuration.md).

## Проверочные контуры

| Контур | Команда | Доказывает | Не доказывает |
| --- | --- | --- | --- |
| Безопасность исходящего запроса | `make test-egress-security` | Имитация провайдера не получает исходные PII и секреты; заблокированный запрос не создаёт исходящий вызов | Промышленную доставку журналов и ограничения межсетевого экрана/CNI |
| Наблюдаемость | `make test-observability-gates` | Аудит, журналы и статусы проверяются отдельно от перехвата запроса | Панели мониторинга и хранилище журналов конкретной установки |
| Проверка с живым провайдером | `make guardrails-smoke`, `make test-e2e`, `make routing-smoke` | Реальные LiteLLM, защитные обработчики, протокол и маршрутизация работают вместе | Не доказывает отсутствие утечки: фактическая нагрузка внешнего провайдера не перехватывается |

Иными словами, успешная проверка с живым провайдером не доказывает отсутствие утечки.

## Покрытие исходящего запроса

`make test-egress-security` объединяет `make test-pre-egress-proxy` и
`make test-final-leak-proxy` с локальной имитацией провайдера.

| Семейство | Ожидаемое подтверждение |
| --- | --- |
| `negative-clean` | Чистый запрос доходит до Analyzer и провайдера |
| `pii-full-profile` | Провайдер получает `<PHONE_NUMBER_1>`, но не исходный телефон |
| `config-env-block` | `422 pre_egress_policy_blocked`; вызовов Analyzer и провайдера нет |
| `logs-block` | `422`, категория `log`, правило `log_or_stacktrace_payload`; исходящих вызовов нет |
| `regulated-topic-block` | `422 regulated_topic_policy_blocked`; остаются только категория, правило и действие |
| `dlp-canary-leak` | `422 final_payload_leak_check_blocked`; вызова провайдера нет |
| `auth-secrets` | Приватные ключи и секреты в стиле `.env` блокируются до провайдера |
| `synthetic-fixtures` | Разрешённые тестовые PII проходят; остальные в том же запросе обрабатываются |
| `admin-operator-boundary` | Клиентские, провайдерские и административные учётные данные разделены |
| `repeated-and-placeholder-collision` | Повторяющиеся значения и совпадения со служебными метками обрабатываются детерминированно |

## Границы покрытия

Российские реквизиты покрывают `RU_KPP`, `RU_OGRN`, `RU_OGRNIP`, `RU_BIK`,
`RU_SETTLEMENT_ACCOUNT` и `RU_CORRESPONDENT_ACCOUNT`. Это дополняет, но не
заменяет словарную подстановку названий организаций.

`RU_PASSPORT` объединяет номера внутренних и заграничных паспортов, военных
билетов и свидетельств о рождении. `RU_SNILS` проверяется по правилам выпуска
номера, включая ранний диапазон без контрольной суммы.

Инфраструктурные сущности и секреты покрывают `INTERNAL_IP`, `INTERNAL_DOMAIN`,
`HOSTNAME`, `DB_URL`, `JWT`, `BEARER_TOKEN`, `PRIVATE_KEY`, `API_KEY`,
`SECRET_KEY`, `AUTH_TOKEN`, `LOGIN` и `PASSWORD`. Правила рассчитаны на
отдельные значения в обычном запросе. Это не является полноценным сканированием секретов в исходном коде:
бинарные вложения и общий поиск по энтропии не поддерживаются.

Все корректные IP-адреса входят в покрытие по умолчанию. Настройка
`PRESIDIO_ANALYZER_DETECT_PUBLIC_IPS=false` ограничивает его приватными и
внутренними диапазонами.

`CONTRACT_NUMBER` подтверждается BERT и строгим контекстным правилом. Версии,
даты, шаблоны и случайные коды отбрасываются; решения считает
`ru_presidio_analyzer_merge_decisions_total` без исходных значений и смещений.

### Словарные подстановки

`DICTIONARY_SUBSTITUTIONS_ENABLED=true` и
`dictionary-substitutions.default.json` дают детерминированную замену десяти
российских банков. Это бизнес-политика, а не распознаватель PII. В Redis хранится
временное обратное сопоставление; журналы содержат только `rule_id` и счётчики.
Склонённое или перефразированное моделью значение не восстанавливается.

### Регулируемые темы

`REGULATED_TOPIC_POLICY_MODE=block` останавливает внутренние материалы по
AML/CFT / ПОД/ФТ, санкционным проверкам и мониторингу транзакций. Это не персональные данные.
Код `regulated_topic_policy_blocked` и метрика
`ru_regulated_topic_policy_blocked_total` содержат только ограниченные категории
и правила, но не исходный запрос. Дополнительные правила задаются через
`REGULATED_TOPIC_POLICY_EXTRA_RULES_JSON`.

### Синтетические данные

`SYNTHETIC_PII_ALLOWLIST_MODE=off` является промышленным значением по умолчанию.
`SYNTHETIC_PII_ALLOWLIST_JSON` разрешает только контролируемые synthetic/test
значения. Срабатывания считает `ru_synthetic_pii_allowlist_hits_total`; исходные
значения не попадают в журналы или метрики. Применение списка к промышленному
трафику требует отдельного согласования.

### Административная граница

Пользователи получают ключи LiteLLM или JWT/OIDC; `LITELLM_MASTER_KEY`,
`UI_USERNAME`, `UI_PASSWORD` и ключи провайдеров остаются у операторов. `/ui` и
Admin API закрываются SSO, VPN, mTLS, частной сетью или отключаются через
`DISABLE_ADMIN_UI=True`. Роли, ротация и аудит административных действий:
[docs/admin-access.md](admin-access.md).

## Сетевые ограничения

Прикладные проверки дополняются политикой «запрещено всё, кроме разрешённого»:

- `litellm` обращается только к Analyzer, Redis, PostgreSQL и утверждённым FQDN;
- Analyzer, Redis и PostgreSQL не имеют интернет-доступа во время работы;
- CNI или шлюз исходящего трафика фиксирует запрещённые соединения;
- локальная bridge-сеть Docker Compose не считается промышленным ограничением.

Регламент: [docs/egress-controls.md](egress-controls.md). Шаблоны:
[`deploy/kubernetes/egress`](../deploy/kubernetes/egress). Сетевой слой не
заменяет `PRE_EGRESS_POLICY_MODE`, `FINAL_PAYLOAD_LEAK_CHECK_MODE` и
`make test-egress-security`.

## Подтверждения наблюдаемости

Реализованный в #29 `gateway_guardrail_audit` записывает одно решение до вызова
модели: `request_id`, `model`, `status`, `latency_ms`, `guardrail_mode`,
`call_type`, `policy_mode`, `policy_result`, `redaction_count`, `entity_counts`,
а для блокировок — `block_reason`, `error_code` и ограниченные правила.

Реализованная в #31 телеметрия Analyzer пишет `presidio_analyzer_request` и
метрики:

- `ru_presidio_analyzer_requests_total`;
- `ru_presidio_analyzer_latency_seconds_*`;
- `ru_presidio_analyzer_entities_detected_total`;
- `ru_presidio_analyzer_capacity_rejections_total`;
- `ru_presidio_analyzer_failures_total`;
- `ru_presidio_analyzer_ner_failures_total`;
- `ru_presidio_analyzer_ner_inference_total`;
- `ru_presidio_analyzer_ner_inference_duration_seconds_*`;
- `ru_presidio_analyzer_ner_windows_processed_*`;
- `ru_presidio_analyzer_merge_decisions_total`.

События `presidio_ner_startup_*` и `presidio_ner_inference` содержат только
ограниченные эксплуатационные поля. Аудит и телеметрия не включают исходный
текст, значения сущностей, смещения, ключи или содержимое Redis.

## Подтверждения для ручной проверки

Для каждого сценария приложите:

1. Команду и проверочный контур.
2. Семейство запроса, например `config-env-block` или `dlp-canary-leak`.
3. HTTP-статус и безопасный код ошибки.
4. Сводку имитации провайдера: `provider_requests=0` либо
   `provider_saw_raw_phone=false`.
5. Подтверждение, что запрещённое значение отсутствует в журналах LiteLLM.

Для административной проверки дополнительно приложите схему доступа к `/ui` и
Admin API, список ролей, подтверждение ротации и пример аудита изменения ключа,
бюджета или модели.

Зелёная наблюдаемость при красной проверке исходящего запроса означает регрессию
безопасности. Обратная ситуация означает, что защитное поведение работает, но
его доказательств недостаточно для аудита.
