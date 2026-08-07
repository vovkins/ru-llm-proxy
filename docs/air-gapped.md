# Запуск в изолированной корпоративной среде

Эта инструкция относится к ветке `air-gapped-environment`. Ветка предназначена
для стенда, где внешние соединения проходят через корпоративный прокси, TLS
переподписывается корпоративным удостоверяющим центром, а пакеты Python берутся
из внутреннего зеркала. Изменения не предназначены для слияния в `main`.

Проверка TLS остается включенной. Проект добавляет согласованный корпоративный
сертификат в хранилища доверия, а не использует `SSL_VERIFY=False`.

## Что подготовить

- адрес корпоративного HTTP-прокси;
- цепочку корневых и промежуточных сертификатов прокси в формате PEM;
- адрес внутреннего индекса Python-пакетов;
- отдельный индекс с точным CPU-пакетом `torch==2.13.0+cpu`;
- доступ через прокси к закрепленной модели Hugging Face и образам Docker;
- два ключа Z.AI и секреты локального запуска.

Прокси для загрузки базовых образов настраивается в Docker daemon. Переменные
контейнеров и параметры сборки не влияют на `docker pull`.

## Настройка

1. Создайте `.env` и заполните секреты:

   ```bash
   cp .env.example .env
   make setup
   ```

2. Поместите корпоративную цепочку в `certs/proxy-ca.crt`. Требования к файлу
   описаны в [certs/README.md](../certs/README.md).

3. Задайте сетевые параметры в `.env`:

   ```dotenv
   HTTP_PROXY=http://proxy.corp.example:3128
   HTTPS_PROXY=http://proxy.corp.example:3128
   NO_PROXY=localhost,127.0.0.1,db,redis,presidio-analyzer,litellm,nginx

   PIP_INDEX_URL=https://pypi.corp.example/simple
   PIP_TRUSTED_HOST=
   PIP_PROXY=
   PYTORCH_INDEX_URL=https://pypi.corp.example/pytorch-cpu/simple
   NER_MODEL_PROXY=http://model-proxy.corp.example:3128
   ```

   Добавьте в `NO_PROXY` внутренние домены и подсети стенда. Не удаляйте имена
   сервисов проекта: иначе обращения к Analyzer, Redis и PostgreSQL уйдут во
   внешний прокси.

   `PIP_TRUSTED_HOST` отключает проверку TLS для указанного узла. Оставляйте его
   пустым, если внутреннее зеркало использует сертификат из согласованной
   цепочки. Не помещайте логины и пароли в URL параметров сборки.

## Сборка и запуск

```bash
make build
make up
make health
```

Во время сборки:

- `apt`, `pip` и spaCy используют общий прокси;
- обычные зависимости берутся из `PIP_INDEX_URL`;
- CPU-only PyTorch берется из `PYTORCH_INDEX_URL`;
- разрешенные файлы модели скачиваются через `NER_MODEL_PROXY` либо общий
  `HTTPS_PROXY` и проверяются по размеру и SHA-256;
- корпоративный сертификат добавляется в образы LiteLLM, Analyzer и тестов.

Во время работы Analyzer использует только модель внутри образа и не обращается
к Hugging Face. Внешний сетевой доступ требуется LiteLLM для вызова провайдеров.

## Проверка

Nginx публикует единую точку входа на `NGINX_HTTP_PORT`, по умолчанию `80`:

```bash
curl -fsS http://localhost/health/liveliness

curl -fsS http://localhost/v1/chat/completions \
  -H "Authorization: Bearer $LITELLM_MASTER_KEY" \
  -H "Content-Type: application/json" \
  -d '{"model":"glm-5.2","messages":[{"role":"user","content":"ping"}],"max_tokens":8}'
```

После запуска выполните проверки защитного слоя:

```bash
make guardrails-smoke
make routing-smoke
```

Порт LiteLLM `4000` и порт Analyzer `5001` в этой ветке оставлены опубликованными
для диагностики. На стенде ограничьте их межсетевым экраном и предоставляйте
клиентам только nginx.

## Диагностика

| Ошибка | Что проверить |
| --- | --- |
| LiteLLM не доверяет сертификату провайдера | Наличие `certs/proxy-ca.crt`, затем полная пересборка образа LiteLLM |
| `pip` не находит пакет | `PIP_INDEX_URL`, доступ через прокси и наличие пакета во внутреннем зеркале |
| Не найден `torch==2.13.0+cpu` | Отдельный `PYTORCH_INDEX_URL` и точная версия CPU-пакета |
| Не скачивается NER-модель | `NER_MODEL_PROXY` или общий `HTTPS_PROXY`, доступ к `huggingface.co` |
| Внутренние запросы уходят в прокси | Имена сервисов и внутренние домены в `NO_PROXY` |
| Не загружается базовый образ | Настройки прокси Docker daemon вне проекта |
