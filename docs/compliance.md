# Compliance Evidence Gates

Этот документ описывает, какими проверками проект доказывает ключевые свойства
маскирующего proxy. Он дополняет README и monitoring guide: README отвечает за
быстрый старт, `docs/monitoring.md` — за production observability, а этот документ
разделяет security evidence, observability evidence и live compatibility smoke.

## Gate Boundaries

| Gate | Команда | Что доказывает | Что не доказывает |
| --- | --- | --- | --- |
| Egress-security gate | `make test-egress-security` | Mock provider capture показывает, что raw PII, секреты, config/log payloads и final leak canaries не доходят до provider-bound request; blocked-запросы дают zero provider capture. | Полноту audit schema и production log shipping. |
| Observability gate | `make test-observability-gates` | Lightweight checks фиксируют, что egress и observability gates существуют отдельно, smoke проверяет safe logs, а документация не смешивает live smoke с leakage proof. | Полную gateway audit schema из #29 и analyzer request telemetry из #31. |
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

Не все compliance families из внешних требований уже имеют recognizer-level coverage.
Например, `counterparty-full-profile`, `infrastructure-internal` и
`code-identifiers-companies` зависят от будущих recognizers/policies в #34, #37 и #25.
#30 отвечает за структуру evidence gates, а не за добавление новых entity detectors.

## Observability Evidence

В рамках #30 observability gate остается lightweight:

- smoke scripts проверяют, что LiteLLM logs не содержат raw forbidden values из
  blocked/masked test payloads;
- static checks гарантируют, что egress-security и observability статусы не
  схлопываются в один target;
- документация явно говорит, что live-provider smoke не является leakage proof.

Полноценный gateway audit event будет реализован в #29. Он должен добавить safe
decision fields вроде `request_id`, `model`, `status`, `latency_ms`,
`policy_result`, `block_reason`, `error_code` и redaction/entity counts.

Per-request telemetry Presidio Analyzer будет реализована в #31. Она должна
добавить safe outcome/latency/entity-count/failure telemetry без raw input text,
raw entity values и reconstructable offsets.

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
