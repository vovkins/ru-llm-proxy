# Claude Code

Этот гайд описывает Claude Code как целевой клиентский шлюз для ru-llm-proxy. Конфигурация репозитория по умолчанию проверяет только базовый путь авторизации Anthropic Messages; полноценный шлюз Claude Code пока не считается полностью проверенным.

Что сейчас валидируется:

- базовые клиенты Anthropic Messages, которые умеют задавать `ANTHROPIC_BASE_URL` и отправлять пользовательский ключ LiteLLM.
- настройка Claude Code CLI как целевой путь, для которого еще нужна отдельная проверка шлюза.

Пока не полностью проверено:

- полный контракт шлюза Claude Code `POST /v1/messages?beta=true`;
- поведение потоковых SSE-ответов через прокси;
- пересылка `anthropic-version` и `anthropic-beta` в конфигурации по умолчанию;
- дополнительные маршруты подсчёта токенов и обнаружения моделей.

Что не входит в этот гайд:

- размещение общих файлов входа Claude.ai или Claude Code на прокси как учётных данных внешнего провайдера;
- Claude Desktop или облачные функции, которые не используют те же настройки шлюза.

## Ключи доступа

Используйте пользовательский ключ LiteLLM как клиентский токен:

```bash
export RU_LLM_PROXY_TOKEN="sk-..."
```

Настоящий `ANTHROPIC_API_KEY` в режиме, где провайдера оплачивает и вызывает прокси, остается только на хосте прокси. Не кладите `ANTHROPIC_API_KEY` или `LITELLM_MASTER_KEY` в локальную конфигурацию Claude Code.

Все переменные окружения из этого гайда описаны в [../configuration.md](../configuration.md).

Обычные пользовательские ключи создавайте в административном интерфейсе LiteLLM. Вспомогательный скрипт командной строки нужен только как дополнительный путь для DevOps, CI и первичной настройки с хоста прокси:

```bash
scripts/create_virtual_key.sh --alias claude-code-local --models anthropic,standard --duration 30d
```

## Настройка с оплатой провайдера на стороне прокси

Используйте этот режим, когда прокси должен вызывать провайдера через серверный `ANTHROPIC_API_KEY`. Направьте Claude Code на прокси для базового пути Anthropic Messages:

```bash
export ANTHROPIC_BASE_URL="http://localhost:4000"
export ANTHROPIC_AUTH_TOKEN="$RU_LLM_PROXY_TOKEN"
export ANTHROPIC_MODEL="anthropic-example-standard"
```

Модель более высокого класса используйте только если выданный ключ разрешает такое публичное имя:

```bash
export ANTHROPIC_MODEL="anthropic-example-premium"
```

Имена моделей Anthropic сейчас являются примерами дополнительных провайдеров. Добавляйте их из `examples/litellm-config.optional-providers.yaml` только после проверки исходных идентификаторов моделей на целевом образе LiteLLM и конкретной подписке.

Claude Code отправляет пользовательский ключ в прокси. Затем прокси использует серверный
`ANTHROPIC_API_KEY` для вызова Anthropic. Считайте это проверяемым целевым сценарием до тех пор,
пока отдельная быстрая проверка шлюза Claude Code не покроет `?beta=true`, потоковую передачу SSE
и пересылку заголовков `anthropic-*`.

## Сквозная передача клиентского Anthropic API-ключа

Используйте этот режим, когда клиент приносит собственный Anthropic API-ключ, но
по-прежнему аутентифицируется в прокси через пользовательский ключ LiteLLM. Это отдельный
режим запуска; он не включён в конфигурации репозитория по умолчанию.

Сквозная передача ключа через LiteLLM рассчитана на провайдерские заголовки вроде
`x-api-key` и `api-key`. Токен прокси остаётся отдельным:

```bash
export ANTHROPIC_BASE_URL="http://localhost:4000"

curl "$ANTHROPIC_BASE_URL/v1/messages" \
  -H "Content-Type: application/json" \
  -H "x-litellm-api-key: Bearer $RU_LLM_PROXY_TOKEN" \
  -H "x-api-key: $ANTHROPIC_BYOK_API_KEY" \
  -d '{"model":"anthropic-example-standard","max_tokens":32,"messages":[{"role":"user","content":"Reply with ok."}]}'
```

## Сквозная передача подписки Claude

Используйте этот режим, когда Claude Code должен работать с локальной Claude
подпиской пользователя, но всё ещё идти через прокси для защитных слоёв, учёта и
контроля доступа на прокси.

Это отдельный сценарий для проверки, а не готовый промышленный режим по умолчанию. Авторизация подписки Claude
обычно опирается на провайдерский `Authorization`; стандартный путь LiteLLM не
считается способным гарантированно пересылать этот заголовок провайдеру. Конфигурация
репозитория по умолчанию не включает пересылку заголовков.

Не задавайте `ANTHROPIC_AUTH_TOKEN` равным ключу прокси в этом режиме. Вместо этого
передавайте ключ прокси через `ANTHROPIC_CUSTOM_HEADERS`, а Claude Code пусть
управляет авторизацией аккаунта Claude локально:

```bash
export ANTHROPIC_BASE_URL="http://localhost:4000"
export ANTHROPIC_MODEL="anthropic-example-standard"
export ANTHROPIC_CUSTOM_HEADERS="x-litellm-api-key: Bearer $RU_LLM_PROXY_TOKEN"

claude
```

Если Claude Code попросит войти, выберите поток входа через подписку аккаунта Claude. Прокси
аутентифицирует запрос через `x-litellm-api-key`; Claude Code отдельно отправляет
провайдерскую OAuth-авторизацию Claude. Если проверка на живом сервисе покажет, что обычный маршрут LiteLLM
`/v1/messages` удаляет нужный провайдерский заголовок `Authorization`, этому режиму
нужен сквозной маршрут, боковой контейнер или отдельный адаптер перед промышленным использованием.

Не копируйте общий файл учётных данных Claude на прокси для всех пользователей. Этот
режим нужно проверить на живом сервисе и зафиксированном образе LiteLLM перед промышленным внедрением.

## Динамическая выдача токена

Claude Code поддерживает `apiKeyHelper` для сценариев командной строки. Используйте его,
когда локальная команда должна получать или ротировать токен прокси:

```json
{
  "apiKeyHelper": "/absolute/path/to/print-ru-llm-proxy-token.sh"
}
```

Помощник должен печатать пользовательский ключ LiteLLM, а не внешний Anthropic API-ключ.

## Поверхность шлюза Anthropic Messages

Для полностью проверенного шлюза Claude Code через `ANTHROPIC_BASE_URL`
основной путь вывода модели выглядит так:

```text
POST /v1/messages?beta=true
```

Шлюз должен сохранять семантику Anthropic Messages, ретранслировать потоковые
SSE-ответы и передавать `anthropic-version` и `anthropic-beta` без изменений.
LiteLLM поддерживает маршрут `/v1/messages`, совместимый с Anthropic, но в этом
репозитории ещё нет отдельной быстрой проверки шлюза Claude Code для `?beta=true`,
потокового пути и пересылки заголовков в конфигурации по умолчанию.

Дополнительные маршруты шлюза Claude Code:

```text
POST /v1/messages/count_tokens
GET /v1/models?limit=1000
```

`POST /v1/messages/count_tokens` необязателен; при его отсутствии Claude Code
может вернуться к локальной оценке контекста. `GET /v1/models?limit=1000` —
необязательный маршрут обнаружения моделей, когда эта функция включена на шлюзе.

`make client-auth-smoke` — базовая быстрая проверка авторизации Anthropic Messages. Она отправляет
обычный `POST /v1/messages` без потоковой передачи с пользовательским ключом LiteLLM только когда настроен
`ANTHROPIC_API_KEY`, а `MESSAGES_MODEL` указывает на проверенное на живом сервисе имя модели прокси:

```bash
MESSAGES_MODEL=<validated-messages-alias> make client-auth-smoke
```

Он не проверяет параметр `?beta=true` Claude Code, пересылку заголовков `anthropic-*`,
потоковую передачу SSE, подсчёт токенов или обнаружение моделей. Добавьте отдельную
быструю проверку шлюза Claude Code перед тем, как считать эту поверхность полностью проверенной.

## Ссылки

- Шлюз LLM Claude Code: https://docs.anthropic.com/en/docs/claude-code/llm-gateway
- Claude Code environment variables: https://docs.anthropic.com/en/docs/claude-code/settings#environment-variables
- Подписка Claude Code Max в LiteLLM: https://docs.litellm.ai/docs/tutorials/claude_code_max_subscription
- LiteLLM forward client headers: https://docs.litellm.ai/docs/proxy/forward_client_headers
- LiteLLM Anthropic Messages API: https://docs.litellm.ai/docs/anthropic_unified/
