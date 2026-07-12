# Codex CLI / Codex App

Codex подключается к прокси через OpenAI Responses API.

Поддерживаемый сценарий:

- Codex CLI.
- локальные задачи Codex App, которые используют локальную конфигурацию Codex.

Что не входит в этот гайд:

- Codex cloud tasks и hosted integrations;
- передача ChatGPT/Codex login files на прокси как upstream credentials.

## Ключи доступа

Используйте LiteLLM virtual key как клиентский токен:

```bash
export RU_LLM_PROXY_TOKEN="sk-..."
```

Настоящий `OPENAI_API_KEY` в server-funded режиме остается только на proxy host. Не кладите `OPENAI_API_KEY` или `LITELLM_MASTER_KEY` в локальную конфигурацию Codex.

Все переменные окружения из этого гайда описаны в [../configuration.md](../configuration.md).

Обычные пользовательские ключи создавайте в LiteLLM Admin UI. CLI helper нужен только как дополнительный DevOps/CI/bootstrap путь с proxy host:

```bash
make virtual-key-create MODELS=openai,standard KEY_ALIAS=codex-local
```

Если запускаете скрипт напрямую, передайте те же значения флагами:

```bash
scripts/create_virtual_key.sh --alias codex-local --models openai,standard --duration 30d
```

## Server-funded настройка

Используйте этот режим, когда прокси должен платить серверным `OPENAI_API_KEY`. Добавьте custom provider в `~/.codex/config.toml`:

```toml
model_provider = "ru_llm_proxy"
model = "openai-example-standard"

[model_providers.ru_llm_proxy]
name = "ru-llm-proxy"
base_url = "http://localhost:4000/v1"
env_key = "RU_LLM_PROXY_TOKEN"
wire_api = "responses"
```

Модель большего класса используйте только если выданный ключ разрешает её alias:

```toml
model = "openai-example-premium"
```

OpenAI aliases сейчас являются optional provider examples. Добавляйте их из `examples/litellm-config.optional-providers.yaml` только после проверки raw model IDs на целевом LiteLLM image и конкретной подписке.

## ChatGPT subscription passthrough

Используйте этот режим, когда Codex должен работать с локальной ChatGPT/Codex
subscription пользователя, но всё ещё идти через proxy для guardrails, tracking и
proxy access control.

Это opt-in validation target, а не production-ready default. ChatGPT/Codex
subscription auth обычно опирается на provider `Authorization`; стандартный
LiteLLM path не считается способным гарантированно форвардить этот header
upstream. Дефолтный config репозитория не включает header forwarding.

Сначала войдите локально через Codex:

```bash
codex login
```

Затем настройте provider, который использует OpenAI authentication и отправляет
LiteLLM virtual key в отдельном proxy-auth header:

```toml
model_provider = "ru_llm_proxy_chatgpt"
model = "openai-example-standard"

[model_providers.ru_llm_proxy_chatgpt]
name = "ru-llm-proxy via ChatGPT auth"
base_url = "http://localhost:4000/v1"
wire_api = "responses"
requires_openai_auth = true
env_http_headers = { "x-litellm-api-key" = "RU_LLM_PROXY_TOKEN" }
```

В этом режиме:

- Codex хранит `~/.codex/auth.json` или OS credential-store auth на клиентской машине.
- Proxy аутентифицирует клиента через `x-litellm-api-key`.
- OpenAI/ChatGPT auth остаётся в provider auth path; перед использованием за пределами spike нужно доказать, что она доходит upstream.

Не копируйте общий Codex `auth.json` на proxy для всех пользователей. Если live
validation покажет, что обычный LiteLLM `/v1/responses` route удаляет нужный
provider `Authorization` header, этому режиму нужен pass-through route, sidecar
или custom adapter перед production-использованием.

## Локальные задачи Codex App

Для локальных app workflows держите provider config в `~/.codex/config.toml`.
Положите токен в `~/.codex/.env`, если процесс приложения не наследует shell
environment:

```env
RU_LLM_PROXY_TOKEN=sk-...
```

Перезапустите Codex App после изменения `~/.codex/.env`.

## Проверка

Из репозитория proxy:

```bash
make client-auth-smoke
```

Codex-specific часть проверяет `POST /v1/responses` только когда настроен
`OPENAI_API_KEY`, а `RESPONSES_MODEL` указывает на live-validated proxy alias:

```bash
RESPONSES_MODEL=<validated-responses-alias> make client-auth-smoke
```

## Ссылки

- Codex authentication: https://developers.openai.com/codex/auth
- Codex configuration: https://developers.openai.com/codex/config/
- Codex config reference: https://developers.openai.com/codex/config-reference/
- LiteLLM forward client headers: https://docs.litellm.ai/docs/proxy/forward_client_headers
- LiteLLM virtual keys: https://docs.litellm.ai/docs/proxy/virtual_keys
