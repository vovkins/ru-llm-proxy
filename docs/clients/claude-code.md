# Claude Code

Этот гайд описывает Claude Code как целевой gateway-клиент для ru-llm-proxy. Дефолтная конфигурация репозитория проверяет только basic Anthropic Messages auth path; полноценный Claude Code gateway пока не считается полностью валидированным.

Что сейчас валидируется:

- Basic Anthropic Messages clients, которые умеют задавать `ANTHROPIC_BASE_URL` и отправлять LiteLLM virtual key.
- Claude Code CLI setup как целевой путь, для которого еще нужна отдельная gateway validation.

Пока не полностью проверено:

- полный Claude Code gateway contract `POST /v1/messages?beta=true`;
- streaming SSE behavior через proxy;
- forwarding `anthropic-version` и `anthropic-beta` в дефолтном config;
- optional token counting и model discovery endpoints.

Что не входит в этот гайд:

- размещение общих Claude.ai или Claude Code login files на proxy как upstream credentials;
- Claude Desktop или cloud features, которые не используют те же gateway settings.

## Ключи доступа

Используйте LiteLLM virtual key как клиентский токен:

```bash
export RU_LLM_PROXY_TOKEN="sk-..."
```

Настоящий `ANTHROPIC_API_KEY` в server-funded режиме остается только на proxy host. Не кладите `ANTHROPIC_API_KEY` или `LITELLM_MASTER_KEY` в локальную конфигурацию Claude Code.

Все переменные окружения из этого гайда описаны в [../configuration.md](../configuration.md).

Обычные пользовательские ключи создавайте в LiteLLM Admin UI. CLI helper нужен только как дополнительный DevOps/CI/bootstrap путь с proxy host:

```bash
scripts/create_virtual_key.sh --alias claude-code-local --models anthropic,standard --duration 30d
```

## Server-funded настройка токена

Используйте этот режим, когда прокси должен платить серверным `ANTHROPIC_API_KEY`. Направьте Claude Code на proxy для basic Anthropic Messages path:

```bash
export ANTHROPIC_BASE_URL="http://localhost:4000"
export ANTHROPIC_AUTH_TOKEN="$RU_LLM_PROXY_TOKEN"
export ANTHROPIC_MODEL="anthropic-example-standard"
```

Premium-модель используйте только если выданный ключ разрешает такой alias:

```bash
export ANTHROPIC_MODEL="anthropic-example-premium"
```

Anthropic aliases сейчас являются optional provider examples. Добавляйте их из `examples/litellm-config.optional-providers.yaml` только после проверки raw model IDs на целевом LiteLLM image и конкретной подписке.

Claude Code отправляет virtual key в proxy. Затем proxy использует серверный
`ANTHROPIC_API_KEY` для вызова Anthropic. Считайте это setup target до тех пор,
пока отдельный Claude Code gateway smoke не покроет `?beta=true`, SSE streaming
и forwarding `anthropic-*` headers.

## Anthropic API-Key BYOK

Используйте этот режим, когда клиент приносит собственный Anthropic API key, но
по-прежнему аутентифицируется в proxy через LiteLLM virtual key. Это opt-in
deployment mode; он не включён в дефолтном config репозитория.

LiteLLM BYOK forwarding рассчитан на provider-specific headers вроде
`x-api-key` и `api-key`. Proxy token остаётся отдельным:

```bash
export ANTHROPIC_BASE_URL="http://localhost:4000"

curl "$ANTHROPIC_BASE_URL/v1/messages" \
  -H "Content-Type: application/json" \
  -H "x-litellm-api-key: Bearer $RU_LLM_PROXY_TOKEN" \
  -H "x-api-key: $ANTHROPIC_BYOK_API_KEY" \
  -d '{"model":"anthropic-example-standard","max_tokens":32,"messages":[{"role":"user","content":"Reply with ok."}]}'
```

## Claude subscription passthrough

Используйте этот режим, когда Claude Code должен работать с локальной Claude
subscription пользователя, но всё ещё идти через proxy для guardrails, tracking и
proxy access control.

Это opt-in validation target, а не production-ready default. Claude subscription
auth обычно опирается на provider `Authorization`; стандартный LiteLLM path не
считается способным гарантированно форвардить этот header upstream. Дефолтный
config репозитория не включает header forwarding.

Не задавайте `ANTHROPIC_AUTH_TOKEN` равным proxy key в этом режиме. Вместо этого
передавайте proxy key через `ANTHROPIC_CUSTOM_HEADERS`, а Claude Code пусть
управляет Claude account auth локально:

```bash
export ANTHROPIC_BASE_URL="http://localhost:4000"
export ANTHROPIC_MODEL="anthropic-example-standard"
export ANTHROPIC_CUSTOM_HEADERS="x-litellm-api-key: Bearer $RU_LLM_PROXY_TOKEN"

claude
```

Если Claude Code попросит login, выберите Claude account subscription flow. Proxy
аутентифицирует запрос через `x-litellm-api-key`; Claude Code отдельно отправляет
Claude OAuth provider auth. Если live validation покажет, что обычный LiteLLM
`/v1/messages` route удаляет нужный provider `Authorization` header, этому режиму
нужен pass-through route, sidecar или custom adapter перед production-использованием.

Не копируйте общий Claude credentials file на proxy для всех пользователей. Этот
режим нужно live-валидировать на pinned LiteLLM image перед production rollout.

## Dynamic token setup

Claude Code поддерживает `apiKeyHelper` для CLI workflows. Используйте его,
когда локальная команда должна получать или ротировать proxy token:

```json
{
  "apiKeyHelper": "/absolute/path/to/print-ru-llm-proxy-token.sh"
}
```

Helper должен печатать LiteLLM virtual key, а не upstream Anthropic API key.

## Поверхность Anthropic Messages gateway

Для полностью валидированного Claude Code gateway через `ANTHROPIC_BASE_URL`
основной inference path выглядит так:

```text
POST /v1/messages?beta=true
```

Gateway должен сохранять Anthropic Messages semantics, ретранслировать streaming
SSE responses и передавать `anthropic-version` и `anthropic-beta` без изменений.
LiteLLM поддерживает Anthropic-compatible `/v1/messages` endpoint, но в этом
репозитории ещё нет отдельного Claude Code gateway smoke для `?beta=true`
streaming path и header forwarding в дефолтном config.

Опциональные Claude Code gateway endpoints:

```text
POST /v1/messages/count_tokens
GET /v1/models?limit=1000
```

`POST /v1/messages/count_tokens` опционален; при его отсутствии Claude Code
может вернуться к локальной оценке контекста. `GET /v1/models?limit=1000` —
опциональный model discovery endpoint, когда gateway model discovery включён.

`make client-auth-smoke` — basic Anthropic Messages auth smoke. Он отправляет
non-streaming `POST /v1/messages` с LiteLLM virtual key только когда настроен
`ANTHROPIC_API_KEY`, а `MESSAGES_MODEL` указывает на live-validated proxy alias:

```bash
MESSAGES_MODEL=<validated-messages-alias> make client-auth-smoke
```

Он не проверяет Claude Code `?beta=true` query, forwarding `anthropic-*` headers,
SSE streaming, token counting или model discovery. Добавьте отдельный Claude Code
gateway smoke перед тем, как считать эту поверхность полностью валидированной.

## Ссылки

- Claude Code LLM gateway: https://docs.anthropic.com/en/docs/claude-code/llm-gateway
- Claude Code environment variables: https://docs.anthropic.com/en/docs/claude-code/settings#environment-variables
- LiteLLM Claude Code Max subscription: https://docs.litellm.ai/docs/tutorials/claude_code_max_subscription
- LiteLLM forward client headers: https://docs.litellm.ai/docs/proxy/forward_client_headers
- LiteLLM Anthropic Messages API: https://docs.litellm.ai/docs/anthropic_unified/
