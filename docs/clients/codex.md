# Codex CLI / Codex App

Codex подключается к прокси через OpenAI Responses API.

Поддерживаемый сценарий:

- Codex CLI.
- локальные задачи Codex App, которые используют локальную конфигурацию Codex.

Что не входит в этот гайд:

- облачные задачи Codex и размещённые интеграции;
- передача файлов входа ChatGPT/Codex на прокси как учётных данных внешнего провайдера.

## Ключи доступа

Используйте пользовательский ключ LiteLLM как клиентский токен:

```bash
export RU_LLM_PROXY_TOKEN="sk-..."
```

Настоящий `OPENAI_API_KEY` в режиме, где провайдера оплачивает и вызывает прокси, остается только на хосте прокси. Не кладите `OPENAI_API_KEY` или `LITELLM_MASTER_KEY` в локальную конфигурацию Codex.

Все переменные окружения из этого гайда описаны в [../configuration.md](../configuration.md).

Обычные пользовательские ключи создавайте в административном интерфейсе LiteLLM. Вспомогательный скрипт командной строки нужен только как дополнительный путь для DevOps, CI и первичной настройки с хоста прокси:

```bash
make virtual-key-create MODELS=openai,standard KEY_ALIAS=codex-local
```

Если запускаете скрипт напрямую, передайте те же значения флагами:

```bash
scripts/create_virtual_key.sh --alias codex-local --models openai,standard --duration 30d
```

## Настройка с оплатой провайдера на стороне прокси

Используйте этот режим, когда прокси должен вызывать провайдера через серверный `OPENAI_API_KEY`. Добавьте пользовательского провайдера в `~/.codex/config.toml`:

```toml
model_provider = "ru_llm_proxy"
model = "openai-example-standard"

[model_providers.ru_llm_proxy]
name = "ru-llm-proxy"
base_url = "http://localhost:4000/v1"
env_key = "RU_LLM_PROXY_TOKEN"
wire_api = "responses"
```

Модель большего класса используйте только если выданный ключ разрешает её публичное имя:

```toml
model = "openai-example-premium"
```

Имена моделей OpenAI сейчас являются примерами дополнительных провайдеров. Добавляйте их из `examples/litellm-config.optional-providers.yaml` только после проверки исходных идентификаторов моделей на целевом образе LiteLLM и конкретной подписке.

## Сквозная передача подписки ChatGPT

Используйте этот режим, когда Codex должен работать с локальной ChatGPT/Codex
подпиской пользователя, но всё ещё идти через прокси для защитных слоёв, учёта и
контроля доступа на прокси.

Репозиторий содержит отдельный opt-in контур `chatgpt-codex`. Он не меняет
обычный GLM-контур и включает специальный guardrail, который разделяет два вида
авторизации:

- `x-litellm-api-key` проверяется самим ru-llm-proxy;
- OAuth из `Authorization` текущего пользователя пересылается в ChatGPT backend;
- остальные входящие HTTP-заголовки, включая ключ прокси, не пересылаются;
- PII и словарные термины маскируются до OAuth bridge и восстанавливаются в
  unary/streaming-ответах Responses API.

Сначала войдите локально через Codex:

```bash
codex login
```

Запустите профиль на порту из `.env` (`4100` в локальной конфигурации):

```bash
docker compose \
  -f docker-compose.yml \
  -f docker-compose.chatgpt-codex.yml \
  up -d --build db redis presidio-analyzer litellm
```

Установите готовую конфигурацию Codex:

```bash
cp config/ru-chatgpt-codex.config.toml \
  ~/.codex/ru-chatgpt-codex.config.toml
```

Её эквивалент:

```toml
model_provider = "ru-chatgpt-codex"
model = "gpt-5.6-sol"

[model_providers.ru-chatgpt-codex]
name = "ru-llm-proxy via ChatGPT auth"
base_url = "http://127.0.0.1:4100/v1"
wire_api = "responses"
requires_openai_auth = true
supports_websockets = false
env_http_headers = { "x-litellm-api-key" = "RU_LLM_PROXY_TOKEN" }
```

В этом режиме:

- Codex хранит OAuth в системном хранилище или локальном auth-файле клиента.
- Прокси аутентифицирует клиента через `x-litellm-api-key`.
- Guardrail получает исходный OAuth из закрытого `secret_fields.raw_headers` LiteLLM,
  устанавливает его как per-request provider key и удаляет provider-несовместимое
  поле `metadata` из egress payload.

Не копируйте общий Codex `auth.json` на прокси для всех пользователей. Если проверка на живом сервисе покажет, что обычный маршрут LiteLLM `/v1/responses` удаляет нужный
провайдерский заголовок `Authorization`, проверьте, что запущен именно overlay
`docker-compose.chatgpt-codex.yml`, а в `/guardrails/list` присутствует
`chatgpt-codex-auth`.

Перед запуском задайте клиентский ключ прокси и откройте Codex:

```bash
export RU_LLM_PROXY_TOKEN="sk-..."
codex --profile ru-chatgpt-codex
```

Для локальной проверки вместо virtual key можно временно использовать
`LITELLM_MASTER_KEY` из `.env`, не выводя его в терминал:

```bash
set -a
source .env
set +a
export RU_LLM_PROXY_TOKEN="$LITELLM_MASTER_KEY"
codex --profile ru-chatgpt-codex
```

## Локальные задачи Codex App

Для локальных сценариев Codex App держите конфигурацию провайдера в `~/.codex/config.toml`.
Положите токен в `~/.codex/.env`, если процесс приложения не наследует окружение shell:

```env
RU_LLM_PROXY_TOKEN=sk-...
```

Перезапустите Codex App после изменения `~/.codex/.env`.

## Проверка

Проверка ChatGPT-профиля без записи файлов:

```bash
codex exec --profile ru-chatgpt-codex \
  --sandbox read-only --ephemeral --skip-git-repo-check \
  'Ответь только идентификатором: KdirService'
```

Ожидаемый клиентский ответ: `KdirService`. Для доказательства полного цикла
используйте `tests/e2e/mock_chatgpt_codex_upstream.py`: его `/capture` показывает,
что провайдер получил `CompanynameabcService`, но не получил `KdirService` и
`x-litellm-api-key`.

Общая проверка client-auth для режима с серверными API-ключами:

```bash
make client-auth-smoke
```

Часть, относящаяся к Codex, проверяет `POST /v1/responses` только когда настроен
`OPENAI_API_KEY`, а `RESPONSES_MODEL` указывает на проверенное на живом сервисе имя модели прокси:

```bash
RESPONSES_MODEL=<validated-responses-alias> make client-auth-smoke
```

## Ссылки

- Codex authentication: https://developers.openai.com/codex/auth
- Codex configuration: https://developers.openai.com/codex/config/
- Codex config reference: https://developers.openai.com/codex/config-reference/
- LiteLLM forward client headers: https://docs.litellm.ai/docs/proxy/forward_client_headers
- LiteLLM virtual keys: https://docs.litellm.ai/docs/proxy/virtual_keys
