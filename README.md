# ru-llm-proxy 🛡️

`ru-llm-proxy` — прокси для безопасной работы с внешними языковыми моделями через LiteLLM. Он принимает запросы в формате OpenAI API и базовые запросы Anthropic Messages API, проверяет русскоязычные персональные данные и служебные секреты, а затем либо маскирует их перед провайдером, либо блокирует запрос до выхода во внешнюю сеть.

Проект рассчитан на модель, где доступ к провайдерам оплачивается и настраивается на стороне прокси: ключи провайдеров остаются в серверном окружении, а пользователи получают клиентские ключи LiteLLM с нужными моделями, бюджетами и лимитами.

## Статус проекта

✅ В текущем `main` уже есть:

- LiteLLM-шлюз с публичным именем модели `glm-5.2` по умолчанию и дополнительным именем `glm-5.1`.
- Две подписки Z.AI Coding Plan в одном пуле (`ZAI_API_KEY`, `ZAI_API_KEY_2`) и закрепление клиентского ключа за выбранным развёртыванием модели.
- Русскоязычный защитный обработчик LiteLLM: распознаватели на регулярных выражениях, DeepPavlov NER, обратимые сопоставления в Redis, Chat Completions, текстовые поля Responses API и базовые текстовые блоки `content` в Anthropic Messages.
- Режимы `PII_GUARDRAIL_MODE=mask|block`: обратимое маскирование или безопасная блокировка запроса до вызова провайдера.
- Политика предварительной проверки перед выходом к провайдеру для `.env`, kubeconfig, манифестов Kubernetes, конфигураций nginx, журналов доступа/аутентификации и трассировок ошибок.
- Финальная проверка уже изменённой полезной нагрузки перед отправкой провайдеру.
- Словарные подстановки для обратимой замены бизнес-терминов; базовый набор содержит 10 крупных российских банков.
- Список разрешённых синтетических тестовых персональных данных для контролируемых проверочных значений.
- Политика регулируемых тем для внутренних AML/CFT / ПОД/ФТ, санкционных проверок, мониторинга транзакций, сценариев подозрительной активности и обхода комплаенс-процедур с высокой уверенностью.
- Ограничение очереди и параллелизма Presidio Analyzer, безопасный отказ при перегрузке, переиспользуемые Redis/httpx-клиенты защитного обработчика и откалиброванные пороги русскоязычных распознавателей.
- Метрики Prometheus, пригодные для аудита журналы, быстрые проверки работоспособности и базовые проверки CI.

⚠️ Ограничения:

- Восстановление работает только для плейсхолдеров и словарных замен, которые модель вернула точно.
- Восстановление потоковых ответов поддерживает `delta.content` и `delta.reasoning_content`; потоковые фрагменты аргументов вызовов инструментов/функций пока не переписываются.
- Claude Code как полноценный клиентский шлюз пока не считается полностью проверенным: покрыта базовая проверка аутентификации Anthropic Messages, но не весь контракт Claude Code.
- Промышленное развёртывание в Kubernetes проектируется отдельно в issue #59.

## Что защищает прокси

Прокси закрывает два класса рисков:

- **Персональные и чувствительные данные:** телефоны, email, ИНН, КПП, ОГРН/ОГРНИП, СНИЛС, паспорт РФ, банковские карты, БИК, расчетные и корреспондентские счета, адреса, ФИО, организации и локации.
- **Служебные секреты и инфраструктурные маркеры:** приватные и внутренние IP-адреса, внутренние домены, имена хостов, URL баз данных и сервисов с учётными данными, JWT, bearer-токены, приватные ключи, API-ключи, пары логин/пароль, фрагменты конфигураций и журналов.

Детальный список сущностей, пороги и ограничения распознавания описаны в [архитектуре](docs/architecture.md) и [примерах API](docs/examples.md).

Для `RU_INN` действует явная политика: при `PRESIDIO_ANALYZER_DETECT_BARE_INN_BY_CHECKSUM=true` 12-значный ИНН с корректной контрольной суммой проходит порог `score_threshold=0.35` даже без контекстного слова; 10-значный ИНН требует контекст. Если настройка выключена, любой ИНН без контекста не проходит порог.

## Быстрый старт

```bash
git clone https://github.com/vovkins/ru-llm-proxy.git
cd ru-llm-proxy

make setup
```

После `make setup` заполните в `.env` два ключа провайдера:

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

Первая сборка скачивает модель spaCy `ru_core_news_sm` и архив DeepPavlov `ner_rus_bert_torch_new.tar.gz`, поэтому может занять заметное время.

## Первый запрос

Создайте клиентский ключ LiteLLM через административный интерфейс или вспомогательный скрипт:

```bash
make virtual-key-create KEY_ALIAS=local-client MODELS=standard,zai DURATION=30d
```

Вызовите прокси как маршрут, совместимый с OpenAI API:

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

Не используйте `LITELLM_MASTER_KEY` как пользовательский ключ. Это административный секрет для LiteLLM API и административного интерфейса. Пользователям выдаются только клиентские ключи LiteLLM.

Больше примеров: [docs/examples.md](docs/examples.md).

## Архитектура

```text
Клиент
  │
  ▼
LiteLLM Proxy :4000
  │
  ├─ ru-pii-mask-pre: проверки политик, словарные подстановки, маскирование PII
  │      │
  │      ├─ Presidio Analyzer :5001
  │      │    ├─ spaCy ru_core_news_sm
  │      │    ├─ русскоязычные распознаватели на регулярных выражениях
  │      │    └─ DeepPavlov ner_rus_bert
  │      │
  │      └─ Redis: сопоставления для восстановления в рамках запроса
  │
  ├─ маршрутизация LiteLLM: развёртывания провайдера для glm-5.2 / glm-5.1
  │
  └─ ru-pii-mask-post: восстановление ответа и очистка сопоставлений
```

PostgreSQL хранит состояние LiteLLM, Redis используется для временных сопоставлений PII и закрепления маршрута за клиентским ключом. Подробная схема, порядок обработчиков и границы данных: [docs/architecture.md](docs/architecture.md).

## Режимы работы

| Режим | Что делает | Где читать |
| --- | --- | --- |
| `PII_GUARDRAIL_MODE=mask` | Маскирует найденные значения, сохраняет сопоставление, восстанавливает ответ. | [Архитектура](docs/architecture.md), [примеры](docs/examples.md) |
| `PII_GUARDRAIL_MODE=block` | Возвращает безопасную ошибку `422`, если в запросе есть персональные данные. | [Примеры](docs/examples.md) |
| `PRE_EGRESS_POLICY_MODE=block` | Блокирует исходную полезную нагрузку с конфигурациями, журналами и секретами до Analyzer; безопасный код ошибки — `pre_egress_policy_blocked`. | [Сетевые ограничения](docs/egress-controls.md), [архитектура](docs/architecture.md) |
| `FINAL_PAYLOAD_LEAK_CHECK_MODE=block` | Проверяет полезную нагрузку после изменений и до вызова провайдера. | [Примеры](docs/examples.md), [мониторинг](docs/monitoring.md) |
| `REGULATED_TOPIC_POLICY_MODE=block` | Блокирует внутренние регулируемые темы с высокой уверенностью. | [Архитектура](docs/architecture.md), [проверочные контуры](docs/compliance.md) |

Поведение по умолчанию для инфраструктурных сбоев защитного обработчика — безопасный отказ (`PII_GUARDRAIL_FAILURE_MODE=fail_closed`). Если Analyzer или Redis недоступны, запрос останавливается. Перегрузка Analyzer (`analyzer_overloaded`) всегда обрабатывается как безопасный отказ независимо от этой настройки.

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
| Подготовить подтверждения для проверки | [docs/compliance.md](docs/compliance.md) |
| Настроить сетевые ограничения | [docs/egress-controls.md](docs/egress-controls.md), [deploy/kubernetes/egress](deploy/kubernetes/egress) |
| Разобраться с закреплением маршрутов | [docs/routing.md](docs/routing.md) |
| Посмотреть историческое исследование | [docs/research.md](docs/research.md) |

OpenAI и Anthropic не входят в активную конфигурацию по умолчанию. Примеры дополнительных провайдеров лежат в [examples/litellm-config.optional-providers.yaml](examples/litellm-config.optional-providers.yaml); перед промышленным включением идентификаторы моделей нужно проверить на текущем образе LiteLLM и конкретной подписке.

Клиентские гайды включают [ZCode](docs/clients/zcode.md), [Codex](docs/clients/codex.md), [Claude Code](docs/clients/claude-code.md), [OpenCode](docs/clients/opencode.md), [Kilo Code](docs/clients/kilo-code.md) и [JWT/OIDC](docs/clients/jwt.md).

## Команды

| Команда | Назначение |
| --- | --- |
| `make setup` | Создать `.env` и сгенерировать локальные секреты. |
| `make build` | Собрать Docker-образы. |
| `make up` | Запустить LiteLLM, Analyzer, PostgreSQL и Redis. |
| `make health` | Проверить состояние сервисов. |
| `make test` | Быстрый локальный набор: `test-unit` и `test-static`. |
| `make test-static` | Лёгкие статические и asyncio-регрессионные тесты на хосте через `PYTHON_LOCAL`. |
| `make test-recognizer-api` | Docker-проверка Analyzer API и порогов распознавателей. |
| `make test-e2e` | Быстрая проверка с реальным ключом провайдера. |
| `make monitor-smoke` | Проверить состояние сервисов, список защитных слоёв и `/metrics`. |
| `make guardrails-smoke` | Проверить обычные и потоковые защитные слои в локальном Docker Compose. |
| `make routing-smoke` | Проверить закрепление развёртывания за одним ключом. |
| `make client-auth-smoke` | Проверить аутентификацию клиентов и базовые проверки протоколов `/v1`. |

Полный список команд: `make help`.

## Мониторинг

LiteLLM `/metrics` открыт без ключа LiteLLM API; доступ к нему должен ограничиваться сетевыми настройками. `make monitor-smoke` проверяет:

- состояние LiteLLM, Analyzer, PostgreSQL и Redis;
- `ner=loaded` у Presidio Analyzer;
- наличие метрик LiteLLM, защитного слоя и Analyzer.

Подробные метрики, алерты и эксплуатационный регламент: [docs/monitoring.md](docs/monitoring.md).

## Проверки

Перед изменениями в коде или документации обычно достаточно:

```bash
make test-static
git diff --check
```

Для полного локального прогона с Docker и ключом провайдера:

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
