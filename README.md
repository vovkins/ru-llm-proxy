# ru-llm-proxy 🛡️

LLM-прокси для командной работы с внешними LLM через серверные provider keys, LiteLLM virtual keys и русскоязычные PII guardrails.

Прокси обнаруживает и маскирует чувствительные данные в запросах перед отправкой провайдеру, умеет блокировать PII по policy mode и восстанавливает оригинальные данные в ответах, если модель вернула плейсхолдеры. Клиенты работают через OpenAI-compatible и Anthropic-compatible API, а внешний LLM получает обезличенный текст.

## Статус проекта

✅ **Готово в текущем `main`**:

- LiteLLM gateway с server-funded upstream keys (`ZAI_API_KEY`, `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`) и client access через LiteLLM virtual keys.
- Русскоязычный PII guardrail: regex recognizers, DeepPavlov NER, reversible Redis mapping, coverage для Chat Completions, базовых Anthropic Messages `content` string/text blocks и Responses API text payloads (`instructions`, `input`, message-like items, tool-call arguments, tool-output text и text blocks).
- `PII_GUARDRAIL_MODE=mask|block`: reversible masking по умолчанию или безопасный `422` до provider call.
- `DICTIONARY_SUBSTITUTIONS_ENABLED=true`: обратимые business dictionary substitutions до Analyzer; default seed содержит 10 крупных российских банков в `litellm_guardrails/dictionary-substitutions.default.json`.
- `SYNTHETIC_PII_ALLOWLIST_MODE=off|allow`: выключенный по умолчанию narrow allowlist для явно заданных synthetic/test PII fixtures, которые нужны для проверок и демонстраций.
- `REGULATED_TOPIC_POLICY_MODE=off|block`: conservative block-only policy pack для high-confidence AML/CFT / ПОД/ФТ, sanctions-screening, transaction-monitoring, suspicious-activity и compliance-bypass тем.
- Non-streaming restoration для `content`, `reasoning_content`, response content blocks и tool/function arguments.
- Streaming restoration для `delta.content` и `delta.reasoning_content`, включая placeholders, разорванные между чанками.
- Bounded Presidio Analyzer capacity: worker count, concurrency limit, queue limit/timeout и fail-closed overload handling.
- Reused Redis/httpx guardrail clients with pool limits for analyzer and mapping dependencies.
- Calibrated Russian recognizer thresholds: checksum validation for `RU_INN` and a tighter baseline `RU_ADDRESS` corpus.
- Counterparty and bank-requisite recognizers: `RU_KPP`, `RU_OGRN`, `RU_OGRNIP`, `RU_BIK`, `RU_SETTLEMENT_ACCOUNT`, `RU_CORRESPONDENT_ACCOUNT`.
- Infrastructure/secret recognizers: `INTERNAL_IP`, `INTERNAL_DOMAIN`, `HOSTNAME`, `DB_URL`, `JWT`, `BEARER_TOKEN`, `PRIVATE_KEY`, `API_KEY`, `LOGIN`, `PASSWORD`.
- Production egress-control guidance and Kubernetes/Cilium templates for deny-by-default runtime networking: [docs/egress-controls.md](docs/egress-controls.md), [deploy/kubernetes/egress](deploy/kubernetes/egress).
- Production admin/operator access guidance: [docs/admin-access.md](docs/admin-access.md) separates client virtual keys, upstream provider keys, Admin UI credentials and `LITELLM_MASTER_KEY`.
- Sticky routing diagnostics, baseline CI, local guardrails smoke canary и FastAPI lifespan startup.

⚠️ **Текущие ограничения** — восстановление возможно только для плейсхолдеров или dictionary replacements, которые провайдер вернул в ответе точно. Streaming restoration поддерживает текстовые deltas (`content`, `reasoning_content`); streaming tool/function-call argument deltas пока не переписываются. Synthetic/test PII allowlist не является механизмом пропуска production PII и должен содержать только контролируемые тестовые значения. Regulated-topic policy остаётся block-only и не выполняет semantic rewriting.

## Что маскируется

| Тип данных | Entity | Метод |
|-----------|--------|-------|
| Телефоны | `PHONE_NUMBER` | Regex + валидация количества цифр |
| Email | `EMAIL_ADDRESS` | Regex |
| ИНН | `RU_INN` | Regex + checksum для 10/12 цифр; bare 12-digit INN включён по умолчанию, 10-digit требует контекст |
| КПП | `RU_KPP` | Regex + сильный налоговый/реквизитный контекст |
| ОГРН | `RU_OGRN` | Regex + checksum для 13 цифр |
| ОГРНИП | `RU_OGRNIP` | Regex + checksum для 15 цифр |
| СНИЛС | `RU_SNILS` | Regex + checksum |
| Паспорт РФ | `RU_PASSPORT` | Regex + проверка региона |
| Банковские карты | `CREDIT_CARD` | Regex + Luhn |
| БИК | `RU_BIK` | Regex + банковский контекст + базовая структурная проверка |
| Расчётный счёт | `RU_SETTLEMENT_ACCOUNT` | Regex + сильный контекст; при наличии БИК рядом проверяется контрольный ключ |
| Корреспондентский счёт | `RU_CORRESPONDENT_ACCOUNT` | Regex + сильный контекст/prefix `301`; при наличии БИК рядом проверяется контрольный ключ |
| Внутренние IP | `INTERNAL_IP` | Regex + `ipaddress` validation для private/loopback/link-local/CGNAT/ULA ranges; public IP опционален |
| Внутренние домены | `INTERNAL_DOMAIN` | Regex по настраиваемым internal suffixes |
| Hostname | `HOSTNAME` | Context-bound key/value hostname patterns |
| DB/service URL с credentials | `DB_URL` | Credential-bearing URL patterns |
| JWT | `JWT` | Base64url JSON header/payload validation |
| Bearer token | `BEARER_TOKEN` | Authorization/Bearer token patterns |
| Private key | `PRIVATE_KEY` | PEM private key block patterns |
| API key/token | `API_KEY` | Provider-specific keys и context-bound token assignments |
| Login | `LOGIN` | Context-bound login/user key-value pairs |
| Password | `PASSWORD` | Context-bound password/passwd/pwd key-value pairs |
| Адреса | `RU_ADDRESS` | Ограниченный regex corpus российских адресов |
| ФИО | `PERSON` | DeepPavlov `ner_rus_bert`, если модель загружена |
| Организации | `ORGANIZATION` | DeepPavlov `ner_rus_bert`, если модель загружена |
| Города/локации | `LOCATION` | DeepPavlov `ner_rus_bert`, если модель загружена |

Текущий `main` использует `score_threshold=0.35`. `RU_INN` всегда проходит checksum validation; по умолчанию `PRESIDIO_ANALYZER_DETECT_BARE_INN_BY_CHECKSUM=true`, поэтому checksum-valid bare ИНН без контекстного слова проходит дефолтный порог. Если включить strict mode (`false`), голый ИНН требует контекст вроде `ИНН` или `налоговый`. `RU_ADDRESS` остаётся ограниченным regex-based покрытием базовых российских адресных форматов.

Реквизиты контрагентов детектируются консервативно. `RU_KPP`, `RU_BIK`, `RU_SETTLEMENT_ACCOUNT` и `RU_CORRESPONDENT_ACCOUNT` требуют явный контекст вроде `КПП`, `БИК`, `расчетный счет`, `р/с`, `корреспондентский счет` или `к/с`, поэтому случайные 9- и 20-значные числа не проходят дефолтный порог. Для счетов при наличии контекстного БИК рядом проверяется российский контрольный ключ; справочник банков/актуальность БИК по ЦБ не запрашивается.

Infrastructure/secret recognizers работают на entity-level внутри обычного `PII_GUARDRAIL_MODE=mask|block`: одиночный private IP, internal domain, JWT или bearer token может быть замаскирован или заблокирован без классификации всего prompt как `.env`/log/config artifact. Доменные suffixes задаются через `PRESIDIO_ANALYZER_INTERNAL_DOMAIN_SUFFIXES`; публичные IP по умолчанию не считаются `INTERNAL_IP`, но могут быть включены через `PRESIDIO_ANALYZER_DETECT_PUBLIC_IPS=true`.

Regulated-topic policy не является PII recognizer и не маскирует entity spans. При `REGULATED_TOPIC_POLICY_MODE=block` он блокирует high-confidence внутренние AML/CFT / ПОД/ФТ, санкционные, transaction-monitoring, suspicious-activity и compliance-bypass темы до Analyzer, Redis mapping и provider egress. Public defaults не содержат organization-specific confidential terms; свои block-only regex rules можно добавить через `REGULATED_TOPIC_POLICY_EXTRA_RULES_JSON`.

Synthetic/test PII allowlist — это отдельная narrow exception после Presidio Analyzer и до `PII_GUARDRAIL_MODE=mask|block`. При `SYNTHETIC_PII_ALLOWLIST_MODE=allow` guardrail удаляет из результатов Analyzer только явно настроенные synthetic spans по exact values или anchored safe regex patterns. Остальная PII продолжает маскироваться или блокироваться по текущей политике.

Dictionary substitutions — отдельная product policy до Presidio Analyzer, а не новый PII recognizer. При `DICTIONARY_SUBSTITUTIONS_ENABLED=true` guardrail сначала заменяет настроенные business terms на synthetic replacements, затем исключает эти replacement spans из PII mask/block, сохраняет restore mapping в Redis и восстанавливает replacement обратно клиенту в non-streaming и streaming ответах. Default seed заменяет 10 крупных российских банков; оператор может заменить файл или передать JSON через env. Восстановление exact-match: если модель склоняет, переводит или перефразирует replacement, proxy не сможет восстановить исходную форму.

DeepPavlov NER соблюдает параметры Analyzer API: если в запросе указан `entities`, NER запускается только для `PERSON`, `ORGANIZATION` или `LOCATION`; если запрошены только regex-типы вроде `RU_INN`, NER пропускается. Так как DeepPavlov не возвращает per-entity confidence, проект присваивает NER-результатам фиксированный score `0.7` и не запускает NER при `score_threshold > 0.7`.

## Архитектура

```text
┌──────────┐     ┌──────────────┐     ┌────────────────────┐     ┌──────────────┐
│  Клиент  │────▶│ LiteLLM Proxy│────▶│  PII Guardrail     │────▶│ LLM Provider │
│          │     │  порт 4000   │     │ pre-egress + PII   │     │   glm-5.1    │
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
3. Guardrail собирает provider-bound строковые поля: `message.content`, Anthropic top-level `system` string/text blocks, Responses API `instructions` / `input` string/list text items, tool-call `arguments`, tool-output `output` string/list text items, text content blocks, `tool_calls[].function.arguments` и `function_call.arguments`.
4. Regulated-topic policy, если включён, проверяет high-confidence AML/CFT / ПОД/ФТ, sanctions-screening, transaction-monitoring, suspicious-activity и compliance-bypass темы. При срабатывании запрос заканчивается безопасной `422` ошибкой до Analyzer, Redis mapping и провайдера.
5. Pre-egress classifier проверяет эти поля на `.env` secret dumps, kubeconfig/Kubernetes manifests, nginx configs, access/auth logs и stack traces. При срабатывании запрос заканчивается безопасной `422` ошибкой до Analyzer, Redis mapping и провайдера.
6. Dictionary substitution policy, включённая по умолчанию, заменяет configured business terms до Analyzer. Default file `dictionary-substitutions.default.json` содержит 10 крупных российских банков; replacement spans исключаются из последующего PII mask/block.
7. Guardrail отправляет уже substituted строковые поля запроса в Presidio Analyzer через `POST /api/v1/analyze`.
8. Analyzer возвращает entity spans, entity types и scores.
9. Если включён `SYNTHETIC_PII_ALLOWLIST_MODE=allow`, guardrail вычитает из результатов Analyzer только явно настроенные synthetic/test PII spans. Hits пишутся в safe logs/metrics без raw values.
10. В `PII_GUARDRAIL_MODE=block` при найденной PII вне dictionary replacement spans поток останавливается безопасной `422` ошибкой: provider не вызывается, request payload не меняется, Redis mapping не создаётся.
11. В `PII_GUARDRAIL_MODE=mask` guardrail строит уникальные плейсхолдеры: `<PHONE_NUMBER_1>`, `<PHONE_NUMBER_2>`, `<RU_INN_1>`, `<RU_BIK_1>`, `<INTERNAL_IP_1>`, `<BEARER_TOKEN_1>` и применяет masked text к provider-bound request fields.
12. Final payload leak check сканирует уже provider-bound payload после dictionary substitution и masking, до provider call, включая request containers `messages` / `input` / `instructions` / `system` (в том числе Anthropic Messages `system` и `tool_use` blocks), `tools` / `tool_choice`, legacy `functions` / `function_call`, `prediction`, `response_format`, `text`, provider-specific `extra_body`, `stop` / `stop_sequences`, `prompt_cache_key`, `safety_identifier`, `web_search_options`, `user` и provider `metadata`. Этот scan-only слой не расширяет PII masking/Redis mapping на служебные provider поля.
13. При final-check блокировке guardrail откатывает masked/substituted text обратно к исходному request и возвращает безопасную `422` ошибку без Redis mapping и provider egress.
14. Если финальная проверка чистая, guardrail сохраняет combined restore mapping в Redis с TTL `PII_MAPPING_TTL_SECONDS`; при Redis save failure guardrail откатывает mutated text обратно к исходному request, чтобы не отправлять необратимые placeholders/replacements без mapping. Для dictionary mapping failure default — `fail_closed`.
15. Guardrail записывает server-side `pii_request_id` в internal metadata и LiteLLM отправляет masked/substituted request LLM-провайдеру.
16. LiteLLM запускает `ru-pii-mask-post` в режиме `post_call`.
17. Guardrail восстанавливает плейсхолдеры и exact dictionary replacements в `content`, `reasoning_content`, response content blocks, `tool_calls[].function.arguments` и `function_call.arguments`.
18. Для streaming responses `async_post_call_streaming_iterator_hook` восстанавливает placeholders/replacements в `delta.content` и `delta.reasoning_content`, включая значения, разорванные между чанками.
19. Redis mapping удаляется после post-call или streaming-iterator обработки.

Маскирование и восстановление выполняются внутри LiteLLM guardrail. Отдельный сервис анонимизации не используется в текущем request path и удалён из runtime-состава проекта.

## spaCy и DeepPavlov: кто за что отвечает

В проекте используются две разные NLP-модели, они не являются обёртками друг над другом.

| Модель | Где используется | Роль |
|--------|------------------|------|
| `ru_core_news_sm` | Presidio Analyzer / spaCy NLP engine | Токенизация и базовая языковая обработка для Presidio |
| DeepPavlov `ner_rus_bert` | `presidio/ner/deeppavlov_recognizer.py` | NER для `PERSON`, `LOCATION`, `ORGANIZATION` |

Regex recognizers отвечают за структурированные российские PII, реквизиты и infrastructure/secret entities: телефоны, ИНН, КПП, ОГРН/ОГРНИП, БИК, расчётные/корреспондентские счета, СНИЛС, паспорта, карты, email, адреса, private/internal IP/domain/hostname markers, DB/service URLs с credentials, JWT/bearer/API keys, private keys, login/password pairs. DeepPavlov добавляет NER-сущности поверх этого результата.

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
DISABLE_ADMIN_UI=False          # set True for API-only production deployments
POSTGRES_PASSWORD=...           # автогенерируется через make setup
LITELLM_DB_URL=postgresql://litellm:...@db:5432/litellm
REDIS_URL=redis://redis:6379
PRESIDIO_ANALYZER_URL=http://presidio-analyzer:5001
PRESIDIO_ANALYZER_WORKERS=1
PRESIDIO_ANALYZER_CONCURRENCY_LIMIT=1
PRESIDIO_ANALYZER_QUEUE_LIMIT=8
PRESIDIO_ANALYZER_QUEUE_TIMEOUT_SECONDS=0.25
PRESIDIO_ANALYZER_DETECT_BARE_INN_BY_CHECKSUM=true
PRESIDIO_ANALYZER_INTERNAL_DOMAIN_SUFFIXES=internal,local,lan,corp,corp.local,cluster.local,svc.cluster.local
PRESIDIO_ANALYZER_DETECT_PUBLIC_IPS=false
PII_GUARDRAIL_MODE=mask
DICTIONARY_SUBSTITUTIONS_ENABLED=true
DICTIONARY_SUBSTITUTIONS_FILE=/app/litellm_guardrails/dictionary-substitutions.default.json
DICTIONARY_SUBSTITUTIONS_JSON=
DICTIONARY_SUBSTITUTIONS_FAILURE_MODE=fail_closed
SYNTHETIC_PII_ALLOWLIST_MODE=off
SYNTHETIC_PII_ALLOWLIST_JSON=[]
REGULATED_TOPIC_POLICY_MODE=off
REGULATED_TOPIC_POLICY_EXTRA_RULES_JSON=
PRE_EGRESS_POLICY_MODE=block
FINAL_PAYLOAD_LEAK_CHECK_MODE=block
FINAL_PAYLOAD_LEAK_CHECK_CANARIES=
PII_GUARDRAIL_FAILURE_MODE=fail_open
PII_MAPPING_TTL_SECONDS=3600
PII_GUARDRAIL_REDIS_MAX_CONNECTIONS=20
PII_GUARDRAIL_REDIS_SOCKET_CONNECT_TIMEOUT_SECONDS=1.0
PII_GUARDRAIL_REDIS_SOCKET_TIMEOUT_SECONDS=2.0
PII_GUARDRAIL_ANALYZER_TIMEOUT_SECONDS=30.0
PII_GUARDRAIL_ANALYZER_CONNECT_TIMEOUT_SECONDS=5.0
PII_GUARDRAIL_ANALYZER_MAX_CONNECTIONS=20
PII_GUARDRAIL_ANALYZER_MAX_KEEPALIVE_CONNECTIONS=10
```

`make setup` не перезаписывает уже заданные реальные секреты. Если `.env` уже существует, команда добавит отсутствующие `UI_USERNAME` / `UI_PASSWORD`, `DISABLE_ADMIN_UI`, опциональные routing/client-smoke переменные, Analyzer capacity defaults, dictionary substitution env vars, `SYNTHETIC_PII_ALLOWLIST_MODE`, `SYNTHETIC_PII_ALLOWLIST_JSON`, `REGULATED_TOPIC_POLICY_MODE`, `REGULATED_TOPIC_POLICY_EXTRA_RULES_JSON`, `PRE_EGRESS_POLICY_MODE`, final leak-check env vars и заменит только placeholder-значения.

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
| `PRESIDIO_ANALYZER_DETECT_BARE_INN_BY_CHECKSUM` | `true` | Детектировать checksum-valid bare 12-digit INN без контекстного слова при API `score_threshold=0.35`. 10-digit INN требует контекст вроде `ИНН` или `налогоплательщик` даже в default mode. Если `false`, любой голый ИНН требует контекст. |
| `PRESIDIO_ANALYZER_INTERNAL_DOMAIN_SUFFIXES` | `internal,local,lan,corp,corp.local,cluster.local,svc.cluster.local` | Comma/space-separated suffixes, которые `INTERNAL_DOMAIN` считает внутренними. |
| `PRESIDIO_ANALYZER_DETECT_PUBLIC_IPS` | `false` | Если `true`, `INTERNAL_IP` также детектирует global public IP; по умолчанию ловятся только private/internal ranges. |

Эффективный лимит активных model calls: `replicas * PRESIDIO_ANALYZER_WORKERS * PRESIDIO_ANALYZER_CONCURRENCY_LIMIT`. Память оценивайте как `replicas * PRESIDIO_ANALYZER_WORKERS * measured_RSS_per_worker + headroom`.

При перегрузке Analyzer возвращает `503` с reason `queue_full` или `queue_timeout`. LiteLLM guardrail трактует `analyzer_overloaded` как fail-closed override независимо от `PII_GUARDRAIL_FAILURE_MODE`: запрос останавливается, чтобы не отправить raw PII провайдеру. Для PII-sensitive окружений дополнительно используйте `fail_closed` для остальных инфраструктурных сбоев и масштабируйте Analyzer workers/replicas под доступную память.

Recognizer calibration:

- `RU_INN` всегда проходит checksum validation. По умолчанию `PRESIDIO_ANALYZER_DETECT_BARE_INN_BY_CHECKSUM=true`, поэтому checksum-valid bare 12-digit INN проходит дефолтный Analyzer API `score_threshold=0.35`; 10-digit INN без контекста остаётся ниже threshold, потому что около 10% случайных 10-значных чисел проходят checksum. Для 10-digit detection нужен контекст вроде `ИНН`, `налогоплательщик`, `налоговый`.
- В strict mode (`PRESIDIO_ANALYZER_DETECT_BARE_INN_BY_CHECKSUM=false`) любой голый ИНН без контекста не проходит `score_threshold=0.35`; для детекции нужен контекст.
- `RU_KPP`, `RU_BIK`, `RU_SETTLEMENT_ACCOUNT` и `RU_CORRESPONDENT_ACCOUNT` требуют сильный контекст и не детектируют голые digit runs при `score_threshold=0.35`. `RU_OGRN` и `RU_OGRNIP` проходят checksum validation; невалидный контрольный разряд отбрасывается. Для расчётных и корреспондентских счетов при наличии БИК рядом выполняется cross-field проверка контрольного ключа; без БИК используется только сильный контекст и структурные ограничения. Проект не делает online lookup по справочнику БИК ЦБ.
- Infrastructure/secret recognizers используют high-confidence правила. `INTERNAL_IP` по умолчанию покрывает private/loopback/link-local/CGNAT/ULA ranges, `INTERNAL_DOMAIN` ограничен `PRESIDIO_ANALYZER_INTERNAL_DOMAIN_SUFFIXES`, `HOSTNAME`, `LOGIN` и `PASSWORD` требуют key-value context, `JWT` проверяет decodable JSON header/payload, а generic API token assignments отбрасывают obvious placeholder values.
- `RU_ADDRESS` остаётся ограниченным regex recognizer. Поддерживаются базовые формы вроде `ул. Ленина, д. 10`, `ул Ленина 10`, `Тверская улица, дом 7`, но полноценный разбор индексов, регионов, владений и всех свободных российских адресов вне текущего scope. Сокращения street type требуют границу слева, а форма `Тверская улица, дом 7` требует явное `дом`/`д.`, чтобы не маскировать фразы вроде `стул Иванова 10 раз` или `Тверская улица 10 лет`.

Runtime dependency clients guardrail:

| Переменная | По умолчанию | Назначение |
| --- | --- | --- |
| `PII_GUARDRAIL_REDIS_MAX_CONNECTIONS` | `20` | Максимум Redis connections в shared pool guardrail на один процесс/event loop LiteLLM. |
| `PII_GUARDRAIL_REDIS_SOCKET_CONNECT_TIMEOUT_SECONDS` | `1.0` | Таймаут установки Redis connection для PII mapping store. |
| `PII_GUARDRAIL_REDIS_SOCKET_TIMEOUT_SECONDS` | `2.0` | Таймаут Redis операций `setex`, `get`, `delete`. |
| `PII_GUARDRAIL_ANALYZER_TIMEOUT_SECONDS` | `30.0` | Общий read/write/pool timeout HTTP-вызова Presidio Analyzer из guardrail. |
| `PII_GUARDRAIL_ANALYZER_CONNECT_TIMEOUT_SECONDS` | `5.0` | Таймаут установки HTTP connection к Analyzer. |
| `PII_GUARDRAIL_ANALYZER_MAX_CONNECTIONS` | `20` | Максимум HTTP connections к Analyzer в shared client на один процесс/event loop LiteLLM. |
| `PII_GUARDRAIL_ANALYZER_MAX_KEEPALIVE_CONNECTIONS` | `10` | Максимум keep-alive HTTP connections к Analyzer в shared client. |

Эти настройки ограничивают dependency clients внутри LiteLLM guardrail. Они не заменяют `PRESIDIO_ANALYZER_*` capacity limiter: Analyzer всё равно отдельно контролирует, сколько inference jobs одновременно выполняется внутри каждого worker.

При graceful shutdown или тестовом reset shared clients нужно закрывать через `close_guardrail_dependency_clients()`: helper очищает process-local caches и вызывает закрытие HTTPX/Redis pools. В штатном Docker Compose stop процесс завершается целиком, но для embedded/custom hosting или test harness этот helper должен быть частью teardown.

### PII policy mode

`PII_GUARDRAIL_MODE` управляет штатным поведением после успешной детекции PII:

| Значение | Поведение |
| --- | --- |
| `mask` | Значение по умолчанию. Guardrail маскирует PII, сохраняет Redis mapping и отправляет masked request провайдеру. |
| `block` | Guardrail отклоняет запрос с найденной PII на pre-call этапе. Провайдер не вызывается, Redis mapping не создаётся. |

В block mode клиент получает безопасную `422` ошибку с entity types, но без raw PII, offsets или текста запроса.

`PII_GUARDRAIL_FAILURE_MODE` остаётся отдельной настройкой для инфраструктурных сбоев Presidio/Redis: `fail_open` пропускает запрос дальше, `fail_closed` останавливает его. Перегрузка Analyzer (`analyzer_overloaded`) всегда обрабатывается как fail-closed.

### Dictionary substitutions

`DICTIONARY_SUBSTITUTIONS_ENABLED=true` по умолчанию. Этот слой запускается до Presidio Analyzer и заменяет business terms на synthetic replacements, не завися от DeepPavlov `ORGANIZATION` detection.

| Переменная | По умолчанию | Назначение |
| --- | --- | --- |
| `DICTIONARY_SUBSTITUTIONS_ENABLED` | `true` | Включает deterministic reversible substitutions. |
| `DICTIONARY_SUBSTITUTIONS_FILE` | `/app/litellm_guardrails/dictionary-substitutions.default.json` | JSON-файл с правилами. |
| `DICTIONARY_SUBSTITUTIONS_JSON` | empty | Inline JSON override; если задан, используется вместо файла. |
| `DICTIONARY_SUBSTITUTIONS_FAILURE_MODE` | `fail_closed` | Поведение при invalid config, ambiguous request или Redis save failure для dictionary mapping. |

Default seed содержит 10 крупных российских банков: `Сбербанк`, `ВТБ`, `Газпромбанк`, `Альфа-Банк`, `ПСБ`, `Россельхозбанк`, `Т-Банк`, `Московский кредитный банк`, `Банк Дом.РФ`, `Совкомбанк`. Список нужен как стартовая business dictionary policy и должен адаптироваться оператором под свой контур.

Пример правила:

```json
{
  "substitutions": [
    {
      "id": "tbank_to_zetta",
      "enabled": true,
      "source": "Т-Банк",
      "replacement": "Зетта Групп",
      "match": {
        "case_sensitive": false,
        "whole_phrase": true
      },
      "restore": true
    }
  ]
}
```

Поведение:

```text
Client request:   Проверь договор с Т-Банк.
Provider request: Проверь договор с Зетта Групп.
Client response:  ... Т-Банк ...
```

Dictionary replacement spans исключаются из последующего PII mask/block, поэтому synthetic replacement отправляется провайдеру как настроенное значение, а не как `<ORGANIZATION_1>`. Если в исходном запросе уже есть replacement text, например одновременно `Т-Банк` и `Зетта Групп`, запрос считается ambiguous и при default `fail_closed` останавливается: иначе post-call restore мог бы заменить чужое совпадение. Восстановление exact-match only: склонения, переводы, сокращения или paraphrase replacement не восстанавливаются.

### Synthetic/test PII allowlist

`SYNTHETIC_PII_ALLOWLIST_MODE=off` по умолчанию. Режим нужен только для контролируемых synthetic/test fixtures, которые должны проходить через proxy в демонстрациях, smoke-тестах или проверочных наборах без маскирования/блокировки. Он не должен использоваться для production PII.

| Значение | Поведение |
| --- | --- |
| `off` | Значение по умолчанию. Любая найденная PII обрабатывается обычным `PII_GUARDRAIL_MODE`. |
| `allow` | Guardrail вычитает из результатов Analyzer только явно настроенные synthetic/test spans, затем оставшиеся сущности маскируются или блокируются как обычно. |

Правила задаются через `SYNTHETIC_PII_ALLOWLIST_JSON`. Поддерживаются exact values и safe regex patterns. Regex должен быть anchored (`^...$` или `\A...\Z`) и ссылаться на контролируемый synthetic namespace вроде `example.test`, `TEST_`, `RU_PROXY_`, `SYNTHETIC_` или `CANARY_`; broad patterns вроде `^.*$` игнорируются.

```env
SYNTHETIC_PII_ALLOWLIST_MODE=allow
SYNTHETIC_PII_ALLOWLIST_JSON=[{"rule_id":"docs_synthetic_contacts","entity_types":["PHONE_NUMBER","EMAIL_ADDRESS"],"values":["+79031234567"],"patterns":["^[A-Za-z0-9._%+-]+@example\\.test$"]}]
```

Allowlist применяется только к PII policy. Он не отключает `PRE_EGRESS_POLICY_MODE`, `REGULATED_TOPIC_POLICY_MODE` или `FINAL_PAYLOAD_LEAK_CHECK_MODE`. Structured logs и `gateway_guardrail_audit` содержат только bounded `rule_id`, entity type и counts; raw allowed values не пишутся.

### Regulated-topic policy

`REGULATED_TOPIC_POLICY_MODE` управляет отдельным conservative policy pack для AML/CFT / ПОД/ФТ и похожих внутренних compliance topics. Этот слой не является PII recognizer: он не ищет entity spans, не создаёт placeholders и не сохраняет Redis mapping.

| Значение | Поведение |
| --- | --- |
| `off` | Значение по умолчанию. Политика не блокирует business/compliance темы, PII и egress layers работают отдельно. |
| `block` | Guardrail отклоняет high-confidence regulated-topic payloads до Presidio Analyzer, Redis mapping и provider egress. |

Public defaults покрывают только rule families без organization-specific confidential terms: `aml_cft_internal_controls`, `sanctions_watchlist_matching`, `transaction_monitoring_thresholds`, `suspicious_activity_playbook` и `compliance_bypass_procedure`. Для ambiguity вроде публичного вопроса “What is AML?” classifier остаётся консервативным и пропускает запрос дальше.

При блокировке клиент получает безопасную `422` ошибку с `code=regulated_topic_policy_blocked`, categories, rule ids и action `block`. Error body, `gateway_guardrail_audit` и structured logs не содержат raw prompt, raw matched text, snippets или offsets. Метрика `ru_regulated_topic_policy_blocked_total` использует bounded labels `category` и `rule_id`.

`REGULATED_TOPIC_POLICY_EXTRA_RULES_JSON` позволяет operator-defined block-only правила без изменения кода:

```json
[
  {
    "category": "internal_watchlist",
    "rule_id": "custom_watchlist_codename",
    "action": "block",
    "pattern": "PROJECT_MARS_WATCHLIST",
    "flags": "i"
  }
]
```

Mask и dictionary-substitute actions намеренно не включены в первую версию. Они должны добавляться отдельно вместе с reversible dictionary substitution из #25, чтобы не создавать ложного ожидания восстановления.

### Pre-egress config/log policy

`PRE_EGRESS_POLICY_MODE` управляет отдельным whole-payload classifier перед Presidio Analyzer и provider egress.

| Значение | Поведение |
| --- | --- |
| `block` | Значение по умолчанию. Guardrail отклоняет высокосигнальные `.env` secret dumps, kubeconfig/Kubernetes manifests, nginx configs, access/auth logs и stack traces до вызова Analyzer и провайдера. |
| `off` | Отключает config/log classifier. PII mask/block продолжает работать по `PII_GUARDRAIL_MODE`. |

При блокировке клиент получает безопасную `422` ошибку с `code=pre_egress_policy_blocked`, categories и rule ids. В зависимости от LiteLLM/FastAPI wrapper эти поля находятся в `error`, `detail.error`, `error.provider_specific_fields.error` или `error.param.pre_egress_policy`; машинно-читаемый контракт `message` / `type` / `code` / `details.categories` / `details.rules` остаётся тем же. Error body и structured logs не содержат raw payload, snippets, offsets или secret values. PII Redis mapping `pii_mapping:*` не создаётся, потому что запрос останавливается до `_save_mapping`.

Этот слой не заменяет PII mask/block: PII policy работает по entity spans и может маскировать/восстанавливать данные, а pre-egress policy останавливает целые операционные артефакты, которые нельзя безопасно отправлять внешнему LLM даже после частичной маскировки.

Если меняете `PRE_EGRESS_POLICY_MODE` в `.env`, пересоздайте контейнер LiteLLM, чтобы Docker Compose передал новое значение окружения:

```bash
docker compose up -d --force-recreate --no-deps litellm
```

### Final payload leak check

`FINAL_PAYLOAD_LEAK_CHECK_MODE` управляет финальной синхронной проверкой provider-bound текста после proxy-side request mutation: PII masking уже применён к mutable request text fields, а request containers `messages` / `input` / `instructions` / `system`, `tools` / `tool_choice`, legacy `functions` / `function_call`, `prediction`, `response_format`, `text`, provider-specific `extra_body`, `stop` / `stop_sequences`, `prompt_cache_key`, `safety_identifier`, `web_search_options`, `user` и provider `metadata` дополнительно сканируются без мутации. Вызова внешнего LLM provider на этом этапе ещё не было.

| Значение | Поведение |
| --- | --- |
| `block` | Значение по умолчанию. Guardrail отклоняет configured canaries и high-confidence raw leak markers вроде `BEGIN PRIVATE KEY`, bearer/JWT-like tokens, provider-key-like values и env-secret-like assignments перед provider egress. |
| `off` | Отключает финальную проверку. PII mask/block и `PRE_EGRESS_POLICY_MODE` продолжают работать отдельно. |

`FINAL_PAYLOAD_LEAK_CHECK_CANARIES` задаёт deterministic canary tokens через запятую или newline. Это regression/smoke механизм для доказательства, что sanitizer miss не доходит до provider. Не используйте реальные секреты как canaries.

При блокировке клиент получает безопасную `422` ошибку; guardrail body использует `code=final_payload_leak_check_blocked`, но LiteLLM proxy может завернуть её как `code=422`. Error body и structured logs не содержат raw matched values, snippets, offsets, prompt text, provider keys или Redis mapping contents. Подтверждённые final-check hits не fail-open’ятся: `PII_GUARDRAIL_FAILURE_MODE` применяется к инфраструктурным сбоям, а не к найденной утечке.

Этот слой не является enterprise DLP и не заменяет внешние контрольные контуры. Его задача уже внутри proxy остановить deterministic canaries и высокосигнальные raw secret markers перед внешним provider call.

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
        - name: "dictionary_substitutions"
          type: "string"
          description: "DICTIONARY_SUBSTITUTIONS_ENABLED: true by default; applies reversible exact business-term replacements from dictionary-substitutions.default.json or DICTIONARY_SUBSTITUTIONS_JSON before Presidio analysis."
        - name: "synthetic_pii_allowlist_mode"
          type: "string"
          description: "SYNTHETIC_PII_ALLOWLIST_MODE: off by default; allow removes explicitly configured synthetic/test PII spans from Presidio results before mask/block handling."
        - name: "synthetic_pii_allowlist_rules"
          type: "string"
          description: "SYNTHETIC_PII_ALLOWLIST_JSON: optional PII-only rules with rule_id, entity_types, exact values, and anchored safe synthetic regex patterns. Matches are logged and counted without raw values."
        - name: "pre_egress_policy_mode"
          type: "string"
          description: "PRE_EGRESS_POLICY_MODE: block rejects high-confidence config/log operational payloads before Presidio analysis and provider calls; off disables this classifier."
        - name: "final_payload_leak_check_mode"
          type: "string"
          description: "FINAL_PAYLOAD_LEAK_CHECK_MODE: block rejects configured canaries and high-confidence raw leak markers after request mutation and before provider calls, including provider-bound request containers (messages/input/instructions/system), tools/tool_choice, legacy functions/function_call, prediction, response_format, text, extra_body, stop/stop_sequences, prompt_cache_key, safety_identifier, web_search_options, user, and provider metadata; off disables this final check."
        - name: "request_fields"
          type: "list[string]"
          description: "Masks message.content, Anthropic Messages system and tool_result.content, Responses API instructions/input string/list text items, tool-call arguments, tool-output output string/list text items, text content blocks, tool_calls[].function.arguments, and function_call.arguments."
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
| `make test` | Быстрый локальный suite: `test-unit` и `test-static` |
| `make test-unit` | Recognizers/NER, guardrail unit tests и deterministic flow |
| `make test-static` | Host lightweight static/asyncio regression tests через локальный `PYTHON_LOCAL` |
| `make test-recognizers` | Unit-тесты recognizers и NER helpers |
| `make test-recognizer-api` | Docker API-level Analyzer recognizer regression tests; отдельный CI gate `recognizer-api`, не входит в быстрый `make test` |
| `make test-guardrail` | Unit-тесты LiteLLM guardrail |
| `make test-flow` | Deterministic проверка mask/unmask без внешнего LLM |
| `make test-routing-diagnostics` | Static regression tests для `routing-smoke` и `guardrails-smoke` Makefile targets |
| `make test-pre-egress-proxy` | Docker smoke с mock provider: pre-egress block не доходит до Analyzer/provider |
| `make test-final-leak-proxy` | Docker smoke с mock provider: final leak-check не доходит до provider после Analyzer miss |
| `make test-egress-security` | Docker egress-security gate: mock provider capture доказывает no raw provider egress |
| `make test-observability-gates` | Lightweight observability/audit gate checks |
| `make test-e2e` | Live smoke test против поднятых сервисов и реального LLM |
| `make virtual-key-create` | DevOps/CI helper: создать LiteLLM virtual key через admin API |
| `make client-auth-smoke` | Проверить client auth и базовые `/v1` protocol smokes |
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

Production Admin UI/API нельзя считать защищёнными только за счёт shared `UI_USERNAME` / `UI_PASSWORD` или `LITELLM_MASTER_KEY`. В production закрывайте `/ui` и admin API routes внешней operator boundary: SSO/OIDC/SAML, VPN, IP allowlist, mTLS, zero-trust proxy или private network. Для API-only deployment можно отключить UI:

```env
DISABLE_ADMIN_UI=True
```

Подробный runbook: [docs/admin-access.md](docs/admin-access.md).

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
- ZCode: [docs/clients/zcode.md](docs/clients/zcode.md)
- OpenCode CLI / Desktop: [docs/clients/opencode.md](docs/clients/opencode.md)
- Kilo Code VS Code / CLI: [docs/clients/kilo-code.md](docs/clients/kilo-code.md)
- JWT/OIDC proxy auth: [docs/clients/jwt.md](docs/clients/jwt.md)

Базовые endpoint contracts:

- OpenAI Chat Completions: `POST /v1/chat/completions`
- OpenAI Responses: `POST /v1/responses`
- Anthropic Messages: `POST /v1/messages`

Proxy auth по умолчанию основан на LiteLLM virtual keys. JWT/OIDC — отдельный enterprise deployment path: он включается только при наличии IdP/JWKS и не заменяет upstream provider keys.

ZCode подключается в API Key mode как OpenAI-compatible клиент: Base URL указывает на proxy `/v1`, а API Key — это `RU_LLM_PROXY_TOKEN`. Серверный `ZAI_API_KEY` остаётся только на proxy; account login `Continue with Z.ai` через proxy не считается поддержанным режимом.

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

## Compliance Evidence

Проверки разделены на три контура: egress-security, observability и live-provider
smoke. Для доказательства отсутствия raw provider egress используйте mock-provider
контур:

```bash
make test-egress-security
```

Для lightweight проверки observability/audit wiring:

```bash
make test-observability-gates
```

Live smoke (`make guardrails-smoke`, `make test-e2e`, `make routing-smoke`) проверяет
совместимость с реальным LiteLLM/provider flow, но не доказывает отсутствие утечки,
потому что фактический provider-bound payload внешнего провайдера проект не
захватывает. Подробная карта evidence gates: [docs/compliance.md](docs/compliance.md).

Production network egress controls вынесены в отдельный слой: deny-by-default
egress, allowlist provider FQDNs и запрет internet egress для Analyzer/Redis/PostgreSQL
описаны в [docs/egress-controls.md](docs/egress-controls.md). Стартовые Kubernetes/Cilium
шаблоны лежат в [deploy/kubernetes/egress](deploy/kubernetes/egress). Local Docker
Compose не считается production egress enforcement.

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
- `ru_pre_egress_policy_blocked_total`
- `ru_final_payload_leak_check_blocked_total`
- `ru_regulated_topic_policy_blocked_total`
- `ru_synthetic_pii_allowlist_hits_total`
- `ru_presidio_analyzer_requests_total`
- `ru_presidio_analyzer_latency_seconds_*`
- `ru_presidio_analyzer_entities_detected_total`
- `ru_presidio_analyzer_capacity_rejections_total`
- `ru_presidio_analyzer_failures_total`

Guardrail также пишет structured JSON logs без prompt text и без raw PII. Для
gateway-level мониторинга используйте `gateway_guardrail_audit`: один safe event
на pre-call решение с `request_id`, `model`, `status`, `latency_ms`,
`policy_result`, `redaction_count`, `entity_counts`, а для блокировок/ошибок —
`block_reason` и `error_code`. Для regulated-topic blocks audit/logs содержат только
bounded `categories`, `rules`, `actions` и counts без raw matched text. Для synthetic/test PII allowlist audit/logs содержат только bounded rule ids, entity types и counts без raw allowed values. Подробный DevOps guide:
[docs/monitoring.md](docs/monitoring.md).

Presidio Analyzer отдельно пишет `presidio_analyzer_request` и отдает metrics на
`http://localhost:5001/metrics`; эти события и метрики содержат только safe
outcome, latency, capacity snapshot и entity type counts без raw input text,
entity values или offsets.

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

Локальный test flow разделен на host lightweight checks и Docker suites. `make test-static`
использует локальный `PYTHON_LOCAL` (`.venv/bin/python`, если есть), а Docker targets не
устанавливают Python-пакеты в окружение хоста.

```bash
make test             # test-unit + test-static
make test-unit        # recognizers, NER helpers, guardrail unit tests, deterministic flow
make test-static      # lightweight static/asyncio checks через локальный PYTHON_LOCAL
make test-recognizers
make test-recognizer-api  # API-level Analyzer recognizer tests; отдельный CI gate recognizer-api
make test-guardrail
make test-flow        # deterministic проверка без внешнего LLM
make test-routing-diagnostics
make test-egress-security # Docker mock-provider egress-security gate
make test-observability-gates # lightweight observability/audit gate checks
make test-e2e         # live smoke test; нужны make up и ZAI_API_KEY
make routing-smoke    # live sticky routing smoke; нужны make up и provider key
make client-auth-smoke # live проверка virtual keys и базовых /v1 protocol smokes
RESPONSES_MODEL=openai-gpt-5.4-mini MESSAGES_MODEL=claude-sonnet-4.6 REQUIRE_ALL_PROTOCOLS=1 make client-auth-smoke
# fail, если нет provider key или live-validated model alias для любого /v1 протокола
```

`make test-flow` проверяет, что PII маскируется до simulated model call и восстанавливается после него. `make test-routing-diagnostics` статически проверяет `routing-smoke` и `guardrails-smoke`: HTTP/network failures, `/v1/chat/completions`, streaming canary wiring, Redis cleanup checks и отсутствие печати proxy token. `make test-egress-security` использует mock provider capture и доказывает, что raw test values не попадают в provider-bound payload, включая regulated-topic block case с `regulated_topic_policy_blocked`. `make test-observability-gates` проверяет lightweight audit/observability wiring и safe-log assertions. `make test-e2e` остаётся live smoke test: реальный провайдер может опустить или переформулировать плейсхолдеры, а сам live smoke не является leakage proof.

`make test-static` также проверяет, что production egress-control guide и
`deploy/kubernetes/egress` templates остаются связаны с основной документацией.

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
├── deploy/
│   └── kubernetes/
│       └── egress/
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
    ├── compliance.md
    ├── egress-controls.md
    ├── examples.md
    ├── monitoring.md
    ├── routing.md
    └── research.md
```

## Лицензия

MIT
