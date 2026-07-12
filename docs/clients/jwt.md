# JWT / OIDC-аутентификация прокси

По умолчанию проект использует пользовательские ключи LiteLLM как клиентские учётные данные. JWT/OIDC — это корпоративный SSO-путь для той же границы входа в прокси.

JWT/OIDC отвечает на тот же вопрос, что и пользовательские ключи: «кто может пользоваться этим прокси?» Он не заменяет учётные данные внешних провайдеров. Прокси по-прежнему использует серверные `OPENAI_API_KEY`, `ANTHROPIC_API_KEY` и `ZAI_API_KEY` для вызова провайдеров.

Все переменные окружения из этого гайда описаны в [../configuration.md](../configuration.md).

## Статус

JWT/OIDC-аутентификация не включена в `litellm-config.yaml` по умолчанию, потому что требует:

- поставщика идентичности и JWKS URL;
- конкретную политику audience/issuer;
- LiteLLM Enterprise для JWT-аутентификации и сопоставления JWT с пользовательскими ключами.

Запускаемая схема по умолчанию остается на пользовательских ключах LiteLLM. Используйте JWT/OIDC, когда установке нужен доступ через SSO вместо раздачи ключей прокси разработчикам.

Административный/операторский доступ к `/ui` и административным API-маршрутам — отдельная граница от клиентской JWT/OIDC-аутентификации. Защищайте ее средствами из [../admin-access.md](../admin-access.md).

## Базовая OIDC-аутентификация

Задайте значения обнаружения IdP на хосте прокси:

```env
JWT_PUBLIC_KEY_URL=https://idp.example.com/.well-known/jwks.json
JWT_AUDIENCE=ru-llm-proxy
```

Затем включите JWT-аутентификацию в `litellm-config.yaml` для конкретной установки.
Базовая JWT-аутентификация сопоставляет claims с пользователями и командами LiteLLM; создайте команды LiteLLM,
у которых `team_id` совпадает с выбранными значениями claims из IdP.

```yaml
general_settings:
  master_key: os.environ/LITELLM_MASTER_KEY
  database_url: os.environ/DATABASE_URL
  enable_jwt_auth: true
  litellm_jwtauth:
    user_id_jwt_field: "sub"
    team_ids_jwt_field: "groups"
    user_id_upsert: true
    enforce_team_based_model_access: true
```

Клиенты отправляют JWT как bearer-токен:

```bash
curl "$RU_LLM_PROXY_URL/v1/chat/completions" \
  -H "Authorization: Bearer $OIDC_JWT" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "glm-5.2",
    "messages": [{"role": "user", "content": "Привет"}]
  }'
```

## Сопоставление JWT с пользовательским ключом

Для бюджетов, лимитов частоты запросов и ограничений моделей на пользователя используйте сопоставление JWT с пользовательским ключом в LiteLLM. Развёртывание по-прежнему создает или сопоставляет пользовательские ключи, но пользователи аутентифицируются через свой OIDC JWT.

Добавьте claim для сопоставления клиента в JWT-конфигурацию:

```yaml
general_settings:
  enable_jwt_auth: true
  litellm_jwtauth:
    user_id_jwt_field: "sub"
    team_id_jwt_field: "team_id"
    jwt_client_id_field: "client_id"
    unregistered_jwt_client_behavior: "reject"
```

Пример административного потока:

```bash
curl -X POST "$RU_LLM_PROXY_URL/jwt_client/new" \
  -H "Authorization: Bearer $LITELLM_MASTER_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "jwt_claim_name": "client_id",
    "jwt_claim_value": "dev-alice",
    "models": ["standard", "openai", "anthropic"],
    "max_budget": 50,
    "budget_duration": "30d",
    "rpm_limit": 120,
    "tpm_limit": 100000,
    "team_id": "engineering"
  }'
```

Сгенерированный пользовательский ключ остается управляемым на стороне сервера. Разработчики используют JWT от IdP.

## Заметки для клиентов

- Пользовательские провайдеры Codex могут использовать bearer-токен, получаемый командой, где это доступно; команда должна печатать OIDC JWT.
- Claude Code может использовать `apiKeyHelper` для сценариев командной строки; помощник должен печатать OIDC JWT или сопоставленный токен прокси, а не внешний Anthropic-ключ.
- OpenCode и Kilo Code проще настраивать через статические пользовательские ключи. Используйте JWT только там, где установка поддерживает помощник токена или управляемую конфигурацию.

## Ссылки

- LiteLLM OIDC JWT auth: https://docs.litellm.ai/docs/proxy/token_auth
- Сопоставление JWT с пользовательскими ключами LiteLLM: https://docs.litellm.ai/docs/proxy/jwt_key_mapping
- LiteLLM virtual keys: https://docs.litellm.ai/docs/proxy/virtual_keys
