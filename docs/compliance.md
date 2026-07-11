# Compliance Evidence Gates

Этот документ описывает, какими проверками проект доказывает ключевые свойства
маскирующего proxy. Он дополняет README и monitoring guide: README отвечает за
быстрый старт, `docs/monitoring.md` — за production observability, а этот документ
разделяет security evidence, observability evidence и live compatibility smoke.

## Gate Boundaries

| Gate | Команда | Что доказывает | Что не доказывает |
| --- | --- | --- | --- |
| Egress-security gate | `make test-egress-security` | Mock provider capture показывает, что raw PII, секреты, config/log payloads и final leak canaries не доходят до provider-bound request; blocked-запросы дают zero provider capture. | Полноту audit schema и production log shipping. |
| Observability gate | `make test-observability-gates` | Lightweight checks фиксируют, что egress и observability gates существуют отдельно, smoke проверяет safe logs, gateway audit schema и Analyzer telemetry описаны, а документация не смешивает live smoke с leakage proof. | Production log shipping и vendor-specific dashboards. |
| Live-provider smoke | `make guardrails-smoke`, `make test-e2e`, `make routing-smoke` | Реальный LiteLLM image, guardrail hooks, provider protocol и sticky routing работают в live окружении. | Live-provider smoke не доказывает отсутствие утечки, потому что проект не видит фактический provider-bound payload у внешнего провайдера. |

## Egress-security Fixtures

`make test-egress-security` агрегирует Docker smoke с локальным mock upstream:

- `make test-pre-egress-proxy` проверяет блокировку operational payloads до Analyzer и provider.
- `make test-final-leak-proxy` проверяет final provider-bound leak check после request mutation и до provider call.

Текущий fixture set покрывает следующие семейства:

| Семейство | Где проверяется | Ожидаемое evidence |
| --- | --- | --- |
| `negative-clean` | pre-egress и final leak smoke | Analyzer получает clean text, provider получает один request. |
| `pii-full-profile` baseline | final leak smoke, masked phone case | Provider получает placeholder `<PHONE_NUMBER_1>`, но не raw phone. |
| `config-env-block` | pre-egress smoke | `422`, `pre_egress_policy_blocked`, Analyzer/provider requests равны `0`. |
| `logs-block` | pre-egress smoke, access-log case | `422`, category `log`, rule `log_or_stacktrace_payload`, Analyzer/provider requests равны `0`. |
| `dlp-canary-leak` | final leak smoke | `422`, `final_payload_leak_check_blocked`, provider requests равны `0`. |
| `auth-secrets` deterministic markers | final leak smoke | Private key/env-secret-like markers in provider-bound fields block before provider. |
| `repeated-and-placeholder-collision` | guardrail unit tests and flow tests | Placeholder replacement remains deterministic; broader egress evidence should stay in mock-provider smoke when new fixtures are added. |

Не все compliance families из внешних требований уже имеют полный coverage.
`counterparty-full-profile` теперь частично покрыт Presidio recognizers для
российских реквизитов: `RU_KPP`, `RU_OGRN`, `RU_OGRNIP`, `RU_BIK`,
`RU_SETTLEMENT_ACCOUNT` и `RU_CORRESPONDENT_ACCOUNT`. Эти recognizers закрывают
налоговые и банковские реквизиты, но не заменяют словарную замену названий
организаций из #25.

`infrastructure-internal` теперь имеет entity-level coverage через
`INTERNAL_IP`, `INTERNAL_DOMAIN`, `HOSTNAME`, `DB_URL`, `JWT`, `BEARER_TOKEN`,
`PRIVATE_KEY`, `API_KEY`, `LOGIN` и `PASSWORD`. Это покрытие предназначено для
одиночных технических идентификаторов и секретов внутри обычных prompt'ов. Оно
не является full source-code secret scanning, не классифицирует бинарные
attachments и не заменяет production egress allowlist из #38. Семейство
`code-identifiers-companies` остаётся в scope #25 и связанных policy tasks,
если потребуется dictionary substitution для названий внутренних систем,
организаций или кодовых идентификаторов. #30 отвечает за структуру evidence
gates, а не за добавление новых entity detectors.

## Observability Evidence

В рамках #30 observability gate остается lightweight:

- smoke scripts проверяют, что LiteLLM logs не содержат raw forbidden values из
  blocked/masked test payloads;
- static checks гарантируют, что egress-security и observability статусы не
  схлопываются в один target;
- документация явно говорит, что live-provider smoke не является leakage proof.

Gateway audit logging из #29 пишет `gateway_guardrail_audit` один раз на pre-call
решение. Event содержит safe decision fields: `request_id`, `model`, `status`,
`latency_ms`, `guardrail_mode`, `call_type`, `policy_mode`, `policy_result`,
`redaction_count`, `entity_counts`, а для блокировок/ошибок — `block_reason`,
`error_code`, bounded `categories`/`rules` и counts. Он не содержит raw prompt
text, raw PII, snippets, offsets, provider keys или Redis mapping contents.

Per-request telemetry Presidio Analyzer из #31 пишет `presidio_analyzer_request`
на каждый `/api/v1/analyze` request и exposes metrics
`ru_presidio_analyzer_requests_total`,
`ru_presidio_analyzer_latency_seconds_*`,
`ru_presidio_analyzer_entities_detected_total`,
`ru_presidio_analyzer_capacity_rejections_total` и
`ru_presidio_analyzer_failures_total`. Telemetry содержит safe outcome, latency,
entity type counts, capacity snapshot, NER state и bounded failure reason без raw
input text, raw entity values, reconstructable offsets, API keys или proxy tokens.

## Evidence For Manual Review

Для ручной проверки требований удобно прикладывать:

1. Команду и gate: `make test-egress-security` или `make test-observability-gates`.
2. Request family: например `config-env-block`, `logs-block` или `dlp-canary-leak`.
3. HTTP status и safe error code: `pre_egress_policy_blocked` или
   `final_payload_leak_check_blocked`.
4. Capture summary из mock upstream: `provider_requests=0` для block/no-egress
   сценариев или `provider_saw_raw_phone=false` для mask сценариев.
5. Подтверждение log safety: raw forbidden value отсутствует в LiteLLM logs.

Если egress-security gate проходит, а observability gate падает, это означает, что
защита provider egress может быть корректной, но evidence/logging недостаточны для
аудита. Если observability gate проходит, а egress-security падает, это означает
реальный security regression независимо от качества логов.
