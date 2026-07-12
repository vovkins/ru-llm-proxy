# Compliance Evidence Gates

Этот документ описывает, какими проверками проект доказывает ключевые свойства
маскирующего proxy. Он дополняет README и monitoring guide: README отвечает за
быстрый старт, `docs/monitoring.md` — за production observability, а этот документ
разделяет security evidence, observability evidence и live compatibility smoke.
Переменные окружения, которые управляют этими gates и политиками, описаны в
[configuration.md](configuration.md).

## Gate Boundaries

| Gate | Команда | Что доказывает | Что не доказывает |
| --- | --- | --- | --- |
| Egress-security gate | `make test-egress-security` | Mock provider capture показывает, что raw PII, секреты, regulated AML/CFT / ПОД/ФТ topics, config/log payloads и final leak canaries не доходят до provider-bound request; blocked-запросы дают zero provider capture. | Полноту audit schema, production log shipping и production network firewall/CNI enforcement. |
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
| `regulated-topic-block` | pre-egress smoke, regulated-topic case | `422`, `regulated_topic_policy_blocked`, bounded category/rule/action, Analyzer/provider requests равны `0`. |
| `dlp-canary-leak` | final leak smoke | `422`, `final_payload_leak_check_blocked`, provider requests равны `0`. |
| `auth-secrets` deterministic markers | final leak smoke | Private key/env-secret-like markers in provider-bound fields block before provider. |
| `synthetic-fixtures` | guardrail unit tests and manual/demo smoke | Explicit synthetic/test PII values can be allowlisted without masking, while non-allowlisted PII in the same request is still masked or blocked. |
| `admin-operator-boundary` | docs/static checks and production deployment evidence | Client credentials, provider credentials and admin credentials are separated; Admin UI/API is protected by SSO/reverse-proxy/private-network boundary or disabled. |
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
`code-identifiers-companies` частично покрывается reversible dictionary substitution:
названия организаций и business terms можно заменять exact rules до Analyzer без
ожидания NER-срабатывания. Для class names, env vars и code identifiers нужны
явные operator rules в dictionary config; generic source-code rewriting остаётся вне
scope recognizers.

`business-dictionary-substitution` покрывает требования к детерминированной замене
организаций, банков и продуктовых/проектных терминов перед provider egress. По
умолчанию `DICTIONARY_SUBSTITUTIONS_ENABLED=true`, а
`dictionary-substitutions.default.json` содержит seed из 10 крупных российских
банков: `Сбербанк`, `ВТБ`, `Газпромбанк`, `Альфа-Банк`, `ПСБ`,
`Россельхозбанк`, `Т-Банк`, `Московский кредитный банк`, `Банк Дом.РФ`,
`Совкомбанк`. Это business policy, не PII recognizer: replacement spans
исключаются из последующего mask/block, combined restore mapping хранится в Redis
с TTL `PII_MAPPING_TTL_SECONDS`, logs/metrics содержат только bounded `rule_id`
и counts. Restore exact-match only; склонения, переводы и paraphrase не являются
гарантированно обратимыми.

`regulated-topic-block` покрывает требование не раскрывать внутренние AML/CFT /
ПОД/ФТ меры внешним моделям. Это не является PII: запрос может описывать внутренние
контроли, санкционный скрининг, transaction-monitoring thresholds, suspicious-activity
playbooks или compliance-bypass процедуры без персональных данных. Первая версия
policy pack intentionally block-only и по умолчанию `REGULATED_TOPIC_POLICY_MODE=off`;
включение `block` является deployment decision. Public defaults содержат только
bounded categories/rule ids/action type, а organization-specific confidential terms
добавляются через `REGULATED_TOPIC_POLICY_EXTRA_RULES_JSON`. Metric
`ru_regulated_topic_policy_blocked_total` считает blocks по bounded `category` и
`rule_id`; logs не содержат raw prompt или raw matched text. Mask и
dictionary-substitute actions не смешиваются с broad topic blocking: reversible
dictionary substitution реализован отдельным exact-match слоем.

`synthetic-fixtures` покрывает эксплуатационную потребность использовать заранее
согласованные тестовые PII-значения в smoke/demo/checklist сценариях. Это narrow
exception после Analyzer и до обычного `mask`/`block` поведения, а не способ
пропускать реальные персональные данные. По умолчанию `SYNTHETIC_PII_ALLOWLIST_MODE=off`;
rules задаются через `SYNTHETIC_PII_ALLOWLIST_JSON`, broad regex patterns
игнорируются, а logs/metrics пишут только bounded `rule_id`, entity type и counts без
raw values. Если в production traffic появляются
`ru_synthetic_pii_allowlist_hits_total`, это должно быть ожидаемым тестовым контуром
или отдельным incident/usage review.

`admin-operator-boundary` покрывает требование отделить пользовательский доступ к
proxy от administrator/operator access. Нормальные пользователи и приложения получают
LiteLLM virtual keys или validated JWT/OIDC tokens; upstream provider keys остаются
только на proxy; `LITELLM_MASTER_KEY`, `UI_USERNAME` и `UI_PASSWORD` считаются
privileged admin credentials. Production Admin UI/API должны быть закрыты
operator-only boundary (SSO/OIDC/SAML, VPN, IP allowlist, mTLS, zero-trust proxy,
private network) или UI должен быть отключён через `DISABLE_ADMIN_UI=True`.
Подробный runbook: [docs/admin-access.md](admin-access.md).

## Production Network Egress Evidence

Production egress controls покрываются отдельным инфраструктурным слоем, а не только
application smoke-тестами. Требуемое целевое состояние и стартовые manifests описаны в
[docs/egress-controls.md](egress-controls.md) и
[deploy/kubernetes/egress](../deploy/kubernetes/egress):

- namespace/pod-level default deny egress;
- allowlist для текущих provider FQDNs (`api.z.ai`, `api.openai.com`,
  `api.anthropic.com`) и явно настроенных `api_base` hosts;
- internal-only egress от `litellm` к `presidio-analyzer`, Redis и PostgreSQL;
- отсутствие runtime internet egress у `presidio-analyzer`, Redis и PostgreSQL;
- CNI/egress/firewall logs для denied outbound flows и DNS drift.

Этот слой не заменяет `PRE_EGRESS_POLICY_MODE`, `FINAL_PAYLOAD_LEAK_CHECK_MODE` и
`make test-egress-security`: он ограничивает сеть, а application gates доказывают, что
provider-bound payload очищается или блокируется до внешнего вызова. Local Docker
Compose bridge network не считается evidence для production deny-all outbound egress.

## Observability Evidence

В рамках #30 observability gate остается lightweight:

- smoke scripts проверяют, что LiteLLM logs не содержат raw forbidden values из
  blocked/masked test payloads;
- static checks гарантируют, что egress-security и observability статусы не
  схлопываются в один target;
- документация явно говорит, что live-provider smoke не является leakage proof.

Gateway audit logging из #29 пишет `gateway_guardrail_audit` один раз на pre-call
решение. Event содержит safe decision fields: `request_id`, `model`, `status`,
`latency_ms`, `guardrail_mode`, `call_type`, `policy_mode`,
`regulated_topic_policy_mode`, `policy_result`,
`redaction_count`, `entity_counts`, а для блокировок/ошибок — `block_reason`,
`error_code`, bounded `categories`/`rules`/`actions`, synthetic allowlist rule/entity counts
и counts. Он не содержит raw prompt
text, raw PII, raw matched text, snippets, offsets, provider keys или Redis mapping contents.

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
2. Request family: например `regulated-topic-block`, `config-env-block`, `logs-block` или `dlp-canary-leak`.
3. HTTP status и safe error code: `regulated_topic_policy_blocked`, `pre_egress_policy_blocked` или
   `final_payload_leak_check_blocked`.
4. Capture summary из mock upstream: `provider_requests=0` для block/no-egress
   сценариев или `provider_saw_raw_phone=false` для mask сценариев.
5. Подтверждение log safety: raw forbidden value отсутствует в LiteLLM logs.

Для admin/operator boundary дополнительно прикладывайте:

1. Схему ingress/reverse-proxy/SSO, которая показывает, что `/ui` и admin API routes
   не являются публичными без operator boundary.
2. Список operator roles/groups и break-glass owners.
3. Evidence ротации `LITELLM_MASTER_KEY` и `UI_PASSWORD` в staging.
4. Пример admin action audit события или ticket/change record для key/budget/model change.
5. Подтверждение, что client docs and configs use `RU_LLM_PROXY_TOKEN` or OIDC/JWT,
   not `LITELLM_MASTER_KEY`, as the client credential.

Если egress-security gate проходит, а observability gate падает, это означает, что
защита provider egress может быть корректной, но evidence/logging недостаточны для
аудита. Если observability gate проходит, а egress-security падает, это означает
реальный security regression независимо от качества логов.
