# Развёртывание в air-gapped среде (http_proxy + DLP + корпоративный CA)

Эта инструкция описывает запуск `ru-llm-proxy` в изолированной корпоративной сети, где
выход в интернет разрешён только через перехватывающий (re-signing) HTTP-прокси / DLP, а
пакеты Python ставятся из внутреннего PyPI-зеркала.

Ключевой принцип: проект **доверяет корневому CA корпоративного прокси**, а не отключает
проверку TLS (`SSL_VERIFY=False`). Прокси переподписывает исходящий HTTPS, поэтому его CA
добавляется в trust store образов и в `certifi`.

Слой air-gapped-настроек **опциональный и деградирует мягко**: если прокси и CA не заданы,
стек собирается и работает как при прямом выходе в интернет.

## Что нужно заранее

- Корневой сертификат корпоративного прокси (полная цепочка root + промежуточные), в формате PEM.
- Адрес(а) HTTP-прокси (для общего трафика и, при необходимости, отдельный для pip).
- URL внутреннего PyPI-зеркала (Artifactory/Nexus и т.п.) и его хост.
- Прокси, настроенный **на самом демоне Docker**, чтобы `docker pull` мог скачать базовые
  образы (`postgres`, `redis`, `nginx`, официальный образ LiteLLM). Это конфигурируется вне
  этого репозитория — см. `~/.docker/config.json` или `systemd` drop-in для `dockerd`.

## Шаги

### 1. Положить корпоративный CA

Скопируйте цепочку сертификатов в:

```
certs/proxy-ca.crt
```

Файл в `.gitignore` (`certs/*.crt`) — его нельзя коммитить. Подробности: [../certs/README.md](../certs/README.md).
Без этого файла образы всё равно соберутся (доверие через стоковый `certifi`), но исходящий
TLS через перехватывающий прокси упадёт с `Connection error`.

### 2. Подготовить `.env`

Docker Compose и цели `make` читают файл **`.env`** (с точкой). Если ваш файл называется `env`,
переименуйте его:

```
mv env .env
```

Затем догенерируйте секреты (скрипт не трогает уже заполненные значения):

```
make setup
```

Заполните в `.env` как минимум:

- прокси и зеркало:
  ```
  HTTP_PROXY=http://proxy-host:3128
  HTTPS_PROXY=http://proxy-host:3128
  NO_PROXY=db,redis,presidio-analyzer,litellm,localhost,127.0.0.1,<docker-subnet>,<internal-domains>
  PIP_INDEX_URL=https://mirror-host/artifactory/api/pypi/<repo>/simple
  PIP_TRUSTED_HOST=mirror-host
  PIP_PROXY=http://pip-proxy-host:3128   # если pip ходит через отдельный прокси
  ```
- ключи провайдера по умолчанию `ZAI_API_KEY` и `ZAI_API_KEY_2`;
- секреты, которые сгенерировал `make setup` (`LITELLM_MASTER_KEY`, `LITELLM_SALT_KEY`,
  `POSTGRES_PASSWORD`, `UI_PASSWORD`, `LITELLM_DB_URL`).

Важно про `NO_PROXY`:

- всегда включайте имена внутренних сервисов (`db,redis,presidio-analyzer,litellm`) и подсеть
  Docker-сети, иначе внутренний трафик пойдёт в прокси;
- хост PyPI-зеркала либо достаётся через отдельный `PIP_PROXY`, либо добавьте его в `NO_PROXY`,
  чтобы pip ходил к нему напрямую.

Значения политик DLP остаются в безопасных значениях по умолчанию (`PII_GUARDRAIL_FAILURE_MODE=fail_closed`,
`PRE_EGRESS_POLICY_MODE=block`, `FINAL_PAYLOAD_LEAK_CHECK_MODE=block`); переопределяйте их
только осознанно. Полный список переменных — в [configuration.md](configuration.md).

### 3. Собрать образы

```
make build
```

При сборке `presidio-analyzer` прокси и настройки зеркала передаются как build-аргументы
(`docker build` не наследует `environment:` сервиса). Через них идут `apt`, установка пакетов
из внутреннего зеркала, загрузка spaCy `ru_core_news_sm` и архива DeepPavlov `ner_rus_bert`
(`DEEPPAVLOV_NER_MODEL_URL`). Корпоративный CA уже установлен в trust store к моменту этих шагов.

### 4. Запустить

```
make up
make health
```

Первый запрос к `presidio-analyzer` докачивает веса rubert из HuggingFace (через прокси, с
проверкой по корпоративному CA) и кэширует их в `./cache/presidio/huggingface`, поэтому
повторные пересоздания контейнера стартуют быстро.

### 5. Проверить

```
# через единую точку входа nginx (порт NGINX_HTTP_PORT, по умолчанию 80)
curl -s http://localhost/health/liveliness

# запрос к модели (исходящий TLS к провайдеру идёт через прокси)
curl -s http://localhost/v1/chat/completions \
  -H "Authorization: Bearer $LITELLM_MASTER_KEY" \
  -H "Content-Type: application/json" \
  -d '{"model":"glm-5.2","messages":[{"role":"user","content":"ping"}],"max_tokens":8}'
```

Успешный ответ без `Connection error` подтверждает доверие корпоративному CA на исходящем TLS.
Маскирование персональных данных проверяется через `make guardrails-smoke`.

## Как это устроено

- **Единая точка входа `nginx`.** Слушает `NGINX_HTTP_PORT` (по умолчанию `80`) и проксирует на
  `litellm:4000` с поддержкой больших тел и потоковой передачи (SSE). См. `nginx/conf.d/default.conf`.
- **Доверие CA у `litellm`.** `litellm/Dockerfile` дописывает `certs/proxy-ca.crt` в `certifi`
  внутри venv (`/app/.venv`), который LiteLLM использует для исходящих вызовов. Дополнительно
  `DISABLE_AIOHTTP_TRANSPORT=True` переключает LiteLLM на httpx-транспорт (aiohttp-транспорт
  игнорирует кастомный CA), а `AIOHTTP_TRUST_ENV=True` заставляет клиент читать `*_PROXY`.
- **Доверие CA у `presidio-analyzer`.** `presidio/Dockerfile` ставит CA в системный trust store
  (`update-ca-certificates`) и дописывает его в `certifi`, поэтому apt/pip/urllib/requests/httpx/
  huggingface доверяют прокси и при сборке, и в рантайме.
- **Прокси в двух регистрах.** `HTTP_PROXY`/`HTTPS_PROXY`/`NO_PROXY` дублируются строчными
  (`http_proxy`/…), чтобы их видели все HTTP-клиенты.
- **Внутреннее PyPI-зеркало.** `PIP_INDEX_URL`/`PIP_TRUSTED_HOST`/`PIP_PROXY` пишутся в
  `/etc/pip.conf` при сборке, потому что корпоративный прокси обычно блокирует CONNECT к
  публичному pypi.org.
- **Мягкая деградация.** Пустые `HTTP_PROXY`/… означают прямой выход; отсутствие
  `certs/proxy-ca.crt` не ломает сборку (образы сохраняют стоковый `certifi`).

## Заметки по безопасности и эксплуатации

- В проде можно снять публикацию порта `4000` у `litellm` и пускать клиентов только через
  `nginx:80`; порт `4000` сейчас опубликован для диагностических целей Makefile (`make health`,
  `make metrics` и т.п.).
- Данные PostgreSQL монтируются как host bind-mount `./pgdata` (в `.gitignore`). Если на хосте
  возникают проблемы с правами/uid, переключитесь на именованный том.
- Границы исходящих соединений и шаблоны Kubernetes NetworkPolicy/Cilium — в
  [egress-controls.md](egress-controls.md).

## Диагностика

| Симптом | Причина / что проверить |
| --- | --- |
| `Connection error` при вызове провайдера из `litellm` | CA не установлен: положите `certs/proxy-ca.crt` и пересоберите `litellm`. Убедитесь, что `DISABLE_AIOHTTP_TRANSPORT=True`. |
| `pip` не может скачать пакеты при сборке | Не задан `PIP_INDEX_URL` (публичный pypi.org недоступен через прокси) либо не передан `PIP_PROXY`/`HTTP_PROXY` в build-аргументы. |
| `apt` виснет/падает при сборке | Прокси не передан build-аргументом или блокирует HTTP — Dockerfile переключает apt на HTTPS; проверьте доступность прокси на 443. |
| Внутренний трафик уходит в прокси | В `NO_PROXY` нет имён сервисов/подсети Docker. |
| Базовый образ не тянется (`docker pull`) | Прокси не настроен на демоне Docker (вне этого репозитория). |
