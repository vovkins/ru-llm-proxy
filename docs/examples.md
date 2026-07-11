# Примеры API

Все примеры соответствуют текущей конфигурации репозитория: LiteLLM на `localhost:4000`, стабильная Z.AI модель `glm-5.1`, provider-prefixed alias `zai-glm-5.1`, OpenAI aliases `openai-gpt-5.4-mini` / `openai-gpt-5.5` и Anthropic aliases `claude-haiku-4.5` / `claude-sonnet-4.6` / `claude-opus-4.8`.

OpenAI/Anthropic aliases являются proxy-facing примерами. Перед production используйте только model IDs, проверенные live на текущем LiteLLM image и реальных provider keys.

## Окружение

```bash
export API_URL="http://localhost:4000"
export RU_LLM_PROXY_TOKEN="sk-..."
# Optional only for BYOK passthrough examples:
export ANTHROPIC_BYOK_API_KEY="sk-ant-..."
```

Создавайте обычные пользовательские `RU_LLM_PROXY_TOKEN` через LiteLLM Admin UI. CLI helper нужен для DevOps/CI/bootstrap/runbook-сценариев:

```bash
make virtual-key-create KEY_ALIAS=local-examples MODELS=standard,zai,openai,anthropic DURATION=30d
```

`LITELLM_MASTER_KEY` используется только для admin-операций, например создания virtual keys и просмотра списка guardrails.
Production Admin UI/API boundary, operator roles and credential rotation are covered in [admin-access.md](admin-access.md).

## Режимы авторизации

Server-funded режим использует proxy token как обычный bearer token. Proxy сам вызывает провайдера через серверные `OPENAI_API_KEY`, `ANTHROPIC_API_KEY` или `ZAI_API_KEY`:

```bash
curl -s "$API_URL/v1/chat/completions" \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $RU_LLM_PROXY_TOKEN" \
  -d '{"model":"glm-5.1","messages":[{"role":"user","content":"Привет"}]}'
```

BYOK passthrough режим разделяет proxy auth и provider auth. Proxy token передаётся в `x-litellm-api-key`, а provider auth передаётся через поддерживаемый provider-specific header вроде `x-api-key`, `api-key` или `x-goog-api-key`. Этот режим не включён в default config; включайте его отдельным opt-in deployment после live validation на текущем LiteLLM image.

```bash
curl -s "$API_URL/v1/messages" \
  -H "Content-Type: application/json" \
  -H "x-litellm-api-key: Bearer $RU_LLM_PROXY_TOKEN" \
  -H "x-api-key: $ANTHROPIC_BYOK_API_KEY" \
  -d '{
    "model": "claude-sonnet-4.6",
    "max_tokens": 80,
    "messages": [{"role": "user", "content": "Привет"}]
  }'
```

Codex/ChatGPT и Claude subscription OAuth обычно используют provider `Authorization`. Обычный LiteLLM path может не форвардить этот header upstream, поэтому subscription passthrough нужно считать experimental до live validation; при необходимости выносите его в pass-through route, sidecar или custom adapter. Не кладите общий Codex `auth.json` или Claude credentials на proxy.

## Chat completion без PII

```bash
curl -s "$API_URL/v1/chat/completions" \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $RU_LLM_PROXY_TOKEN" \
  -d '{
    "model": "glm-5.1",
    "messages": [
      {
        "role": "user",
        "content": "Скажи короткое приветствие на русском"
      }
    ],
    "max_tokens": 80
  }' | jq '.choices[0].message'
```

## Chat completion с PII

```bash
curl -s "$API_URL/v1/chat/completions" \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $RU_LLM_PROXY_TOKEN" \
  -d '{
    "model": "glm-5.1",
    "messages": [
      {
        "role": "user",
        "content": "Клиент Иванов Иван, телефон +79031234567, ИНН 7707083893. Составь краткую справку."
      }
    ],
    "max_tokens": 120
  }' | jq '.choices[0].message'
```

Перед вызовом провайдера guardrail отправляет masked text примерно такого вида:

```text
Клиент <PERSON_1>, телефон <PHONE_NUMBER_1>, ИНН <RU_INN_1>. Составь краткую справку.
```

Если ответ провайдера содержит эти плейсхолдеры, post-call hook восстановит исходные значения перед возвратом клиенту.

## Несколько значений одного типа

```bash
curl -s "$API_URL/v1/chat/completions" \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $RU_LLM_PROXY_TOKEN" \
  -d '{
    "model": "glm-5.1",
    "messages": [
      {
        "role": "user",
        "content": "Основной телефон +79031234567, резервный телефон 89031234567."
      }
    ],
    "max_tokens": 80
  }' | jq '.choices[0].message'
```

Провайдер получает разные плейсхолдеры:

```text
Основной телефон <PHONE_NUMBER_1>, резервный телефон <PHONE_NUMBER_2>.
```

## OpenAI Responses API

Codex CLI/App local tasks используют Responses API:

```bash
curl -s "$API_URL/v1/responses" \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $RU_LLM_PROXY_TOKEN" \
  -d '{
    "model": "openai-gpt-5.4-mini",
    "input": "Скажи короткое приветствие на русском",
    "max_output_tokens": 80
  }' | jq
```

PII guardrail applies to Anthropic top-level `system` string/text blocks, Responses API top-level `instructions` / `input` strings, message-like `input[]` items with string `content`, tool-call `arguments`, tool-output items with string/list `output`, and text blocks with `text`, `input_text`, or `output_text` types. Non-text inputs such as images/files are passed through unchanged.

Для live smoke этого endpoint задайте `RESPONSES_MODEL` явно:

```bash
RESPONSES_MODEL=openai-gpt-5.4-mini make client-auth-smoke
```

## Basic Anthropic Messages API

Это базовый non-streaming пример Anthropic Messages API через proxy:

```bash
curl -s "$API_URL/v1/messages" \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $RU_LLM_PROXY_TOKEN" \
  -d '{
    "model": "claude-sonnet-4.6",
    "max_tokens": 80,
    "messages": [
      {
        "role": "user",
        "content": "Скажи короткое приветствие на русском"
      }
    ]
  }' | jq
```

Для live smoke этого endpoint задайте `MESSAGES_MODEL` явно:

```bash
MESSAGES_MODEL=claude-sonnet-4.6 make client-auth-smoke
```

Полный Claude Code gateway contract строже этого примера: `POST /v1/messages?beta=true`, streaming SSE responses, forwarding `anthropic-version` / `anthropic-beta`, optional token counting и model discovery. Его статус описан в [clients/claude-code.md](clients/claude-code.md).

## Прямая проверка Analyzer

```bash
curl -s http://localhost:5001/api/v1/analyze \
  -H "Content-Type: application/json" \
  -d '{
    "text": "Мой телефон +79031234567 и ИНН 7707083893",
    "language": "ru"
  }' | jq
```

Ожидаемые entity types: `PHONE_NUMBER` и `RU_INN`.

По умолчанию Analyzer API использует `score_threshold=0.35`, а `RU_INN` проходит checksum validation. При `PRESIDIO_ANALYZER_DETECT_BARE_INN_BY_CHECKSUM=true` checksum-valid bare 12-digit INN детектируется даже без контекстного слова:

```bash
curl -s http://localhost:5001/api/v1/analyze \
  -H "Content-Type: application/json" \
  -d '{
    "text": "500100732259",
    "language": "ru",
    "score_threshold": 0.35
  }' | jq
```

10-digit INN требует контекст даже в default mode, потому что checksum пропускает заметную долю случайных 10-значных чисел. Если выставить `PRESIDIO_ANALYZER_DETECT_BARE_INN_BY_CHECKSUM=false`, strict mode требует контекст вроде `ИНН` или `налогоплательщик` для любого голого ИНН; bare INN без контекста не проходит `score_threshold=0.35`. Это снижает false positives для случайных длинных числовых последовательностей, которые прошли checksum.

`RU_ADDRESS` — ограниченный regex recognizer. Он покрывает базовые формы вроде `ул. Ленина, д. 10`, `ул Ленина 10`, `Тверская улица, дом 7`, требует границу слева у street-type сокращений и явное `дом`/`д.` для `Тверская улица, дом 7`; unsupported cases включают полный разбор индексов, регионов, владений и свободных адресов без явной street/house structure.

Реквизиты контрагента покрываются отдельными entity types:

- `RU_KPP`;
- `RU_OGRN`;
- `RU_OGRNIP`;
- `RU_BIK`;
- `RU_SETTLEMENT_ACCOUNT`;
- `RU_CORRESPONDENT_ACCOUNT`.

`RU_KPP`, `RU_BIK`, `RU_SETTLEMENT_ACCOUNT` и `RU_CORRESPONDENT_ACCOUNT` требуют явный реквизитный контекст. `RU_OGRN` и `RU_OGRNIP` проходят checksum validation. Для расчётных и корреспондентских счетов при наличии БИК рядом Analyzer дополнительно проверяет контрольный ключ; без БИК используется сильный контекст и структурные ограничения, без online lookup по справочнику банков.

```bash
curl -s http://localhost:5001/api/v1/analyze \
  -H "Content-Type: application/json" \
  -d '{
    "text": "КПП 770801001, ОГРН 1027700132195, БИК 044525225, расчетный счет 40702810900000000000, к/с 30101810400000000225",
    "language": "ru",
    "score_threshold": 0.35
  }' | jq
```

Infrastructure/secret recognizers работают на том же Analyzer API и возвращают
entity-level spans для одиночных технических идентификаторов и секретов:

- `INTERNAL_IP`;
- `INTERNAL_DOMAIN`;
- `HOSTNAME`;
- `DB_URL`;
- `JWT`;
- `BEARER_TOKEN`;
- `PRIVATE_KEY`;
- `API_KEY`;
- `LOGIN`;
- `PASSWORD`.

```bash
curl -s http://localhost:5001/api/v1/analyze \
  -H "Content-Type: application/json" \
  -d '{
    "text": "Internal endpoint 10.24.3.7, api.payments.corp.local, Authorization: Bearer abcdefghijklmnopqrstuvwxyz123456, DATABASE_URL=postgresql://svc_user:S3curePass42@db.internal:5432/app",
    "language": "ru",
    "score_threshold": 0.35
  }' | jq
```

`INTERNAL_DOMAIN` использует suffix list из
`PRESIDIO_ANALYZER_INTERNAL_DOMAIN_SUFFIXES`. По умолчанию публичные IP не
детектируются как `INTERNAL_IP`; если deployment policy считает любые IP
чувствительными, включите `PRESIDIO_ANALYZER_DETECT_PUBLIC_IPS=true` и
пересоздайте контейнер `presidio-analyzer`.

## Фильтрация Analyzer по entity types

Analyzer API поддерживает стандартный Presidio-параметр `entities`. Regex recognizers и DeepPavlov NER соблюдают этот список одинаково: если запрошен только `RU_INN`, NER-типы `PERSON`, `LOCATION` и `ORGANIZATION` не вычисляются.

```bash
curl -s http://localhost:5001/api/v1/analyze \
  -H "Content-Type: application/json" \
  -d '{
    "text": "Иван Иванов из Москвы, ИНН 7707083893",
    "language": "ru",
    "entities": ["RU_INN"]
  }' | jq
```

Чтобы получить только NER-сущности, явно запросите соответствующие типы:

```bash
curl -s http://localhost:5001/api/v1/analyze \
  -H "Content-Type: application/json" \
  -d '{
    "text": "Иван Иванов работает в Газпроме",
    "language": "ru",
    "entities": ["PERSON", "ORGANIZATION"],
    "score_threshold": 0.7
  }' | jq
```

NER-результаты имеют фиксированный score `0.7`; при `score_threshold` выше `0.7` DeepPavlov NER не запускается.

## Health Checks

LiteLLM liveness endpoint не требует `LITELLM_MASTER_KEY` и используется для Docker healthcheck контейнера `ru-llm-proxy`:

```bash
curl -s http://localhost:4000/health/liveliness
```

`/health` у LiteLLM предназначен для проверки моделей и может делать реальные LLM API calls, поэтому для liveness/readiness лучше использовать специализированные endpoints.

## Health Analyzer

```bash
curl -s http://localhost:5001/api/v1/health | jq
```

NER status возвращается отдельно:

```json
{"status":"ok","ner":"loaded"}
```

Если модель не загрузилась, сервис продолжит работать для regex recognizers:

```json
{"status":"ok","ner":"not_loaded"}
```

## Guardrails

Список guardrails, зарегистрированных в LiteLLM, смотрит администратор через Makefile target:

```bash
make guardrails-list
```

Live-запрос с явным `guardrails` parameter:

```bash
curl -s -D /tmp/ru-llm-proxy-headers "$API_URL/v1/chat/completions" \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $RU_LLM_PROXY_TOKEN" \
  -d '{
    "model": "glm-5.1",
    "guardrails": ["ru-pii-mask-pre", "ru-pii-mask-post"],
    "messages": [
      {
        "role": "user",
        "content": "Клиент Иванов Иван, телефон +79031234567"
      }
    ],
    "max_tokens": 80
  }' | jq '.choices[0].message'

grep -i '^x-litellm-applied-guardrails:' /tmp/ru-llm-proxy-headers
```

То же самое через Makefile:

```bash
make guardrails-smoke
```

## PII block mode

По умолчанию guardrail работает в reversible masking mode:

```env
PII_GUARDRAIL_MODE=mask
```

Чтобы отклонять запросы с найденной PII до вызова провайдера, задайте block mode и перезапустите LiteLLM:

```env
PII_GUARDRAIL_MODE=block
```

```bash
make restart
```

В block mode запрос с PII возвращает `422` и безопасный error body. Ответ содержит только типы сущностей:

```json
{
  "error": {
    "message": "Request contains personal data and was blocked by PII policy.",
    "type": "pii_detected",
    "code": "pii_blocked",
    "details": {
      "entities": ["PHONE_NUMBER"]
    }
  }
}
```

Raw PII, offsets и исходный текст в error body не возвращаются. Clean-запросы продолжают идти к провайдеру.

## Synthetic/test PII allowlist

По умолчанию allowlist выключен:

```env
SYNTHETIC_PII_ALLOWLIST_MODE=off
SYNTHETIC_PII_ALLOWLIST_JSON=[]
```

Включайте его только для controlled fixtures, которые нужны в smoke-тестах, демо или проверочных документах. Пример ниже разрешает один synthetic phone exact value и synthetic email namespace `example.test`:

```env
SYNTHETIC_PII_ALLOWLIST_MODE=allow
SYNTHETIC_PII_ALLOWLIST_JSON=[{"rule_id":"docs_synthetic_contacts","entity_types":["PHONE_NUMBER","EMAIL_ADDRESS"],"values":["+79031234567"],"patterns":["^[A-Za-z0-9._%+-]+@example\\.test$"]}]
```

В `mask` mode allowlisted spans остаются как есть, а остальные PII в том же запросе маскируются:

```text
Input:  Тестовый телефон +79031234567, реальный +79035551234
Output: Тестовый телефон +79031234567, реальный <PHONE_NUMBER_1>
```

В `block` mode allowlisted-only запрос проходит дальше, но mixed request с allowlisted и real PII всё равно блокируется из-за оставшихся real spans. Allowlist hits видны в `synthetic_pii_allowlist_applied`, `gateway_guardrail_audit` и `ru_synthetic_pii_allowlist_hits_total`; raw allowlisted values в logs/metrics не пишутся.

Broad regex patterns вроде `^.*$` игнорируются. Regex должен быть anchored и ссылаться на controlled synthetic namespace (`example.test`, `TEST_`, `RU_PROXY_`, `SYNTHETIC_`, `CANARY_`). Allowlist не применяется к pre-egress, regulated-topic и final leak-check policies.

## Regulated-topic policy

`REGULATED_TOPIC_POLICY_MODE=off` по умолчанию. Это отдельный policy pack для AML/CFT / ПОД/ФТ, sanctions-screening, transaction-monitoring, suspicious-activity и compliance-bypass тем; он не является PII recognizer, не маскирует entity spans и не создаёт Redis mapping.

Чтобы включить conservative block-only режим:

```env
REGULATED_TOPIC_POLICY_MODE=block
```

```bash
docker compose up -d --force-recreate --no-deps litellm
```

При срабатывании запрос не отправляется в Analyzer и провайдеру:

```json
{
  "error": {
    "message": "Request contains regulated internal compliance content and was blocked by regulated-topic policy.",
    "type": "regulated_topic_policy_violation",
    "code": "regulated_topic_policy_blocked",
    "details": {
      "categories": ["sanctions_screening"],
      "rules": ["sanctions_watchlist_matching"],
      "actions": ["block"]
    }
  }
}
```

Ответ, structured logs, `gateway_guardrail_audit` и метрика `ru_regulated_topic_policy_blocked_total` содержат только bounded categories/rule ids/actions/counts, без raw prompt, raw matched text, snippets или offsets. Public defaults не содержат organization-specific confidential terms. Если нужны внутренние правила, добавьте operator-defined block-only regex rules через `REGULATED_TOPIC_POLICY_EXTRA_RULES_JSON`.

```env
REGULATED_TOPIC_POLICY_EXTRA_RULES_JSON=[{"category":"internal_watchlist","rule_id":"custom_watchlist_codename","action":"block","pattern":"PROJECT_MARS_WATCHLIST","flags":"i"}]
```

Mask и dictionary-substitute actions в первой версии не включены; они должны добавляться отдельно вместе с reversible dictionary substitution из #25.

## Pre-egress config/log policy

`PRE_EGRESS_POLICY_MODE=block` включён по умолчанию и работает раньше Presidio Analyzer. Он останавливает целые operational payloads: `.env` dumps с секретами, kubeconfig/Kubernetes manifests, nginx configs, access/auth logs и stack traces.

При срабатывании запрос не отправляется в Analyzer и провайдеру, а Redis mapping `pii_mapping:*` не создаётся:

```json
{
  "error": {
    "message": "Request contains configuration or log data and was blocked by pre-egress policy.",
    "type": "pre_egress_policy_violation",
    "code": "pre_egress_policy_blocked",
    "details": {
      "categories": ["config"],
      "rules": ["env_secret_assignment"]
    }
  }
}
```

Ответ и structured logs содержат только bounded categories/rule ids/counts, без raw payload, snippets, offsets или secret values. Если нужно временно разрешить такие payloads в dev-среде, задайте `PRE_EGRESS_POLICY_MODE=off`; PII mask/block при этом продолжит работать отдельно. После изменения этой переменной в `.env` пересоздайте контейнер LiteLLM: `docker compose up -d --force-recreate --no-deps litellm`.

В зависимости от LiteLLM/FastAPI wrapper JSON может быть обёрнут как `detail.error`, `error.provider_specific_fields.error` или `error.param.pre_egress_policy`, но поля `message`, `type`, `code`, `details.categories` и `details.rules` остаются обязательными.

Black-box smoke с test-only LiteLLM proxy и mock upstream проверяет `/v1/chat/completions`, `/v1/responses` и `/v1/messages`: clean prompt доходит до Analyzer/provider, а blocked config payload не доходит ни до Analyzer, ни до provider:

```bash
make test-pre-egress-proxy
```

## Final payload leak check

`FINAL_PAYLOAD_LEAK_CHECK_MODE=block` включён по умолчанию и работает после proxy-side mutation: PII masking уже применён к mutable request text fields, а provider-bound request containers `messages` / `input` / `instructions` / `system`, `tools` / `tool_choice`, legacy `functions` / `function_call`, `prediction`, `response_format`, `text`, provider-specific `extra_body`, `stop` / `stop_sequences`, `prompt_cache_key`, `safety_identifier`, `web_search_options`, `user` и provider `metadata` дополнительно просканированы без мутации. Внешний provider на этом этапе ещё не вызван. Этот слой останавливает configured canaries из `FINAL_PAYLOAD_LEAK_CHECK_CANARIES` и high-confidence raw leak markers вроде `BEGIN PRIVATE KEY`, bearer/JWT-like tokens, provider-key-like values и env-secret-like assignments.

При срабатывании запрос не отправляется провайдеру. Canonical guardrail body:

```json
{
  "error": {
    "message": "Request contains a confirmed raw leak marker and was blocked before provider egress.",
    "type": "final_payload_leak_check_violation",
    "code": "final_payload_leak_check_blocked",
    "details": {
      "rules": ["configured_canary"]
    }
  }
}
```

LiteLLM proxy может завернуть этот body и вернуть `error.code="422"`, сохранив безопасное сообщение. Ответ, structured logs и метрика `ru_final_payload_leak_check_blocked_total` содержат только bounded rule ids/counts, без raw matched values, prompt snippets, offsets, provider keys или mapping contents. Если нужно временно отключить слой в dev-среде, задайте `FINAL_PAYLOAD_LEAK_CHECK_MODE=off`; PII mask/block и `PRE_EGRESS_POLICY_MODE` продолжат работать отдельно.

Black-box smoke с test-only LiteLLM proxy и mock OpenAI-compatible upstream проверяет, что Analyzer видит configured canary/private-key marker, tool schema canary блокируется без provider egress, а sanitized PII prompt доходит до provider только с placeholder:

```bash
make test-final-leak-proxy
```

## Sticky routing

Если за моделью настроено несколько deployments, LiteLLM должен удерживать один клиентский ключ на одном healthy deployment. Для быстрой проверки:

```bash
make routing-smoke
```

Команда отправляет два live-запроса одним ключом и сравнивает header `x-litellm-model-id`.

Если хотите проверять не master key, а пользовательский virtual key, задайте его в `.env`:

```env
LITELLM_ROUTING_TEST_KEY=sk-...
```

Подробности настройки нескольких аккаунтов одной модели: [routing.md](routing.md).

## Metrics

LiteLLM и PII guardrail метрики доступны через Prometheus endpoint:

```bash
curl -s "$API_URL/metrics" | grep -E '^(litellm_|ru_)' | head
```

То же самое через Makefile:

```bash
make metrics
make monitor-smoke
```

PII guardrail метрики `ru_pii_guardrail_*` появятся после первого запроса, который прошёл через guardrail. Метрики не содержат raw PII или текст пользовательского запроса.

## Клиентские гайды

- Codex CLI / Codex App local tasks: [clients/codex.md](clients/codex.md)
- Claude Code: [clients/claude-code.md](clients/claude-code.md)
- OpenCode CLI / Desktop: [clients/opencode.md](clients/opencode.md)
- Kilo Code VS Code / CLI: [clients/kilo-code.md](clients/kilo-code.md)
- JWT/OIDC proxy auth: [clients/jwt.md](clients/jwt.md)

## Добавление моделей

По умолчанию настроены provider-prefixed aliases для Z.AI, OpenAI и Anthropic. Чтобы добавить ещё один провайдер, добавьте модель в `litellm-config.yaml`, добавьте нужный API key в `.env` и перезапустите LiteLLM:

```yaml
model_list:
  - model_name: my-openai-model
    litellm_params:
      model: openai/gpt-5.4-mini
      api_key: os.environ/OPENAI_API_KEY
    model_info:
      id: openai-gpt-5-4-mini-primary
      base_model: gpt-5.4-mini
      access_groups: ["openai", "standard"]
```

```bash
make restart
```

Если это второй deployment той же публичной модели, оставьте прежний `model_name`, но задайте новый `model_info.id`. Так LiteLLM сможет корректно хранить sticky affinity.

## Streaming

LiteLLM принимает streaming requests, а `ru-pii-mask-post` восстанавливает request-scoped placeholders в streaming `delta.content` и `delta.reasoning_content`. Guardrail удерживает возможный суффикс placeholder между чанками, поэтому `<PHONE_` в одном чанке и `NUMBER_1>` в следующем клиент получит как исходное значение из Redis mapping.

```bash
curl -sS "$API_URL/v1/chat/completions" \
  -H "Authorization: Bearer $RU_LLM_PROXY_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "glm-5.1",
    "stream": true,
    "guardrails": ["ru-pii-mask-pre", "ru-pii-mask-post"],
    "messages": [
      {"role": "user", "content": "Проверь телефон +79031234567"}
    ]
  }'
```

Streaming restoration покрывает текстовые deltas. Если провайдер стримит placeholders внутри tool/function-call argument deltas, они могут остаться в ответе как placeholders; raw PII при этом не раскрывается.
