# Provider Routing and Sticky Affinity

Этот документ описывает, как `ru-llm-proxy` удерживает одного клиента на одном provider deployment, когда за публичной моделью настроено несколько провайдеров или несколько аккаунтов одного провайдера.

Переменные окружения, которые используются в routing examples и smoke-командах, описаны в [configuration.md](configuration.md).

## Зачем это нужно

Многие LLM-провайдеры умеют кэшировать входные токены/prompt prefix на своей стороне. Если запросы одного клиента постоянно попадают на разные аккаунты или разные provider deployments, эффективность такого кэша падает. Sticky routing повышает шанс, что повторяющийся контекст одного клиента будет обрабатываться одним и тем же deployment.

В проекте это не заменяет load balancing и failover. Это дополнительная pre-call проверка LiteLLM Router: если для клиентского ключа уже выбран healthy deployment, LiteLLM отдаёт приоритет ему.

## Текущая настройка

Sticky affinity включена в `litellm-config.yaml` через `optional_pre_call_checks`:

```yaml
router_settings:
  redis_url: os.environ/REDIS_URL
  routing_strategy: simple-shuffle
  optional_pre_call_checks:
    - deployment_affinity
  deployment_affinity_ttl_seconds: 86400
```

Ключевые параметры:

| Параметр | Назначение |
| --- | --- |
| `redis_url` | Хранилище для affinity mapping. Используется тот же Redis, что и для временных PII mappings. |
| `optional_pre_call_checks` | Включает pre-call проверку `deployment_affinity`. |
| `deployment_affinity_ttl_seconds` | TTL привязки клиентского ключа к deployment. Сейчас `86400` секунд. |
| `routing_strategy` | Базовая стратегия выбора deployment, если affinity ещё нет или pinned deployment недоступен. |

## Как LiteLLM выбирает deployment

LiteLLM Proxy добавляет в request metadata хэш клиентского API key: `user_api_key_hash`. Проверка `deployment_affinity` использует этот идентификатор, а не raw token.

Поток выбора:

1. Клиент вызывает публичную модель, например `glm-5.2`.
2. LiteLLM получает список healthy deployments для этой model group.
3. Pre-call check ищет в Redis deployment id, ранее связанный с `user_api_key_hash`.
4. Если deployment найден и сейчас healthy, LiteLLM выбирает его.
5. Если mapping отсутствует, устарел или deployment недоступен, LiteLLM выбирает deployment обычной router strategy и сохраняет новую привязку.

Если в запросе когда-нибудь будет включена `session_affinity` с `metadata.session_id`, session affinity имеет приоритет над affinity по ключу. В текущем проекте включена именно key-based deployment affinity, потому что пользовательский LiteLLM token естественно соответствует клиенту/потребителю proxy.

## Stable deployment ids

Каждый deployment должен иметь стабильный `model_info.id`. Этот id попадает в affinity mapping, поэтому его нельзя менять без причины.

Текущий дефолтный model group:

```yaml
model_list:
  - model_name: glm-5.2
    litellm_params:
      model: openai/glm-5.2
      api_base: https://api.z.ai/api/coding/paas/v4
      api_key: os.environ/ZAI_API_KEY
    model_info:
      id: glm-5-2-zai-coding-primary
      base_model: glm-5.2

  - model_name: glm-5.2
    litellm_params:
      model: openai/glm-5.2
      api_base: https://api.z.ai/api/coding/paas/v4
      api_key: os.environ/ZAI_API_KEY_2
    model_info:
      id: glm-5-2-zai-coding-secondary
      base_model: glm-5.2
```

`model_name` — публичное имя, которое видит клиент. Несколько записей с одинаковым `model_name` образуют одну model group. `model_info.id` — внутренний стабильный deployment id.

`glm-5.1` остаётся дополнительным публичным алиасом для инсталляций, которым нужна предыдущая версия модели. Smoke-проверки и quick start по умолчанию используют `glm-5.2`.

## Два аккаунта Z.AI

Дефолтный runtime profile ожидает два Z.AI Coding Plan ключа:

```env
ZAI_API_KEY=...
ZAI_API_KEY_2=...
```

Обе переменные участвуют в `glm-5.2` model group сразу после запуска. Если оператор меняет состав deployments, правило остаётся тем же: один публичный `model_name`, разные upstream credentials и разные стабильные `model_info.id`.

Пример:

```yaml
model_list:
  - model_name: glm-5.2
    litellm_params:
      model: openai/glm-5.2
      api_base: https://api.z.ai/api/coding/paas/v4
      api_key: os.environ/ZAI_API_KEY
    model_info:
      id: glm-5-2-zai-coding-primary
      base_model: glm-5.2

  - model_name: glm-5.2
    litellm_params:
      model: openai/glm-5.2
      api_base: https://api.z.ai/api/coding/paas/v4
      api_key: os.environ/ZAI_API_KEY_2
    model_info:
      id: glm-5-2-zai-coding-secondary
      base_model: glm-5.2
```

Для другого провайдера или внутреннего deployment схема такая же: оставьте тот же публичный `model_name`, задайте provider-specific `litellm_params` и уникальный стабильный `model_info.id`.

## Проверка

Для smoke-проверки используйте:

```bash
make routing-smoke
```

Команда отправляет два live request с одним и тем же ключом, проверяет HTTP status каждого ответа, читает response header `x-litellm-model-id` и проверяет, что оба запроса попали в один deployment.

Если LiteLLM недоступен, провайдер вернул `4xx/5xx` или proxy не вернул `x-litellm-model-id`, команда завершится с ненулевым кодом. При HTTP-ошибке она печатает status и безопасные response headers, но не печатает proxy token или request payload.

По умолчанию используется `LITELLM_MASTER_KEY`. Для более реалистичной проверки создайте virtual key в LiteLLM UI или через API, затем задайте его в `.env`:

```env
LITELLM_ROUTING_TEST_KEY=sk-...
```

После этого `make routing-smoke` будет использовать virtual key вместо master key.

Если оператор временно оставил только один deployment, smoke-тест тоже должен проходить, но он доказывает только работоспособность routing path и наличие `x-litellm-model-id`. Дефолтная конфигурация проекта рассчитана на два deployments, чтобы sticky affinity можно было проверять на реальном multi-deployment model group.

## Наблюдаемость

Для диагностики routing:

- проверяйте response header `x-litellm-model-id` на live-запросах;
- смотрите LiteLLM deployment metrics в `/metrics`, особенно `litellm_deployment_*`;
- держите `model_info.id` стабильными, чтобы метрики и affinity mapping не меняли смысл после перезапуска;
- используйте отдельные virtual keys для разных команд/приложений, чтобы affinity отражала реальных потребителей.

Redis хранит affinity mapping отдельно от PII mappings. TTL для PII задаётся `PII_MAPPING_TTL_SECONDS`, TTL для routing affinity — `deployment_affinity_ttl_seconds`.

## Обновления LiteLLM

`deployment_affinity` реализован в LiteLLM Router как optional pre-call check. Если текущий image не принимает `optional_pre_call_checks` или не возвращает `x-litellm-model-id`, обновите LiteLLM:

```bash
make update-litellm
```

После обновления проверьте:

```bash
make health
make guardrails-list
make routing-smoke
make monitor-smoke
```

## References

- [LiteLLM load balancing](https://docs.litellm.ai/docs/proxy/load_balancing)
- [LiteLLM router settings per key/team](https://docs.litellm.ai/docs/proxy/keys_teams_router_settings)
- [LiteLLM tag routing](https://docs.litellm.ai/docs/proxy/tag_routing)
- [LiteLLM deployment affinity source](https://github.com/BerriAI/litellm/blob/main/litellm/router_utils/pre_call_checks/deployment_affinity_check.py)
