# Примеры API

Примеры рассчитаны на LiteLLM по адресу `localhost:4000`, модель `glm-5.2` и
пользовательский ключ прокси. Настройки окружения описаны в
[configuration.md](configuration.md), правила выдачи ключей — в
[admin-access.md](admin-access.md).

## Подготовка

```bash
export API_URL="http://localhost:4000"
export RU_LLM_PROXY_TOKEN="sk-..."
```

Создать ключ для локальной проверки можно командой:

```bash
make virtual-key-create KEY_ALIAS=local-examples MODELS=standard,zai DURATION=30d
```

`LITELLM_MASTER_KEY` оставляйте только администраторам.

## Chat Completions

```bash
curl -s "$API_URL/v1/chat/completions" \
  -H "Authorization: Bearer $RU_LLM_PROXY_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "glm-5.2",
    "messages": [{
      "role": "user",
      "content": "Клиент Иванов Иван, телефон +79031234567, ИНН 7707083893. Составь краткую справку."
    }],
    "max_tokens": 120
  }' | jq '.choices[0].message'
```

В режиме маскирования провайдер получает:

```text
Клиент <PERSON_1>, телефон <PHONE_NUMBER_1>, ИНН <RU_INN_1>. Составь краткую справку.
```

Значения восстанавливаются, только если модель вернула служебные метки без
изменений.

### Потоковый ответ

```bash
curl -sS "$API_URL/v1/chat/completions" \
  -H "Authorization: Bearer $RU_LLM_PROXY_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "glm-5.2",
    "stream": true,
    "guardrails": ["ru-pii-mask-pre", "ru-pii-mask-post"],
    "messages": [{"role":"user","content":"Проверь телефон +79031234567"}]
  }'
```

В потоке восстанавливаются `delta.content` и `delta.reasoning_content`, включая
служебные метки, разорванные между фрагментами. Аргументы вызовов инструментов в
потоковых дельтах пока не восстанавливаются.

## ChatGPT OAuth и OpenAI Responses API

Сначала примените пул подписок по
[инструкции настройки](configuration.md#локальные-профили-openai-oauth) и выдайте
пользовательскому ключу доступ к нужным моделям. Для регулярных проверок
используйте Luna:

```bash
curl -s "$API_URL/v1/responses" \
  -H "Authorization: Bearer $RU_LLM_PROXY_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "gpt-5.6-luna",
    "input": "Скажи короткое приветствие на русском",
    "store": false,
    "max_output_tokens": 80
  }' | jq

RESPONSES_MODEL=gpt-5.6-luna make client-auth-smoke
```

Sol и Terra доступны под явными именами `gpt-5.6-sol` и `gpt-5.6-terra`.
Общий алиас `gpt-5.6` в режим пула не входит.

## Дополнительный базовый пример Anthropic Messages API

Пример также требует совместимого имени модели из файла дополнительных
провайдеров.

```bash
curl -s "$API_URL/v1/messages" \
  -H "Authorization: Bearer $RU_LLM_PROXY_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "anthropic-example-standard",
    "max_tokens": 80,
    "messages": [{"role":"user","content":"Скажи короткое приветствие"}]
  }' | jq

MESSAGES_MODEL=<validated-messages-alias> make client-auth-smoke
```

Полный контракт шлюза для Claude Code строже этого примера: он включает
`POST /v1/messages?beta=true`, потоковые SSE-ответы, `anthropic-version`,
`anthropic-beta`, подсчёт токенов и обнаружение моделей. Текущий статус:
[clients/claude-code.md](clients/claude-code.md).

## Presidio Analyzer

### Базовый запрос

```bash
curl -s http://localhost:5001/api/v1/analyze \
  -H "Content-Type: application/json" \
  -d '{
    "text": "Мой телефон +79031234567 и ИНН 7707083893",
    "language": "ru",
    "score_threshold": 0.35
  }' | jq
```

При `PRESIDIO_ANALYZER_DETECT_BARE_INN_BY_CHECKSUM=true` корректный 12-значный
ИНН проходит `score_threshold=0.35` без контекста. 10-значный ИНН требует
контекст. При `false` контекст требуется для любого ИНН.

### Поддерживаемые сущности

| Группа | Сущности |
| --- | --- |
| Персональные данные и документы | `PERSON`, `LOCATION`, `ORGANIZATION`, `PHONE_NUMBER`, `EMAIL_ADDRESS`, `RU_INN`, `RU_KPP`, `RU_OGRN`, `RU_OGRNIP`, `RU_SNILS`, `RU_PASSPORT`, `CREDIT_CARD`, `RU_ADDRESS`, `CONTRACT_NUMBER` |
| Банковские реквизиты | `RU_BIK`, `RU_SETTLEMENT_ACCOUNT`, `RU_CORRESPONDENT_ACCOUNT` |
| Инфраструктура и секреты | `INTERNAL_IP`, `INTERNAL_DOMAIN`, `HOSTNAME`, `DB_URL`, `JWT`, `BEARER_TOKEN`, `PRIVATE_KEY`, `API_KEY`, `SECRET_KEY`, `AUTH_TOKEN`, `LOGIN`, `PASSWORD` |

`RU_KPP`, `RU_BIK`, `RU_SETTLEMENT_ACCOUNT` и `RU_CORRESPONDENT_ACCOUNT`
требуют контекст. `RU_OGRN` и `RU_OGRNIP` проходят проверку контрольной суммы.
`RU_ADDRESS` ограничен структурированными формами вроде `ул. Ленина, д. 10`;
свободные и неоднозначные адреса покрываются не полностью.

`RU_PASSPORT` охватывает внутренние и заграничные паспорта, военные билеты и
свидетельства о рождении. `LOGIN` распознаёт значения переменных окружения и
технических пользователей в явном контексте. `HOSTNAME` возвращает только имя
узла, без слов `хост`, `сервер` или команды `ssh`. Все корректные IP-адреса
распознаются по умолчанию; для режима только внутренних адресов задайте
`PRESIDIO_ANALYZER_DETECT_PUBLIC_IPS=false`.

Пример реквизитов:

```bash
curl -s http://localhost:5001/api/v1/analyze \
  -H "Content-Type: application/json" \
  -d '{
    "text": "КПП 770801001, ОГРН 1027700132195, БИК 044525225, расчетный счет 40702810900000000000, к/с 30101810400000000225",
    "language": "ru",
    "score_threshold": 0.35
  }' | jq
```

Пример инфраструктуры и секретов:

```bash
curl -s http://localhost:5001/api/v1/analyze \
  -H "Content-Type: application/json" \
  -d '{
    "text": "Endpoint 10.24.3.7, api.corp.local, Authorization: Bearer abcdefghijklmnopqrstuvwxyz123456, DATABASE_URL=postgresql://user:pass@db.internal/app",
    "language": "ru",
    "score_threshold": 0.35
  }' | jq
```

### Фильтрация сущностей

Поле `entities` ограничивает и детерминированные распознаватели, и NER:

```bash
curl -s http://localhost:5001/api/v1/analyze \
  -H "Content-Type: application/json" \
  -d '{
    "text": "Иван Иванов из Москвы, ИНН 7707083893",
    "language": "ru",
    "entities": ["RU_INN"]
  }' | jq
```

NER поддерживает `PERSON`, `LOCATION`, `ORGANIZATION`, `LOGIN`, `PASSWORD`,
`AUTH_TOKEN`, `SECRET_KEY` и `CONTRACT_NUMBER`. Номер договора требует контекст
`договор`, `госконтракт`, `контракт` или `соглашение`.

### Состояние Analyzer

```bash
curl -s http://localhost:5001/api/v1/health | jq
```

Рабочее состояние содержит `"ner_state":"ready"` и
`"ner_warmed_up":true`. Ошибка обязательной модели возвращает безопасный `503`
и сохраняет `unhealthy` до перезапуска.

## Режим блокировки PII

Задайте `PII_GUARDRAIL_MODE=block` и пересоздайте LiteLLM. Запрос с найденными
персональными данными завершится до провайдера:

```json
{
  "error": {
    "message": "Request contains personal data and was blocked by PII policy.",
    "type": "pii_detected",
    "code": "pii_blocked",
    "details": {"entities": ["PHONE_NUMBER"]}
  }
}
```

Исходные значения, текст и смещения в ошибку не включаются.

## Словарные подстановки

`DICTIONARY_SUBSTITUTIONS_ENABLED=true` включает правила из
`dictionary-substitutions.default.json`. Пример правила:

```json
{
  "substitutions": [{
    "id": "tbank_to_zetta",
    "enabled": true,
    "source": "Т-Банк",
    "replacement": "Зетта Групп",
    "match": {"case_sensitive": false, "whole_phrase": true},
    "restore": true
  }]
}
```

Провайдер получает `Зетта Групп`, клиент — восстановленное `Т-Банк`. При
`DICTIONARY_SUBSTITUTIONS_FAILURE_MODE=fail_closed` неоднозначная замена
блокирует запрос. Склонённое или перефразированное моделью значение не
восстанавливается.

## Синтетические персональные данные

В промышленной среде оставляйте `SYNTHETIC_PII_ALLOWLIST_MODE=off`. Для
контролируемого synthetic/test-набора можно разрешить точные значения, например
адреса в домене `example.test`:

```env
SYNTHETIC_PII_ALLOWLIST_MODE=allow
SYNTHETIC_PII_ALLOWLIST_JSON=[{"rule_id":"docs","entity_types":["EMAIL_ADDRESS"],"patterns":["^[A-Za-z0-9._%+-]+@example\\.test$"]}]
```

Разрешённое исходное значение проходит без маскирования; остальные PII в том же
запросе обрабатываются как обычно. Срабатывания считает
`ru_synthetic_pii_allowlist_hits_total`, но сами значения не записываются.
Слишком широкие шаблоны вроде `^.*$` игнорируются.

## Регулируемые темы

`REGULATED_TOPIC_POLICY_MODE=block` включает консервативную блокировку
материалов по AML/CFT / ПОД/ФТ, санкционным проверкам и мониторингу транзакций.
Это не персональные данные: политика не маскирует текст и не создаёт
сопоставление в Redis.

```json
{
  "error": {
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

`ru_regulated_topic_policy_blocked_total` и журналы содержат категории и
идентификаторы правил, но не исходный запрос.

## Предварительная проверка конфигураций и журналов

`PRE_EGRESS_POLICY_MODE=block` проверяет исходную полезную нагрузку до `POST /api/v1/analyze`.
Выгрузки `.env`, kubeconfig, конфигурации nginx, журналы
и трассировки блокируются до Analyzer, Redis и провайдера.

```json
{
  "error": {
    "type": "pre_egress_policy_violation",
    "code": "pre_egress_policy_blocked",
    "details": {"categories": ["config"], "rules": ["env_secret_assignment"]}
  }
}
```

LiteLLM может вложить тело в `detail.error`,
`error.provider_specific_fields.error` или `error.param.pre_egress_policy`.
После изменения режима пересоздайте сервис:

```bash
docker compose up -d --force-recreate --no-deps litellm
make test-pre-egress-proxy
```

## Финальная проверка перед провайдером

`FINAL_PAYLOAD_LEAK_CHECK_MODE=block` сканирует изменённую нагрузку после
маскирования. Проверяются `messages`, `input`, `instructions`, `system`, `tools`,
`tool_choice`, устаревшие `functions`, `function_call`, `prediction`,
`response_format`, `text`, `extra_body`, `stop`, `stop_sequences`,
`prompt_cache_key`, `safety_identifier`, `web_search_options`, `user` и
`metadata`. Слой ищет `FINAL_PAYLOAD_LEAK_CHECK_CANARIES`, приватные ключи,
токены, ключи провайдеров и присваиваний секретов в стиле `.env`.

```json
{
  "error": {
    "type": "final_payload_leak_check_violation",
    "code": "final_payload_leak_check_blocked",
    "details": {"rules": ["configured_canary"]}
  }
}
```

Метрика `ru_final_payload_leak_check_blocked_total` не содержит исходные
значения. Проверка с имитацией провайдера:

```bash
make test-final-leak-proxy
```

## Маршрутизация

```bash
make routing-smoke
```

Команда отправляет два запроса одним ключом и сравнивает
`x-litellm-model-id`. Подробности: [routing.md](routing.md).

## Метрики и защитные слои

```bash
make guardrails-list
make guardrails-smoke
curl -L -s "$API_URL/metrics" | grep -E '^(litellm_|ru_)' | head
```

`/metrics` не требует ключа LiteLLM. Ограничьте к нему сетевой доступ. Полный
справочник: [monitoring.md](monitoring.md).

## Клиенты и дополнительные модели

Руководства по ZCode, Codex, Claude Code, OpenCode, Kilo Code и JWT/OIDC:
[clients/README.md](clients/README.md). Прямая ссылка на ZCode:
[clients/zcode.md](clients/zcode.md).

Пул ChatGPT OAuth включается отдельным файлом Compose. Обычные API-ключи OpenAI
и Anthropic остаются примерами в
[`examples/litellm-config.optional-providers.yaml`](../examples/litellm-config.optional-providers.yaml).
