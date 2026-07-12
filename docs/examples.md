# Примеры API

Все базовые примеры соответствуют текущей конфигурации репозитория: LiteLLM на `localhost:4000`, публичная модель по умолчанию `glm-5.2` и дополнительное имя модели `glm-5.1`. Оба имени GLM используют конечную точку Z.AI Coding Plan через серверные ключи прокси.

Имена моделей OpenAI/Anthropic больше не включены в активный `litellm-config.yaml` по умолчанию. Если нужны эти провайдеры, скопируйте и адаптируйте пример из `examples/litellm-config.optional-providers.yaml`, затем проверьте идентификаторы моделей на текущем образе LiteLLM и реальных ключах провайдеров.

## Окружение

Основные переменные для примеров ниже:

```bash
export API_URL="http://localhost:4000"
export RU_LLM_PROXY_TOKEN="sk-..."
# Нужен только для примеров со сквозной передачей ключа клиента провайдеру:
export ANTHROPIC_BYOK_API_KEY="sk-ant-..."
```

Полный сгруппированный справочник по переменным окружения, допустимым значениям и влиянию на запуск: [configuration.md](configuration.md).

Обычные пользовательские `RU_LLM_PROXY_TOKEN` создавайте через административный интерфейс LiteLLM. Вспомогательный скрипт командной строки нужен для сценариев DevOps, CI, первичной настройки и эксплуатационных регламентов:

```bash
make virtual-key-create KEY_ALIAS=local-examples MODELS=standard,zai DURATION=30d
```

`LITELLM_MASTER_KEY` используется только для административных операций, например создания пользовательских ключей и просмотра списка защитных слоёв.
Граница доступа для административного интерфейса/API в промышленной среде, операторские роли и ротация учётных данных
описаны в [admin-access.md](admin-access.md).

## Режимы авторизации

В обычном режиме провайдерские ключи хранятся на стороне прокси, а клиент использует выданный ему ключ прокси как обычный bearer-токен. В профиле по умолчанию прокси сам вызывает GLM через серверные `ZAI_API_KEY` и `ZAI_API_KEY_2`:

```bash
curl -s "$API_URL/v1/chat/completions" \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $RU_LLM_PROXY_TOKEN" \
  -d '{"model":"glm-5.2","messages":[{"role":"user","content":"Привет"}]}'
```

Режим сквозной передачи ключа клиента провайдеру разделяет авторизацию на прокси и авторизацию у провайдера. Ключ прокси передаётся в `x-litellm-api-key`, а ключ провайдера — через поддерживаемый заголовок провайдера вроде `x-api-key`, `api-key` или `x-goog-api-key`. Этот режим не включён в конфигурацию по умолчанию; включайте его только в отдельном развёртывании после проверки на текущем образе LiteLLM.

```bash
curl -s "$API_URL/v1/messages" \
  -H "Content-Type: application/json" \
  -H "x-litellm-api-key: Bearer $RU_LLM_PROXY_TOKEN" \
  -H "x-api-key: $ANTHROPIC_BYOK_API_KEY" \
  -d '{
    "model": "anthropic-example-standard",
    "max_tokens": 80,
    "messages": [{"role": "user", "content": "Привет"}]
  }'
```

OAuth-доступ подписок Codex/ChatGPT и Claude обычно использует провайдерский `Authorization`. Обычный маршрут LiteLLM может не пересылать этот заголовок провайдеру, поэтому сквозную передачу подписки нужно считать экспериментальной до проверки на текущем образе; при необходимости выносите её в сквозной маршрут, боковой контейнер или отдельный адаптер. Не кладите общий Codex `auth.json` или учётные данные Claude на прокси.

## Chat Completions без персональных данных

```bash
curl -s "$API_URL/v1/chat/completions" \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $RU_LLM_PROXY_TOKEN" \
  -d '{
    "model": "glm-5.2",
    "messages": [
      {
        "role": "user",
        "content": "Скажи короткое приветствие на русском"
      }
    ],
    "max_tokens": 80
  }' | jq '.choices[0].message'
```

## Chat Completions с персональными данными

```bash
curl -s "$API_URL/v1/chat/completions" \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $RU_LLM_PROXY_TOKEN" \
  -d '{
    "model": "glm-5.2",
    "messages": [
      {
        "role": "user",
        "content": "Клиент Иванов Иван, телефон +79031234567, ИНН 7707083893. Составь краткую справку."
      }
    ],
    "max_tokens": 120
  }' | jq '.choices[0].message'
```

Перед вызовом провайдера защитный слой отправляет маскированный текст примерно такого вида:

```text
Клиент <PERSON_1>, телефон <PHONE_NUMBER_1>, ИНН <RU_INN_1>. Составь краткую справку.
```

Если ответ провайдера содержит эти плейсхолдеры, обработчик после ответа восстановит исходные значения перед возвратом клиенту.

## Несколько значений одного типа

```bash
curl -s "$API_URL/v1/chat/completions" \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $RU_LLM_PROXY_TOKEN" \
  -d '{
    "model": "glm-5.2",
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

## Дополнительный пример OpenAI Responses API

Локальные задачи Codex CLI/App используют Responses API. Этот пример требует, чтобы администратор добавил имя модели, совместимое с OpenAI API, в `litellm-config.yaml`, например на основе `examples/litellm-config.optional-providers.yaml`:

```bash
curl -s "$API_URL/v1/responses" \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $RU_LLM_PROXY_TOKEN" \
  -d '{
    "model": "openai-example-standard",
    "input": "Скажи короткое приветствие на русском",
    "max_output_tokens": 80
  }' | jq
```

Защитный слой персональных данных обрабатывает верхнеуровневый `system` в Anthropic Messages, строковые и текстовые блоки `instructions` / `input` в Responses API, элементы `input[]` со строковым `content`, `arguments` у вызовов инструментов, строковые/списочные `output` у результатов инструментов и текстовые блоки с `text`, `input_text` или `output_text`. Нестроковые входы, например изображения и файлы, проходят без изменений.

Для быстрой проверки этого маршрута на живом сервисе задайте `RESPONSES_MODEL` явно:

```bash
RESPONSES_MODEL=<validated-responses-alias> make client-auth-smoke
```

## Дополнительный базовый пример Anthropic Messages API

Это базовый пример Anthropic Messages API без потоковой передачи через прокси. Он требует, чтобы администратор добавил имя модели, совместимое с Anthropic API, в `litellm-config.yaml`:

```bash
curl -s "$API_URL/v1/messages" \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $RU_LLM_PROXY_TOKEN" \
  -d '{
    "model": "anthropic-example-standard",
    "max_tokens": 80,
    "messages": [
      {
        "role": "user",
        "content": "Скажи короткое приветствие на русском"
      }
    ]
  }' | jq
```

Для быстрой проверки этого маршрута на живом сервисе задайте `MESSAGES_MODEL` явно:

```bash
MESSAGES_MODEL=<validated-messages-alias> make client-auth-smoke
```

Полный контракт шлюза для Claude Code строже этого примера: `POST /v1/messages?beta=true`, потоковые SSE-ответы, пересылка `anthropic-version` / `anthropic-beta`, дополнительный подсчёт токенов и обнаружение моделей. Его статус описан в [clients/claude-code.md](clients/claude-code.md).

## Прямая проверка Analyzer

```bash
curl -s http://localhost:5001/api/v1/analyze \
  -H "Content-Type: application/json" \
  -d '{
    "text": "Мой телефон +79031234567 и ИНН 7707083893",
    "language": "ru"
  }' | jq
```

Ожидаемые типы сущностей: `PHONE_NUMBER` и `RU_INN`.

По умолчанию Analyzer API использует `score_threshold=0.35`, а `RU_INN` проходит проверку контрольной суммы. При `PRESIDIO_ANALYZER_DETECT_BARE_INN_BY_CHECKSUM=true` 12-значный ИНН с корректной контрольной суммой детектируется даже без контекстного слова:

```bash
curl -s http://localhost:5001/api/v1/analyze \
  -H "Content-Type: application/json" \
  -d '{
    "text": "500100732259",
    "language": "ru",
    "score_threshold": 0.35
  }' | jq
```

10-значный ИНН требует контекст даже в режиме по умолчанию, потому что контрольная сумма пропускает заметную долю случайных 10-значных чисел. Если выставить `PRESIDIO_ANALYZER_DETECT_BARE_INN_BY_CHECKSUM=false`, строгий режим требует контекст вроде `ИНН` или `налогоплательщик` для любого ИНН без соседних поясняющих слов; такой ИНН без контекста не проходит `score_threshold=0.35`. Это снижает ложные срабатывания для случайных длинных числовых последовательностей, которые прошли проверку контрольной суммы.

`RU_ADDRESS` — ограниченный распознаватель на регулярных выражениях. Он покрывает базовые формы вроде `ул. Ленина, д. 10`, `ул Ленина 10`, `Тверская улица, дом 7`, требует границу слева у сокращений типа улицы и явное `дом`/`д.` для формы `Тверская улица, дом 7`. Ограничения: полный разбор индексов, регионов, владений и свободных адресов без явной структуры «улица/дом» не поддерживается.

Реквизиты контрагента покрываются отдельными entity types:

- `RU_KPP`;
- `RU_OGRN`;
- `RU_OGRNIP`;
- `RU_BIK`;
- `RU_SETTLEMENT_ACCOUNT`;
- `RU_CORRESPONDENT_ACCOUNT`.

`RU_KPP`, `RU_BIK`, `RU_SETTLEMENT_ACCOUNT` и `RU_CORRESPONDENT_ACCOUNT` требуют явный реквизитный контекст. `RU_OGRN` и `RU_OGRNIP` проходят проверку контрольной суммы. Для расчётных и корреспондентских счетов при наличии БИК рядом Analyzer дополнительно проверяет контрольный ключ; без БИК используется сильный контекст и структурные ограничения, без онлайн-проверки по справочнику банков.

```bash
curl -s http://localhost:5001/api/v1/analyze \
  -H "Content-Type: application/json" \
  -d '{
    "text": "КПП 770801001, ОГРН 1027700132195, БИК 044525225, расчетный счет 40702810900000000000, к/с 30101810400000000225",
    "language": "ru",
    "score_threshold": 0.35
  }' | jq
```

Распознаватели инфраструктуры и секретов работают в том же Analyzer API и возвращают
фрагменты на уровне отдельных сущностей для технических идентификаторов и секретов:

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

`INTERNAL_DOMAIN` использует список суффиксов из
`PRESIDIO_ANALYZER_INTERNAL_DOMAIN_SUFFIXES`. По умолчанию публичные IP не
детектируются как `INTERNAL_IP`; если политика развёртывания считает любые IP
чувствительными, включите `PRESIDIO_ANALYZER_DETECT_PUBLIC_IPS=true` и
пересоздайте контейнер `presidio-analyzer`.

## Фильтрация Analyzer по типам сущностей

Analyzer API поддерживает стандартный параметр Presidio `entities`. Распознаватели на регулярных выражениях и DeepPavlov NER соблюдают этот список одинаково: если запрошен только `RU_INN`, NER-типы `PERSON`, `LOCATION` и `ORGANIZATION` не вычисляются.

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

NER-результаты имеют фиксированную оценку `0.7`; при `score_threshold` выше `0.7` DeepPavlov NER не запускается.

## Проверки здоровья LiteLLM

Маршрут проверки живости LiteLLM не требует `LITELLM_MASTER_KEY` и используется для проверки состояния Docker-контейнера `ru-llm-proxy`:

```bash
curl -s http://localhost:4000/health/liveliness
```

`/health` у LiteLLM предназначен для проверки моделей и может делать реальные вызовы LLM API, поэтому для проверок живости и готовности лучше использовать специализированные маршруты.

## Проверка здоровья Analyzer

```bash
curl -s http://localhost:5001/api/v1/health | jq
```

Статус NER возвращается отдельно:

```json
{"status":"ok","ner":"loaded","ner_required":true}
```

По умолчанию `presidio-analyzer` не должен стартовать без DeepPavlov NER. Если оператор явно разрешил деградированный режим через `DEEPPAVLOV_NER_REQUIRED=false`, проверка состояния показывает пониженный статус:

```json
{"status":"degraded","ner":"not_loaded","ner_required":false}
```

## Защитные слои LiteLLM

Список защитных слоёв, зарегистрированных в LiteLLM, администратор смотрит через цель Makefile:

```bash
make guardrails-list
```

Запрос к живому сервису с явным параметром `guardrails`:

```bash
curl -s -D /tmp/ru-llm-proxy-headers "$API_URL/v1/chat/completions" \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $RU_LLM_PROXY_TOKEN" \
  -d '{
    "model": "glm-5.2",
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

## Режим блокировки PII

По умолчанию защитный слой работает в режиме обратимого маскирования:

```env
PII_GUARDRAIL_MODE=mask
```

Чтобы отклонять запросы с найденными персональными данными до вызова провайдера, задайте режим блокировки и перезапустите LiteLLM:

```env
PII_GUARDRAIL_MODE=block
```

```bash
make restart
```

В режиме блокировки запрос с персональными данными возвращает `422` и безопасное тело ошибки. Ответ содержит только типы сущностей:

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

Исходные персональные данные, смещения и исходный текст в теле ошибки не возвращаются. Запросы без персональных данных продолжают идти к провайдеру.

## Словарные подстановки

Словарные подстановки включены по умолчанию:

```env
DICTIONARY_SUBSTITUTIONS_ENABLED=true
DICTIONARY_SUBSTITUTIONS_FILE=/app/litellm_guardrails/dictionary-substitutions.default.json
DICTIONARY_SUBSTITUTIONS_JSON=
DICTIONARY_SUBSTITUTIONS_FAILURE_MODE=fail_closed
```

Файл по умолчанию содержит начальный набор из 10 крупных российских банков: `Сбербанк`, `ВТБ`, `Газпромбанк`, `Альфа-Банк`, `ПСБ`, `Россельхозбанк`, `Т-Банк`, `Московский кредитный банк`, `Банк Дом.РФ`, `Совкомбанк`. Это стартовая политика бизнес-словаря; в промышленной среде замените или дополните файл под свои термины.

Пример:

```text
Запрос клиента:      Проверь договор с Т-Банк.
Запрос провайдеру:   Проверь договор с Зетта Групп.
Ответ клиенту:       Договор с Т-Банк проверен.
```

Правило в JSON:

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

Политика словарных подстановок запускается до Analyzer и не зависит от DeepPavlov NER. Фрагменты замен исключаются из маскирования/блокировки персональных данных, поэтому `Зетта Групп` не будет заменён на `<ORGANIZATION_1>`. Если запрос уже содержит текст замены вместе с исходной фразой, например `Сравни Т-Банк и Зетта Групп`, прокси считает восстановление неоднозначным и при `fail_closed` по умолчанию останавливает запрос.

Ограничение: восстановление работает только по точному совпадению. Если модель вернула `Зетте Групп`, `Zetta Group` или любое перефразирование вместо точного `Зетта Групп`, прокси не сможет восстановить `Т-Банк`.

## Список разрешённых синтетических персональных данных

По умолчанию список разрешённых значений выключен:

```env
SYNTHETIC_PII_ALLOWLIST_MODE=off
SYNTHETIC_PII_ALLOWLIST_JSON=[]
```

Включайте его только для контролируемых тестовых наборов, которые нужны в быстрых проверках, демонстрациях или проверочных документах. Пример ниже разрешает один точный синтетический телефон и синтетические адреса электронной почты в пространстве имён `example.test`:

```env
SYNTHETIC_PII_ALLOWLIST_MODE=allow
SYNTHETIC_PII_ALLOWLIST_JSON=[{"rule_id":"docs_synthetic_contacts","entity_types":["PHONE_NUMBER","EMAIL_ADDRESS"],"values":["+79031234567"],"patterns":["^[A-Za-z0-9._%+-]+@example\\.test$"]}]
```

В режиме `mask` разрешённые фрагменты остаются как есть, а остальные персональные данные в том же запросе маскируются:

```text
Вход:   Тестовый телефон +79031234567, реальный +79035551234
Выход:  Тестовый телефон +79031234567, реальный <PHONE_NUMBER_1>
```

В режиме `block` запрос только с разрешёнными синтетическими значениями проходит дальше, но смешанный запрос с разрешёнными синтетическими и реальными персональными данными всё равно блокируется из-за оставшихся реальных фрагментов. Срабатывания списка разрешённых значений видны в `synthetic_pii_allowlist_applied`, `gateway_guardrail_audit` и `ru_synthetic_pii_allowlist_hits_total`; исходные разрешённые значения в журналы и метрики не пишутся.

Слишком широкие регулярные выражения вроде `^.*$` игнорируются. Шаблон должен быть привязан к началу и концу строки и ссылаться на контролируемое синтетическое пространство имён (`example.test`, `TEST_`, `RU_PROXY_`, `SYNTHETIC_`, `CANARY_`). Список разрешённых значений не применяется к политикам предварительной проверки, регулируемых тем и финальной проверки утечек.

## Политика регулируемых тем

`REGULATED_TOPIC_POLICY_MODE=off` по умолчанию. Это отдельный набор правил для AML/CFT / ПОД/ФТ, санкционных проверок, мониторинга транзакций, сценариев подозрительной активности и обхода комплаенс-процедур; он не является распознавателем персональных данных, не маскирует найденные фрагменты и не создаёт сопоставление в Redis.

Чтобы включить консервативный режим только на блокировку:

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

Ответ, структурированные журналы, `gateway_guardrail_audit` и метрика `ru_regulated_topic_policy_blocked_total` содержат только ограниченные категории, идентификаторы правил, действия и счётчики, без исходного запроса, исходного найденного текста, фрагментов или смещений. Публичные правила по умолчанию не содержат конфиденциальных терминов конкретной организации. Если нужны внутренние правила, добавьте операторские правила блокировки на регулярных выражениях через `REGULATED_TOPIC_POLICY_EXTRA_RULES_JSON`.

```env
REGULATED_TOPIC_POLICY_EXTRA_RULES_JSON=[{"category":"internal_watchlist","rule_id":"custom_watchlist_codename","action":"block","pattern":"PROJECT_MARS_WATCHLIST","flags":"i"}]
```

Политика регулируемых тем остаётся режимом только на блокировку. Обратимые словарные подстановки работают отдельным слоем точных совпадений через `DICTIONARY_SUBSTITUTIONS_ENABLED` и не используются для широкой семантической блокировки тем.

## Предварительная проверка конфигураций и журналов

`PRE_EGRESS_POLICY_MODE=block` включён по умолчанию и работает раньше Presidio
Analyzer, то есть до `POST /api/v1/analyze`. Он останавливает целые операционные
полезные нагрузки: выгрузки `.env` с секретами, kubeconfig/манифесты Kubernetes,
конфигурации nginx, журналы доступа/аутентификации и трассировки ошибок.

При срабатывании запрос не отправляется в Analyzer и провайдеру, а сопоставление Redis `pii_mapping:*` не создаётся:

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

Ответ и структурированные журналы содержат только ограниченные категории, идентификаторы правил и счётчики, без исходной полезной нагрузки, фрагментов, смещений или значений секретов. Если нужно временно разрешить такие данные в среде разработки, задайте `PRE_EGRESS_POLICY_MODE=off`; маскирование/блокировка персональных данных при этом продолжит работать отдельно. После изменения этой переменной в `.env` пересоздайте контейнер LiteLLM: `docker compose up -d --force-recreate --no-deps litellm`.

В зависимости от обёртки LiteLLM/FastAPI JSON может быть вложен как `detail.error`, `error.provider_specific_fields.error` или `error.param.pre_egress_policy`, но поля `message`, `type`, `code`, `details.categories` и `details.rules` остаются обязательными.

Быстрая проверка «чёрного ящика» с тестовым LiteLLM-прокси и имитацией внешнего провайдера проверяет `/v1/chat/completions`, `/v1/responses` и `/v1/messages`: чистый запрос доходит до Analyzer и провайдера, а заблокированная конфигурационная полезная нагрузка не доходит ни до Analyzer, ни до провайдера:

```bash
make test-pre-egress-proxy
```

## Финальная проверка полезной нагрузки перед провайдером

`FINAL_PAYLOAD_LEAK_CHECK_MODE=block` включён по умолчанию и работает после изменений на стороне прокси: маскирование персональных данных уже применено к изменяемым текстовым полям запроса, а контейнеры запроса перед провайдером `messages` / `input` / `instructions` / `system`, `tools` / `tool_choice`, устаревшие `functions` / `function_call`, `prediction`, `response_format`, `text`, провайдерский `extra_body`, `stop` / `stop_sequences`, `prompt_cache_key`, `safety_identifier`, `web_search_options`, `user` и провайдерские `metadata` дополнительно просканированы без изменения. Внешний провайдер на этом этапе ещё не вызван. Этот слой останавливает настроенные контрольные маркеры из `FINAL_PAYLOAD_LEAK_CHECK_CANARIES` и уверенно распознанные признаки исходной утечки вроде `BEGIN PRIVATE KEY`, bearer/JWT-подобных токенов, значений, похожих на ключи провайдеров, и присваиваний секретов в стиле `.env`.

При срабатывании запрос не отправляется провайдеру. Каноническое тело ошибки защитного слоя:

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

LiteLLM-прокси может обернуть это тело и вернуть `error.code="422"`, сохранив безопасное сообщение. Ответ, структурированные журналы и метрика `ru_final_payload_leak_check_blocked_total` содержат только ограниченные идентификаторы правил и счётчики, без исходных найденных значений, фрагментов запроса, смещений, ключей провайдеров или содержимого сопоставлений. Если нужно временно отключить слой в среде разработки, задайте `FINAL_PAYLOAD_LEAK_CHECK_MODE=off`; маскирование/блокировка персональных данных и `PRE_EGRESS_POLICY_MODE` продолжат работать отдельно.

Быстрая проверка «чёрного ящика» с тестовым LiteLLM-прокси и имитацией провайдера, совместимого с OpenAI API, проверяет, что Analyzer видит настроенный контрольный маркер и маркер приватного ключа, контрольный маркер в схеме инструмента блокируется без выхода к провайдеру, а очищенный запрос с персональными данными доходит до провайдера только с плейсхолдером:

```bash
make test-final-leak-proxy
```

## Закрепление маршрута за развёртыванием

Если за моделью настроено несколько развёртываний, LiteLLM должен удерживать один клиентский ключ на одном доступном развёртывании. Для быстрой проверки:

```bash
make routing-smoke
```

Команда отправляет два запроса к живому сервису одним ключом и сравнивает заголовок `x-litellm-model-id`.

Если хотите проверять не административный ключ, а пользовательский ключ, задайте его в `.env`:

```env
LITELLM_ROUTING_TEST_KEY=sk-...
```

Подробности настройки нескольких аккаунтов одной модели: [routing.md](routing.md).

## Метрики

Метрики LiteLLM и защитного слоя персональных данных доступны через маршрут Prometheus:

```bash
curl -L -s "$API_URL/metrics" | grep -E '^(litellm_|ru_)' | head
```

То же самое через Makefile:

```bash
make metrics
make monitor-smoke
```

Метрики защитного слоя `ru_pii_guardrail_*` появятся после первого запроса, который прошёл через него. Метрики не содержат исходные персональные данные или текст пользовательского запроса.

## Клиентские гайды

- Codex CLI / локальные задачи Codex App: [clients/codex.md](clients/codex.md)
- Claude Code: [clients/claude-code.md](clients/claude-code.md)
- ZCode: [clients/zcode.md](clients/zcode.md)
- OpenCode CLI / Desktop: [clients/opencode.md](clients/opencode.md)
- Kilo Code VS Code / CLI: [clients/kilo-code.md](clients/kilo-code.md)
- JWT/OIDC proxy auth: [clients/jwt.md](clients/jwt.md)

Для ZCode используйте `Use API Key` и настройки провайдера, совместимого с OpenAI API: `OpenAI Base URL = http://localhost:4000/v1`, `API Key = $RU_LLM_PROXY_TOKEN`, модель `glm-5.2`. `ZAI_API_KEY` и `ZAI_API_KEY_2` остаются только в окружении прокси.

## Добавление моделей

По умолчанию настроен профиль с GLM как основной моделью: `glm-5.2` как публичное имя модели по умолчанию и `glm-5.1` как дополнительная модель. Чтобы добавить ещё одного провайдера, скопируйте и адаптируйте пример из `examples/litellm-config.optional-providers.yaml`, добавьте нужный API-ключ в окружение и перезапустите LiteLLM:

```yaml
model_list:
  - model_name: openai-example-standard
    litellm_params:
      model: openai/<validated-openai-standard-model-id>
      api_key: os.environ/OPENAI_API_KEY
    model_info:
      id: openai-example-standard-primary
      base_model: <validated-openai-standard-model-id>
      access_groups: ["openai", "standard"]
```

```bash
make restart
```

Если это второе развёртывание той же публичной модели, оставьте прежний `model_name`, но задайте новый `model_info.id`. Так LiteLLM сможет корректно хранить закрепление клиентского ключа за развёртыванием.

## Потоковая передача

LiteLLM принимает потоковые запросы, а `ru-pii-mask-post` восстанавливает плейсхолдеры текущего запроса в потоковых `delta.content` и `delta.reasoning_content`. Защитный слой удерживает возможный суффикс плейсхолдера между фрагментами потока, поэтому `<PHONE_` в одном фрагменте и `NUMBER_1>` в следующем клиент получит как исходное значение из сопоставления Redis.

```bash
curl -sS "$API_URL/v1/chat/completions" \
  -H "Authorization: Bearer $RU_LLM_PROXY_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "glm-5.2",
    "stream": true,
    "guardrails": ["ru-pii-mask-pre", "ru-pii-mask-post"],
    "messages": [
      {"role": "user", "content": "Проверь телефон +79031234567"}
    ]
  }'
```

Потоковое восстановление покрывает текстовые дельты. Если провайдер передаёт плейсхолдеры внутри потоковых дельт аргументов вызова инструмента/функции, они могут остаться в ответе как плейсхолдеры; исходные персональные данные при этом не раскрываются.
