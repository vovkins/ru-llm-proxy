# JWT/OIDC-аутентификация

JWT/OIDC заменяет пользовательский ключ только на входе в прокси. Серверные
ключи провайдеров остаются без изменений. Схема не включена по умолчанию и
требует поставщика идентификации, JWKS, политики issuer/audience и подходящей
редакции LiteLLM.

Административные `/ui` и Admin API образуют отдельную границу:
[admin-access.md](../admin-access.md).

## Базовая настройка

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

Клиент отправляет JWT как bearer-токен:

```bash
curl "$RU_LLM_PROXY_URL/v1/chat/completions" \
  -H "Authorization: Bearer $OIDC_JWT" \
  -H "Content-Type: application/json" \
  -d '{"model":"glm-5.2","messages":[{"role":"user","content":"Привет"}]}'
```

## Сопоставление с пользовательским ключом

Для серверных бюджетов, лимитов и доступа к моделям сопоставьте JWT client ID с
пользовательским ключом LiteLLM:

```yaml
general_settings:
  enable_jwt_auth: true
  litellm_jwtauth:
    user_id_jwt_field: "sub"
    team_id_jwt_field: "team_id"
    jwt_client_id_field: "client_id"
    unregistered_jwt_client_behavior: "reject"
```

Создание сопоставления является административной операцией и выполняется через
`POST /jwt_client/new` с `LITELLM_MASTER_KEY`. Пользователь получает JWT от IdP,
но не административный или провайдерский ключ.

Codex и Claude Code могут получать JWT через поддерживаемого помощника токена.
OpenCode и Kilo Code проще использовать со статическим пользовательским ключом,
если у установки нет управляемого помощника.

## Ссылки

- [JWT-аутентификация LiteLLM](https://docs.litellm.ai/docs/proxy/token_auth)
- [Сопоставление JWT с ключами](https://docs.litellm.ai/docs/proxy/jwt_key_mapping)
