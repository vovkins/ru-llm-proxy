# JWT / OIDC Proxy Auth

This project uses LiteLLM virtual keys as the default client credential. JWT/OIDC is the enterprise SSO path for the same proxy ingress boundary.

JWT/OIDC answers the same question as virtual keys: "Who may use this proxy?" It does not replace upstream provider credentials. The proxy still uses server-side `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, and `ZAI_API_KEY` to call providers.

## Status

JWT/OIDC auth is not enabled in the default `litellm-config.yaml` because it requires:

- an identity provider and JWKS URL;
- a concrete audience/issuer policy;
- LiteLLM Enterprise for JWT auth and JWT to virtual key mapping.

The default runnable setup stays on LiteLLM virtual keys. Use JWT/OIDC when the deployment needs SSO-backed access instead of distributing proxy keys to developers.

## Base OIDC Auth

Set IdP discovery values on the proxy host:

```env
JWT_PUBLIC_KEY_URL=https://idp.example.com/.well-known/jwks.json
JWT_AUDIENCE=ru-llm-proxy
```

Then enable JWT auth in `litellm-config.yaml` for that deployment. Base JWT auth maps claims to LiteLLM users and teams; create LiteLLM teams whose `team_id` values match the IdP claim values you choose.

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

Clients then send the JWT as the bearer token:

```bash
curl "$RU_LLM_PROXY_URL/v1/chat/completions" \
  -H "Authorization: Bearer $OIDC_JWT" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "zai-glm-5.1",
    "messages": [{"role": "user", "content": "Привет"}]
  }'
```

## JWT To Virtual Key Mapping

For per-user budgets, rate limits, and model restrictions, use LiteLLM JWT to virtual key mapping. The deployment still creates or maps virtual keys, but users authenticate with their OIDC JWT.

Add a client mapping claim to the JWT config:

```yaml
general_settings:
  enable_jwt_auth: true
  litellm_jwtauth:
    user_id_jwt_field: "sub"
    team_id_jwt_field: "team_id"
    jwt_client_id_field: "client_id"
    unregistered_jwt_client_behavior: "reject"
```

Example admin flow:

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

The generated virtual key remains server-managed. Developers use their JWT from the IdP.

## Client Notes

- Codex custom providers can use a command-backed bearer token where available; that command should print the OIDC JWT.
- Claude Code can use `apiKeyHelper` for CLI workflows; the helper should print the OIDC JWT or mapped proxy token, not an upstream Anthropic key.
- OpenCode and Kilo Code public config paths are simpler with static virtual keys. Use JWT only when your deployment has a supported token helper or managed config.

## References

- LiteLLM OIDC JWT auth: https://docs.litellm.ai/docs/proxy/token_auth
- LiteLLM JWT to virtual key mapping: https://docs.litellm.ai/docs/proxy/jwt_key_mapping
- LiteLLM virtual keys: https://docs.litellm.ai/docs/proxy/virtual_keys
