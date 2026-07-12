# ru-llm-proxy 🛡️

`ru-llm-proxy` — LLM-прокси для безопасной работы с внешними моделями через LiteLLM. Он принимает OpenAI-compatible и базовые Anthropic-compatible запросы, проверяет русскоязычные персональные данные и служебные секреты, а затем либо маскирует их перед провайдером, либо блокирует запрос до выхода во внешнюю сеть.

Проект рассчитан на server-funded модель доступа: upstream-ключи провайдеров остаются на стороне прокси, а пользователи получают LiteLLM virtual keys с нужными моделями, бюджетами и лимитами.

## Статус проекта

✅ В текущем `main` уже есть:

- LiteLLM gateway с дефолтным публичным алиасом `glm-5.2` и дополнительным алиасом `glm-5.1`.
- Две подписки Z.AI Coding Plan в одном пуле (`ZAI_API_KEY`, `ZAI_API_KEY_2`) и sticky routing между deployments.
- Русскоязычный PII guardrail: regex recognizers, DeepPavlov NER, reversible Redis mapping, Chat Completions, Responses API text payloads и базовых Anthropic Messages `content` string/text blocks.
- Режимы `PII_GUARDRAIL_MODE=mask|block`: обратимое маскирование или безопасная блокировка запроса до provider call.
- Pre-egress policy для `.env`, kubeconfig, Kubernetes manifests, nginx configs, access/auth logs и stack traces.
- Final payload leak check перед отправкой уже измененного provider-bound payload.
- Dictionary substitutions для обратимой замены бизнес-терминов; seed содержит 10 крупных российских банков.
- Synthetic/test PII allowlist для контролируемых тестовых значений.
- Regulated-topic policy для high-confidence AML/CFT / ПОД/ФТ, sanctions-screening, transaction-monitoring, suspicious-activity и compliance-bypass тем.
- Bounded Presidio Analyzer capacity, fail-closed overload handling, Reused Redis/httpx guardrail clients и Calibrated Russian recognizer thresholds.
- Метрики Prometheus, audit-friendly логи, smoke-проверки и baseline CI.

⚠️ Ограничения:

- Восстановление работает только для плейсхолдеров и dictionary replacements, которые модель вернула точно.
- Streaming restoration поддерживает `delta.content` и `delta.reasoning_content`; streaming tool/function-call argument deltas пока не переписываются.
- Claude Code как полноценный gateway target пока не считается полностью валидированным: покрыт basic Anthropic Messages auth smoke, но не весь Claude Code contract.
- Kubernetes production deployment проектируется отдельно в issue #59.

## Что защищает прокси

Прокси закрывает два класса рисков:

- **Персональные и чувствительные данные:** телефоны, email, ИНН, КПП, ОГРН/ОГРНИП, СНИЛС, паспорт РФ, банковские карты, БИК, расчетные и корреспондентские счета, адреса, ФИО, организации и локации.
- **Служебные секреты и инфраструктурные маркеры:** private/internal IP, internal domains, hostnames, DB/service URLs с credentials, JWT, bearer tokens, private keys, API keys, login/password pairs, конфигурационные и логовые фрагменты.

Детальный список сущностей, пороги и ограничения распознавания описаны в [архитектуре](docs/architecture.md) и [примерах API](docs/examples.md).

Для `RU_INN` действует явная политика: при `PRESIDIO_ANALYZER_DETECT_BARE_INN_BY_CHECKSUM=true` checksum-valid bare ИНН без контекстного слова проходит дефолтный `score_threshold=0.35` только для 12 цифр; 10-значный голый ИНН требует контекст. Если настройка выключена, любой голый ИНН требует контекст.

## Быстрый старт

```bash
git clone https://github.com/vovkins/ru-llm-proxy.git
cd ru-llm-proxy

make setup
```

После `make setup` заполните в `.env` два upstream-ключа:

```env
ZAI_API_KEY=your-zai-key
ZAI_API_KEY_2=your-second-zai-key
```

Затем соберите и запустите стек:

```bash
make build
make up
make health
```

Первая сборка скачивает spaCy model `ru_core_news_sm` и DeepPavlov archive `ner_rus_bert_torch_new.tar.gz`, поэтому может занять заметное время.

## Первый запрос

Создайте клиентский virtual key через LiteLLM Admin UI или helper:

```bash
make virtual-key-create KEY_ALIAS=local-client MODELS=standard,zai DURATION=30d
```

Вызовите прокси как OpenAI-compatible endpoint:

```bash
curl http://localhost:4000/v1/chat/completions \
  -H "Authorization: Bearer $RU_LLM_PROXY_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "glm-5.2",
    "messages": [
      {"role": "user", "content": "Клиент Иванов Иван, телефон +79031234567. Составь краткое резюме."}
    ]
  }'
```

Не используйте `LITELLM_MASTER_KEY` как пользовательский ключ. Это административный секрет для LiteLLM API и Admin UI. Пользователям выдаются только virtual keys.

Больше примеров: [docs/examples.md](docs/examples.md).

## Архитектура

```text
Клиент
  │
  ▼
LiteLLM Proxy :4000
  │
  ├─ pre-call guardrail: policy checks, dictionary substitutions, PII masking
  │      │
  │      ├─ Presidio Analyzer :5001
  │      │    ├─ spaCy ru_core_news_sm
  │      │    ├─ Russian regex recognizers
  │      │    └─ DeepPavlov ner_rus_bert
  │      │
  │      └─ Redis: request-scoped restore mappings
  │
  ├─ LiteLLM routing: glm-5.2 / glm-5.1 provider deployments
  │
  └─ post-call guardrail: response restoration and mapping cleanup
```

PostgreSQL хранит состояние LiteLLM, Redis используется для временных PII mappings и sticky routing affinity. Подробная схема, порядок hooks и границы данных: [docs/architecture.md](docs/architecture.md).

## Режимы работы

| Режим | Что делает | Где читать |
| --- | --- | --- |
| `PII_GUARDRAIL_MODE=mask` | Маскирует найденные значения, сохраняет mapping, восстанавливает ответ. | [Архитектура](docs/architecture.md), [примеры](docs/examples.md) |
| `PII_GUARDRAIL_MODE=block` | Возвращает безопасную `422` ошибку, если в запросе есть PII. | [Примеры](docs/examples.md) |
| `PRE_EGRESS_POLICY_MODE=block` | Блокирует raw payload с конфигурациями, логами и секретами до Analyzer; безопасный код ошибки — `pre_egress_policy_blocked`. | [Egress controls](docs/egress-controls.md), [архитектура](docs/architecture.md) |
| `FINAL_PAYLOAD_LEAK_CHECK_MODE=block` | Проверяет provider-bound payload после mutation и до provider call. | [Примеры](docs/examples.md), [мониторинг](docs/monitoring.md) |
| `REGULATED_TOPIC_POLICY_MODE=block` | Блокирует high-confidence внутренние регулируемые темы. | [Архитектура](docs/architecture.md), [compliance gates](docs/compliance.md) |

Production default для инфраструктурных сбоев guardrail — `PII_GUARDRAIL_FAILURE_MODE=fail_closed`. Если Analyzer или Redis недоступны, запрос останавливается. Перегрузка Analyzer (`analyzer_overloaded`) всегда fail-closed независимо от этой настройки.

## Документация

Начните с [карты документации](docs/README.md). Основные документы:

| Задача | Документ |
| --- | --- |
| Понять архитектуру и поток запроса | [docs/architecture.md](docs/architecture.md) |
| Настроить переменные окружения | [docs/configuration.md](docs/configuration.md) |
| Посмотреть curl/API-примеры | [docs/examples.md](docs/examples.md) |
| Подключить клиента | [docs/clients](docs/clients) |
| Создать ключи и разграничить доступ | [docs/admin-access.md](docs/admin-access.md) |
| Настроить мониторинг и алерты | [docs/monitoring.md](docs/monitoring.md) |
| Подготовить evidence для проверки | [docs/compliance.md](docs/compliance.md) |
| Настроить сетевые ограничения | [docs/egress-controls.md](docs/egress-controls.md), [deploy/kubernetes/egress](deploy/kubernetes/egress) |
| Разобраться со sticky routing | [docs/routing.md](docs/routing.md) |
| Посмотреть историческое исследование | [docs/research.md](docs/research.md) |

OpenAI и Anthropic не входят в активный дефолтный конфиг. Примеры optional providers лежат в [examples/litellm-config.optional-providers.yaml](examples/litellm-config.optional-providers.yaml); перед production-включением model IDs нужно проверить на текущем LiteLLM image и конкретной подписке.

Клиентские гайды включают [ZCode](docs/clients/zcode.md), [Codex](docs/clients/codex.md), [Claude Code](docs/clients/claude-code.md), [OpenCode](docs/clients/opencode.md), [Kilo Code](docs/clients/kilo-code.md) и [JWT/OIDC](docs/clients/jwt.md).

## Команды

| Команда | Назначение |
| --- | --- |
| `make setup` | Создать `.env` и сгенерировать локальные секреты. |
| `make build` | Собрать Docker-образы. |
| `make up` | Запустить LiteLLM, Analyzer, PostgreSQL и Redis. |
| `make health` | Проверить состояние сервисов. |
| `make test` | Быстрый локальный набор: `test-unit` и `test-static`. |
| `make test-static` | Легкие static/asyncio regression tests на host через `PYTHON_LOCAL`. |
| `make test-recognizer-api` | Docker-проверка Analyzer API и порогов recognizers. |
| `make test-e2e` | Live smoke-проверка с реальным provider key. |
| `make monitor-smoke` | Проверить health, guardrails list и `/metrics`. |
| `make guardrails-smoke` | Проверить non-streaming и streaming guardrails на локальном docker-compose. |
| `make routing-smoke` | Проверить sticky deployment affinity для одного ключа. |
| `make client-auth-smoke` | Проверить client auth и базовые `/v1` protocol smokes. |

Полный список команд: `make help`.

## Мониторинг

LiteLLM `/metrics` открыт без LiteLLM API key; доступ к нему должен ограничиваться сетевыми настройками. `make monitor-smoke` проверяет:

- health LiteLLM, Analyzer, PostgreSQL и Redis;
- `ner=loaded` у Presidio Analyzer;
- наличие LiteLLM, guardrail и Analyzer metrics.

Подробные метрики, алерты и runbook: [docs/monitoring.md](docs/monitoring.md).

## Проверки

Перед изменениями в коде или документации обычно достаточно:

```bash
make test-static
git diff --check
```

Для полного локального прогона с Docker и provider key:

```bash
make build
make up
make test
make test-recognizer-api
make test-egress-security
make test-observability-gates
make monitor-smoke
make client-auth-smoke
make guardrails-smoke
make routing-smoke
make test-e2e
```

## Лицензия

MIT
