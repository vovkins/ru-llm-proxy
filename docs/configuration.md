# Конфигурация

Этот документ является единым справочником по переменным окружения проекта. `.env.example` намеренно содержит только минимальные значения для быстрого запуска; остальные настройки задаются через `docker-compose.yml`, `litellm-config.yaml`, LiteLLM Admin UI, параметры Makefile или переменные окружения конкретных smoke-проверок.

Правило эксплуатации: секреты и локальные bootstrap-значения лежат в `.env`, модельные алиасы, виртуальные ключи, бюджеты и доступы пользователей администрируются через LiteLLM Admin UI, а продвинутые runtime-политики меняются только осознанно и документируются в change ticket.

## Дефолтный пул GLM-провайдеров

| Переменная | Значение по умолчанию | Допустимые значения | На что влияет |
| --- | --- | --- | --- |
| `ZAI_API_KEY` | Обязательный секрет | Z.AI Coding Plan key | Первый upstream key для дефолтных model groups `glm-5.2` и `glm-5.1`. Остается только на proxy host, клиентам не выдается. |
| `ZAI_API_KEY_2` | Обязательный секрет | Z.AI Coding Plan key | Второй upstream key для того же публичного алиаса. Нужен для пула deployments и проверки sticky routing. |

Оба ключа считаются обязательными даже для quick start: текущий дефолтный `litellm-config.yaml` агрегирует две подписки в один публичный алиас `glm-5.2`, а `glm-5.1` оставляет как дополнительный алиас для установок, которым нужна предыдущая модель.

## Примеры дополнительных провайдеров

| Переменная | Значение по умолчанию | Допустимые значения | На что влияет |
| --- | --- | --- | --- |
| `OPENAI_API_KEY` | Пусто | OpenAI Platform API key | Используется только если администратор добавил OpenAI-compatible aliases из `examples/litellm-config.optional-providers.yaml`. В активном дефолтном конфиге не используется. |
| `ANTHROPIC_API_KEY` | Пусто | Anthropic Console API key | Используется только если администратор добавил Anthropic-compatible aliases из optional provider example. В активном дефолтном конфиге не используется. |
| `ANTHROPIC_BYOK_API_KEY` | Пусто | Anthropic API key пользователя | Только для документационных BYOK passthrough примеров. Не хранится на proxy в server-funded режиме. |

OpenAI/Anthropic model IDs в optional example являются placeholders. Перед production-включением их нужно проверить на текущем LiteLLM image и конкретной подписке.

## LiteLLM Admin UI и секреты

| Переменная | Значение по умолчанию | Допустимые значения | На что влияет |
| --- | --- | --- | --- |
| `LITELLM_MASTER_KEY` | Генерируется `make setup` | Сильный bearer secret, обычно `sk-...` | Главный admin key LiteLLM: создание virtual keys, guardrails diagnostics, admin API. Не используется как клиентский токен. |
| `LITELLM_SALT_KEY` | Генерируется `make setup` | Случайная строка | Соль LiteLLM для безопасного хранения/хеширования ключей. Менять только в рамках контролируемой ротации. |
| `UI_USERNAME` | `admin` | Имя локального admin пользователя | Логин встроенного LiteLLM Admin UI для OSS/local режима. |
| `UI_PASSWORD` | Генерируется `make setup` | Сильный пароль | Пароль встроенного LiteLLM Admin UI. |
| `DISABLE_ADMIN_UI` | `False` | `False`, `True` | Отключает встроенный Admin UI при `True`. В production доступ к UI/API нужно ограничивать сетевым или SSO/reverse-proxy boundary. |
| `JWT_PUBLIC_KEY_URL` | Пусто | URL JWKS/OIDC public keys | Опциональная JWT/OIDC proxy auth настройка LiteLLM Enterprise. |
| `JWT_AUDIENCE` | Пусто | Audience string | Ожидаемая audience для JWT/OIDC auth. |

## Runtime и хранилища LiteLLM

| Переменная | Значение по умолчанию | Допустимые значения | На что влияет |
| --- | --- | --- | --- |
| `LITELLM_PORT` | `4000` | TCP port | Host-side порт, на который Docker Compose публикует LiteLLM. Внутри контейнера LiteLLM слушает `4000`. |
| `LITELLM_DB_URL` | `postgresql://litellm:...@db:5432/litellm` | PostgreSQL URL | Источник для `DATABASE_URL` внутри контейнера LiteLLM. `make setup` обновляет пароль в этой строке при генерации `POSTGRES_PASSWORD`. |
| `DATABASE_URL` | Берется из `LITELLM_DB_URL` | PostgreSQL URL | Переменная, которую читает LiteLLM (`general_settings.database_url`). В compose задается автоматически. |
| `POSTGRES_USER` | `litellm` | PostgreSQL username | Пользователь локального контейнера PostgreSQL. |
| `POSTGRES_PASSWORD` | Генерируется `make setup` | Сильный пароль | Пароль локального PostgreSQL. |
| `POSTGRES_DB` | `litellm` | Имя БД | База данных для LiteLLM persistence. |
| `REDIS_HOST` | `redis` | Hostname/IP | Redis host для LiteLLM и совместимости с компонентами, которые читают host/port отдельно. |
| `REDIS_PORT` | `6379` | TCP port | Redis port. |
| `REDIS_URL` | `redis://redis:6379` | Redis URL | Используется LiteLLM Router для deployment affinity и guardrail для временных PII mappings. |
| `PROMETHEUS_MULTIPROC_DIR` | Не задано | Путь к writable directory | Нужен только если production-запуск LiteLLM использует несколько worker-процессов и Prometheus client multiprocess mode. |

## Сервис Presidio Analyzer

| Переменная | Значение по умолчанию | Допустимые значения | На что влияет |
| --- | --- | --- | --- |
| `PRESIDIO_ANALYZER_URL` | `http://presidio-analyzer:5001` | HTTP URL | Endpoint Analyzer, в который LiteLLM guardrail отправляет текст для детекции. |
| `PRESIDIO_ANALYZER_PORT` | `5001` | TCP port | Port standalone запуска `presidio/analyzer_server.py`; в Docker Compose порт фиксирован командой uvicorn. |
| `PRESIDIO_ANALYZER_WORKERS` | `1` | Положительное целое | Количество uvicorn workers. Каждый worker загружает отдельную копию spaCy/DeepPavlov, поэтому память растет примерно линейно. |
| `PRESIDIO_ANALYZER_CONCURRENCY_LIMIT` | `1` | Положительное целое | Максимум активных Analyzer requests внутри одного worker. |
| `PRESIDIO_ANALYZER_QUEUE_LIMIT` | `8` | Ноль или положительное целое | Размер очереди ожидания свободного Analyzer slot. |
| `PRESIDIO_ANALYZER_QUEUE_TIMEOUT_SECONDS` | `0.25` | Положительное число | Сколько ждать slot перед `503 analyzer_overloaded`. |

Эффективная параллельность Analyzer: `replicas * PRESIDIO_ANALYZER_WORKERS * PRESIDIO_ANALYZER_CONCURRENCY_LIMIT`. Перегрузка Analyzer всегда трактуется guardrail как fail-closed override.

## Калибровка recognizers

| Переменная | Значение по умолчанию | Допустимые значения | На что влияет |
| --- | --- | --- | --- |
| `PRESIDIO_ANALYZER_DETECT_BARE_INN_BY_CHECKSUM` | `true` | `true`, `false` | При `true` checksum-valid bare 12-digit INN проходит дефолтный `score_threshold=0.35`; 10-digit INN все равно требует контекст. При `false` любой голый ИНН требует контекст. |
| `PRESIDIO_ANALYZER_INTERNAL_DOMAIN_SUFFIXES` | `internal,local,lan,corp,corp.local,cluster.local,svc.cluster.local` | Comma/space-separated suffix list | Какие доменные suffixes recognizer `INTERNAL_DOMAIN` считает внутренними. |
| `PRESIDIO_ANALYZER_DETECT_PUBLIC_IPS` | `false` | `true`, `false` | При `true` `INTERNAL_IP` детектирует global public IP наряду с private/internal ranges. |

## Политики guardrail

| Переменная | Значение по умолчанию | Допустимые значения | На что влияет |
| --- | --- | --- | --- |
| `PII_GUARDRAIL_MODE` | `mask` | `mask`, `block` | Штатное поведение при найденной PII: reversible masking или блокировка запроса до provider call. |
| `PII_GUARDRAIL_FAILURE_MODE` | `fail_closed` | `fail_closed`, `fail_open` | Поведение при инфраструктурных сбоях Analyzer/Redis. `fail_closed` останавливает запрос, `fail_open` пропускает его дальше. Analyzer overload всегда fail-closed. |
| `PII_MAPPING_TTL_SECONDS` | `3600` | Положительное целое | TTL Redis mapping `pii_mapping:<pii_request_id>` для восстановления плейсхолдеров в ответе. |
| `PRE_EGRESS_POLICY_MODE` | `block` | `block`, `off` | Whole-payload policy до Analyzer: блокирует `.env` dumps, kubeconfig/manifests, nginx config, access/auth logs и stack traces. |
| `FINAL_PAYLOAD_LEAK_CHECK_MODE` | `block` | `block`, `off` | Финальная scan-only проверка provider-bound payload после mutation и до provider call. |
| `FINAL_PAYLOAD_LEAK_CHECK_CANARIES` | Пусто | Tokens через запятую или newline | Deterministic canaries для smoke/regression проверки, что raw marker не доходит до provider. Не используйте реальные секреты как canaries. |

## Dictionary substitutions

| Переменная | Значение по умолчанию | Допустимые значения | На что влияет |
| --- | --- | --- | --- |
| `DICTIONARY_SUBSTITUTIONS_ENABLED` | `true` | `true`, `false` | Включает reversible exact-match substitutions до Presidio Analyzer. |
| `DICTIONARY_SUBSTITUTIONS_FILE` | `/app/litellm_guardrails/dictionary-substitutions.default.json` | Путь к JSON-файлу | Файл правил substitution. Default seed содержит 10 крупных российских банков. |
| `DICTIONARY_SUBSTITUTIONS_JSON` | Пусто | JSON object | Inline override правил; если задан, используется вместо файла. |
| `DICTIONARY_SUBSTITUTIONS_FAILURE_MODE` | `fail_closed` | `fail_closed`, `fail_open` | Поведение при invalid config, ambiguous request или Redis mapping failure для dictionary layer. |

## Allowlist синтетических PII

| Переменная | Значение по умолчанию | Допустимые значения | На что влияет |
| --- | --- | --- | --- |
| `SYNTHETIC_PII_ALLOWLIST_MODE` | `off` | `off`, `allow` | При `allow` guardrail вычитает явно разрешенные synthetic/test spans из результатов Analyzer перед `PII_GUARDRAIL_MODE`. Не предназначено для real production PII. |
| `SYNTHETIC_PII_ALLOWLIST_JSON` | `[]` | JSON array | Правила с `rule_id`, `entity_types`, exact `values` и anchored safe regex `patterns`. Raw allowed values не пишутся в logs/metrics. |

## Regulated-topic policy

| Переменная | Значение по умолчанию | Допустимые значения | На что влияет |
| --- | --- | --- | --- |
| `REGULATED_TOPIC_POLICY_MODE` | `off` | `off`, `block` | Block-only policy pack для high-confidence AML/CFT / ПОД/ФТ, sanctions-screening, transaction-monitoring, suspicious-activity и compliance-bypass тем. |
| `REGULATED_TOPIC_POLICY_EXTRA_RULES_JSON` | Пусто | JSON array | Operator-defined block-only regex rules с bounded `category`, `rule_id`, `action`, `pattern`, `flags`. |

## Клиенты зависимостей guardrail

| Переменная | Значение по умолчанию | Допустимые значения | На что влияет |
| --- | --- | --- | --- |
| `PII_GUARDRAIL_REDIS_MAX_CONNECTIONS` | `20` | Положительное целое | Размер Redis connection pool guardrail на один процесс/event loop LiteLLM. |
| `PII_GUARDRAIL_REDIS_SOCKET_CONNECT_TIMEOUT_SECONDS` | `1.0` | Положительное число | Таймаут установки Redis connection. |
| `PII_GUARDRAIL_REDIS_SOCKET_TIMEOUT_SECONDS` | `2.0` | Положительное число | Таймаут Redis операций `setex`, `get`, `delete`. |
| `PII_GUARDRAIL_ANALYZER_TIMEOUT_SECONDS` | `30.0` | Положительное число | Общий timeout HTTP-вызова Analyzer из guardrail. |
| `PII_GUARDRAIL_ANALYZER_CONNECT_TIMEOUT_SECONDS` | `5.0` | Положительное число | Timeout установки HTTP connection к Analyzer. |
| `PII_GUARDRAIL_ANALYZER_MAX_CONNECTIONS` | `20` | Положительное целое | Максимум HTTP connections к Analyzer на один process/event loop. |
| `PII_GUARDRAIL_ANALYZER_MAX_KEEPALIVE_CONNECTIONS` | `10` | Положительное целое | Максимум keep-alive HTTP connections к Analyzer. |

## Модель и runtime DeepPavlov

| Переменная | Значение по умолчанию | Допустимые значения | На что влияет |
| --- | --- | --- | --- |
| `DEEPPAVLOV_NER_MODEL_URL` | `http://files.deeppavlov.ai/v1/ner/ner_rus_bert_torch_new.tar.gz` | HTTP/HTTPS URL | Archive с DeepPavlov `ner_rus_bert`, скачивается при сборке `presidio-analyzer`. |
| `DEEPPAVLOV_NER_MODEL_SHA256` | Пусто | SHA-256 hex digest | Если задан, `download_model.py` проверяет checksum скачанного архива. |
| `DEEPPAVLOV_NER_DOWNLOAD_TIMEOUT_SECONDS` | `120` | Положительное число | Timeout скачивания NER archive. |
| `DEEPPAVLOV_NER_MODEL_DIR` | Внутренний путь DeepPavlov cache | Путь в контейнере | Директория распаковки/проверки модели в `download_model.py`; обычно не меняется. |
| `DEEPPAVLOV_NER_REQUIRED` | `true` | `true`, `false` | Если `true`, `presidio-analyzer` отказывается стартовать без DeepPavlov NER. Если явно задано `false`, сервис может стартовать в degraded regex-only режиме, а `/api/v1/health` возвращает `status=degraded`, `ner=not_loaded`. |

## Клиентские токены и локальные гайды

| Переменная | Значение по умолчанию | Допустимые значения | На что влияет |
| --- | --- | --- | --- |
| `RU_LLM_PROXY_TOKEN` | Нет | LiteLLM virtual key | Клиентский bearer token для `/v1/*`. Создается через Admin UI или `scripts/create_virtual_key.sh`. |
| `RU_LLM_PROXY_URL` | Нет | HTTP/HTTPS URL | Base URL proxy в JWT/OIDC примерах, когда клиент вызывает deployed endpoint. |
| `OIDC_JWT` | Нет | JWT access token | Пользовательский OIDC/JWT bearer token для Enterprise JWT auth примера. |
| `ANTHROPIC_BASE_URL` | Нет | HTTP/HTTPS URL | Base URL Claude Code / Anthropic-compatible клиента при работе через proxy. |
| `ANTHROPIC_AUTH_TOKEN` | Нет | Claude Code auth token | В server-funded Claude Code режиме может быть proxy virtual key; в subscription passthrough режиме не должен подменять локальную Claude auth. |
| `ANTHROPIC_MODEL` | Нет | LiteLLM model alias | Модель, которую Claude Code должен вызывать через proxy, например `anthropic-example-standard`. |
| `ANTHROPIC_CUSTOM_HEADERS` | Нет | Header string | Для Claude subscription passthrough может передавать `x-litellm-api-key: Bearer <proxy-token>`, пока Claude auth управляется клиентом. |

## Live smoke-проверки и диагностика

| Переменная | Значение по умолчанию | Допустимые значения | На что влияет |
| --- | --- | --- | --- |
| `LITELLM_URL` | `http://localhost:4000` | HTTP URL | Base URL для helper-скриптов и smoke-тестов. `guardrails-smoke` требует локальный URL, потому что проверяет Redis cleanup через local Docker Compose. |
| `API_URL` | `http://localhost:4000` | HTTP URL | Локальная переменная только для curl-примеров в `docs/examples.md`. |
| `ENV_FILE` | `.env` | Путь к env-файлу | Какой env-файл загружать в smoke/helper scripts. |
| `CHAT_MODEL` | `glm-5.2` | LiteLLM model alias | Model alias для live chat/client/guardrails smoke. |
| `DENIED_MODEL` | `glm-5.2` | LiteLLM model alias | Model alias, доступ к которому должен быть запрещен restricted virtual key в `client-auth-smoke`. |
| `RESPONSES_MODEL` | Пусто | Live-validated LiteLLM alias | Включает strict `/v1/responses` smoke. Если не задан, проверка пропускается, кроме `REQUIRE_ALL_PROTOCOLS=1`. |
| `MESSAGES_MODEL` | Пусто | Live-validated LiteLLM alias | Включает strict `/v1/messages` smoke. Если не задан, проверка пропускается, кроме `REQUIRE_ALL_PROTOCOLS=1`. |
| `REQUIRE_ALL_PROTOCOLS` | `0` | `0`, `1` | При `1` client auth smoke падает, если optional `/v1/responses` или `/v1/messages` не настроены. |
| `ROUTING_SMOKE_MODEL` | `glm-5.2` | LiteLLM model alias | Model alias для sticky routing smoke. |
| `LITELLM_ROUTING_TEST_KEY` | Пусто | LiteLLM virtual key | Optional token для `make routing-smoke`; если пусто, используется `LITELLM_MASTER_KEY`. |
| `ANALYZER_URL` | `http://localhost:5001` | HTTP URL | Analyzer endpoint для `test_e2e.sh`. |
| `CURL_CONNECT_TIMEOUT` | `10` или `2` в mock smokes | Seconds | Таймаут подключения в live/mock smoke scripts. |
| `CURL_MAX_TIME` | `180` или `20` в mock smokes | Seconds | Максимальное время одного curl-запроса в smoke scripts. |
| `SMOKE_RUN_ID` | Timestamp/PID | String | Уникальный id запуска guardrails smoke. |
| `SMOKE_PII_MARKER` | `guardrails-smoke-<id>@example.test` | Synthetic email marker | Уникальный synthetic PII marker для проверки Redis cleanup. |
| `PRE_EGRESS_PROXY_PORT` | `14000` | TCP port | Host port test-only LiteLLM proxy для pre-egress mock smoke. |
| `PRE_EGRESS_PROXY_PROJECT` | `ru-llm-proxy-pre-egress-<pid>` | Docker Compose project name | Изоляция test-only compose project для pre-egress smoke. |
| `FINAL_LEAK_PROXY_PORT` | `14001` | TCP port | Host port test-only LiteLLM proxy для final leak-check smoke. |
| `FINAL_LEAK_PROXY_PROJECT` | `ru-llm-proxy-final-leak-<pid>` | Docker Compose project name | Изоляция test-only compose project для final leak-check smoke. |

## Helper для virtual keys

Эти переменные читает `scripts/create_virtual_key.sh` и Makefile target `make virtual-key-create`. Для регулярного администрирования пользователей предпочтительнее LiteLLM Admin UI; helper нужен для CI/e2e/bootstrap/runbook.

| Переменная | Значение по умолчанию | Допустимые значения | На что влияет |
| --- | --- | --- | --- |
| `KEY_ALIAS` | `client-local` | String | `key_alias` создаваемого virtual key. |
| `MODELS` | `standard` | Comma-separated model aliases/access groups | Список доступных моделей или access groups для virtual key. |
| `DURATION` | Пусто | LiteLLM duration, например `30d`, `12h` | Срок действия ключа. |
| `BUDGET_DURATION` | Пусто | Duration | Период сброса budget. |
| `MAX_BUDGET` | Пусто | Number | Max budget virtual key. |
| `RPM_LIMIT` | Пусто | Integer | Requests-per-minute limit. |
| `TPM_LIMIT` | Пусто | Integer | Tokens-per-minute limit. |
| `USER_ID` | Пусто | String | LiteLLM user id, к которому привязать key. |
| `TEAM_ID` | Пусто | String | LiteLLM team id, к которому привязать key. |
| `METADATA_JSON` | `{}` | JSON object | Metadata object для virtual key. Не кладите туда секреты или PII. |

## Внутренние переменные разработки и контейнеров

| Переменная | Значение по умолчанию | Допустимые значения | На что влияет |
| --- | --- | --- | --- |
| `PYTHONPATH` | Задается в compose/test targets | Path list | Позволяет LiteLLM и test containers импортировать `litellm_guardrails` и `presidio`. |
| `PYTHONDONTWRITEBYTECODE` | `1` в test containers | `0`, `1` | Отключает запись `.pyc` в read-only mounted workspace. |
| `PORT` | `8080` в mock upstream | TCP port | Port test-only `mock_openai_upstream.py`. |
| `PYTHON_LOCAL` | Auto-detected в Makefile | Python executable | Локальный Python для static tests. |
| `PYTEST` | `python -m pytest -p no:cacheprovider -v` | Command fragment | Pytest command для Docker test targets. |
| `PYTEST_DOCKER_FLAGS` | Compose run flags | Docker Compose run flags | Общие параметры запуска test containers. |

## Где что настраивать

| Что меняется | Где менять |
| --- | --- |
| Секреты локального запуска, GLM keys, admin password, DB password | `.env`, создается через `make setup` |
| Пользователи, virtual keys, лимиты, доступы к model groups | LiteLLM Admin UI или `scripts/create_virtual_key.sh` для автоматизации |
| Список model aliases/deployments и optional providers | `litellm-config.yaml` или отдельный GitOps-managed config |
| Guardrail policy defaults для контейнера | `docker-compose.yml` environment defaults или production orchestrator manifest |
| Kubernetes/network/security exposure | Отдельные deployment manifests и network policies |
| Разовые smoke параметры | Переменные окружения конкретной команды |
