# JWT / OIDC Proxy Auth

По умолчанию проект использует LiteLLM virtual keys как клиентские учетные данные. JWT/OIDC — это enterprise SSO путь для той же границы входа в прокси.

JWT/OIDC отвечает на тот же вопрос, что и virtual keys: “кто может пользоваться этим прокси?” Он не заменяет upstream provider credentials. Прокси по-прежнему использует server-side `OPENAI_API_KEY`, `ANTHROPIC_API_KEY` и `ZAI_API_KEY` для вызова провайдеров.

Все переменные окружения из этого гайда описаны в [../configuration.md](../configuration.md).

## Статус

JWT/OIDC auth не включен в дефолтном `litellm-config.yaml`, потому что требует:

- identity provider и JWKS URL;
- конкретную audience/issuer policy;
- LiteLLM Enterprise для JWT auth и JWT to virtual key mapping.

Дефолтный runnable setup остается на LiteLLM virtual keys. Используйте JWT/OIDC, когда развертыванию нужен SSO-backed доступ вместо раздачи proxy keys разработчикам.

Admin/operator access к `/ui` и admin API routes — отдельная граница от client JWT/OIDC auth. Защищайте ее средствами из [../admin-access.md](../admin-access.md).

## Базовая OIDC-аутентификация

Задайте IdP discovery values на proxy host:

```env
JWT_PUBLIC_KEY_URL=https://idp.example.com/.well-known/jwks.json
JWT_AUDIENCE=ru-llm-proxy
```

Затем включите JWT auth в `litellm-config.yaml` для конкретного deployment.
Базовый JWT auth маппит claims на LiteLLM users и teams; создайте LiteLLM teams,
у которых `team_id` совпадает с выбранными IdP claim values.

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

Клиенты отправляют JWT как bearer token:

```bash
curl "$RU_LLM_PROXY_URL/v1/chat/completions" \
  -H "Authorization: Bearer $OIDC_JWT" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "glm-5.2",
    "messages": [{"role": "user", "content": "Привет"}]
  }'
```

## JWT to virtual key mapping

Для per-user budgets, rate limits и model restrictions используйте LiteLLM JWT to virtual key mapping. Развертывание по-прежнему создает или маппит virtual keys, но пользователи аутентифицируются через свой OIDC JWT.

Добавьте client mapping claim в JWT config:

```yaml
general_settings:
  enable_jwt_auth: true
  litellm_jwtauth:
    user_id_jwt_field: "sub"
    team_id_jwt_field: "team_id"
    jwt_client_id_field: "client_id"
    unregistered_jwt_client_behavior: "reject"
```

Пример admin flow:

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

Сгенерированный virtual key остается server-managed. Разработчики используют JWT от IdP.

## Заметки для клиентов

- Codex custom providers могут использовать command-backed bearer token, где это доступно; команда должна печатать OIDC JWT.
- Claude Code может использовать `apiKeyHelper` для CLI workflows; helper должен печатать OIDC JWT или mapped proxy token, а не upstream Anthropic key.
- OpenCode и Kilo Code проще настраивать через static virtual keys. Используйте JWT только там, где deployment поддерживает token helper или managed config.

## Ссылки

- LiteLLM OIDC JWT auth: https://docs.litellm.ai/docs/proxy/token_auth
- LiteLLM JWT to virtual key mapping: https://docs.litellm.ai/docs/proxy/jwt_key_mapping
- LiteLLM virtual keys: https://docs.litellm.ai/docs/proxy/virtual_keys
