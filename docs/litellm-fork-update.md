# Обновление временного ответвления LiteLLM

Этот регламент применяется, пока официальный LiteLLM не поддерживает несколько
ChatGPT/Codex OAuth-профилей через серверный параметр модели. Ответвление должно
содержать только эту возможность, её проверки и процесс публикации образа.

## Текущая версия

| Параметр | Значение |
| --- | --- |
| Исходный выпуск | `v1.97.0` |
| Полный SHA исходного выпуска | `ef84494d52c6708e4e9f4a54ce551a265995ad8f` |
| Ветка ответвления | `ru-llm-proxy/multi-oauth-v1.97.0` |
| Версия образа | `ghcr.io/vovkins/litellm:1.97.0-multi-oauth.10` |
| Ссылка для развёртывания | `ghcr.io/vovkins/litellm@sha256:e834430826222fd126f0e8251564a327add97ac7d1fef027057ce7adeffac9bb` |

Поддержку нескольких OAuth-файлов и публикацию образа добавляют коммиты
`7eb8057161`, `271dc023c6`, `cabdc3c36b`, `9ae8528b79`, `672d970890` и
`599f36dc95`. Общую для всех моделей привязку виртуального ключа к подписке
добавляют `bbf974695e`, `c90ec7eb60`, `75531a45aa`, `2b70bf6715` и
`038ad1a9ab`. Обработка отказов, восстановление, метрики и сквозной эмулятор
добавлены последующими коммитами до `7a28386814` включительно. Коммит
`6171effd0e` исключает неявный вход через общий OAuth-профиль, а `c34ba73155` и
`9871f3ba4c` ограничивают объём исходного кода, загружаемого при выпуске образа.
Коммит `c6c358f863` запрещает интерактивный вход в серверном процессе, отделяет
определение провайдера от чтения OAuth-файла и корректно обрабатывает завершённый
ответ без выходных элементов. Коммит `337f55e448` сохраняет состояние внешнего
запроса при внутреннем преобразовании Responses API, поддерживает строковый
`input` и возвращает обычный JSON, если клиент не запрашивал потоковый ответ.
Несвязанный коммит `69bf07a7c6` из исходного PR не переносится.

## Проверка официального выпуска

Перед созданием новой версии сначала проверьте последний стабильный выпуск:

```bash
cd /path/to/litellm
latest_tag="$(gh api repos/BerriAI/litellm/releases/latest --jq .tag_name)"
git fetch upstream --tags --prune
git show "${latest_tag}:litellm/llms/chatgpt/authenticator.py"
git grep -n "chatgpt_auth_file" "$latest_tag" -- ':!*.lock' || true
gh pr view 33680 --repo BerriAI/litellm
```

Одного совпадения по имени параметра недостаточно. Штатная реализация должна:

- принимать `chatgpt_auth_file` только из серверной конфигурации модели;
- поддерживать этот параметр для Chat Completions и Responses API;
- создавать отдельный аутентификатор для каждого нормализованного пути;
- запрещать клиенту передавать путь в теле запроса;
- иметь модульные проверки перечисленного поведения.

Если всё это есть в стабильном выпуске, не переносите наши коммиты. Подготовьте в
`ru-llm-proxy` переход на официальный образ по digest и выполните проверки из
раздела ниже. Если поддержки нет, обновляйте тонкое ответвление.

## Подготовка новой версии

1. Создайте ветку непосредственно от стабильного тега, а не от предыдущего
   ответвления:

   ```bash
   version=1.98.0
   upstream_tag="v${version}"
   upstream_sha="$(git rev-parse "${upstream_tag}^{commit}")"
   git switch --create "ru-llm-proxy/multi-oauth-v${version}" "$upstream_sha"
   ```

2. Перенесите только одиннадцать зафиксированных коммитов:

   ```bash
   git cherry-pick \
     7eb8057161e851b998ba6e744e2a4b78dcacbba8 \
     271dc023c667ddc9c3e202d881adde11aab2b406 \
     cabdc3c36b24678aa43b89c9161ee5ff75cbe0f9 \
     9ae8528b790448b20905f1da33cc9b5752c3e1ec \
     672d9708904afc5262c3a5c24612e8e37992bc92 \
     599f36dc95c3d5230ddd146b017566aa0ff6950b \
     bbf974695e5bb0902b27738d5d01dfe4e53f3792 \
     c90ec7eb60575ed4fb5532d7df5e7e84e74b9292 \
     75531a45aa91c647c4de532901ce8970e50bc626 \
     2b70bf6715cebcefc184d0fd9ed229e332501a17 \
     038ad1a9abc637fe8aff8ebc04d74343886b2a86
   ```

3. Разрешайте конфликты по смыслу новой версии. Если часть функциональности уже
   появилась в LiteLLM, остановите перенос и повторно оцените возможность перейти
   на официальный образ.
4. В `.github/workflows/publish-multi-oauth-image.yml` обновите исходную версию,
   полный SHA, шаблон версии, версию по умолчанию и шаблон тега запуска.
5. Проверьте состав изменений:

   ```bash
   git diff --name-status "${upstream_sha}...HEAD"
   git diff --check "${upstream_sha}...HEAD"
   git log --reverse --oneline "${upstream_sha}..HEAD"
   ```

Допустимы только файлы реализации ChatGPT OAuth, передача параметра модели,
запрет клиентской подмены, соответствующие тесты и процесс публикации. Изменения
зависимостей, общей документации LiteLLM и несвязанных провайдеров требуют
отдельного обоснования.

## Проверки

До создания тега выполните модульные и статические проверки:

```bash
python -m pytest -q \
  tests/test_litellm/llms/chatgpt/test_chatgpt_authenticator.py \
  tests/test_litellm/llms/chatgpt/chat/test_chatgpt_chat_transformation.py \
  tests/test_litellm/llms/chatgpt/responses/test_chatgpt_responses_transformation.py \
  tests/test_litellm/proxy/auth/test_auth_utils.py \
  tests/test_litellm/router_utils/pre_call_checks/test_openai_subscription_affinity_check.py \
  tests/test_litellm/router_utils/test_openai_subscription_affinity.py

REDIS_HOST=127.0.0.1 REDIS_PORT=6379 python -m pytest -q \
  tests/test_litellm/router_utils/test_openai_subscription_affinity_redis.py

git diff --name-only -z "${upstream_sha}...HEAD" -- '*.py' \
  | xargs -0 python -m ruff check
git diff --name-only -z "${upstream_sha}...HEAD" -- '*.py' \
  | xargs -0 python -m compileall -q
actionlint .github/workflows/publish-multi-oauth-image.yml
```

После проверки отправьте ветку для просмотра. Неизменяемый тег создавайте только
после просмотра полного diff. Процесс GitHub Actions обязан собрать
`linux/amd64` и `linux/arm64`, выполнить быструю проверку каждого образа,
опубликовать SBOM и provenance, а затем подтвердить обе архитектуры итогового
манифеста.

Не переиспользуйте Git-теги и теги образов. Для каждого исправления увеличивайте
суффикс `multi-oauth.N`; в `ru-llm-proxy` фиксируйте образ только по digest.

## Проверка и откат

После смены digest в отдельном PR проекта `ru-llm-proxy` выполните:

```bash
make health
make guardrails-list
make guardrails-smoke
make test-final-leak-proxy
make routing-smoke
make monitor-smoke
```

Проверки OAuth-пула с двумя реальными профилями выполняются отдельно по сценарию
задачи #72. При регрессии верните предыдущий digest в
`docker-compose.openai-oauth.yml` и примените OAuth-режим повторно:

```bash
.venv/bin/python scripts/apply_openai_oauth_profiles.py
```

Сценарий проверит конфиг и секреты, скачает образ и пересоздаст только LiteLLM.
Для возврата к обычному GLM-режиму используйте базовый Compose без OAuth-файла:

```bash
docker compose up -d --force-recreate --no-deps --wait litellm
```

Предыдущий образ не удаляйте до завершения принятого срока наблюдения. Когда
официальный выпуск пройдёт те же проверки, переведите проект на его digest,
проверьте откат и только затем прекратите выпуск нашего ответвления.

## Проверка регламента

21 августа 2026 года исходной точкой ответвления оставался выпуск `v1.97.0`, а
BerriAI/litellm#33680 оставался открытым. Для выпуска
`1.97.0-multi-oauth.10` локально прошли 92 целевых теста обработчиков HTTP,
ChatGPT и Responses API. Контейнерные образы `linux/amd64` и `linux/arm64`
прошли отдельные быстрые проверки и объединены в один манифест. Для образа
опубликованы перечень компонентов (SBOM), сведения о происхождении и аттестация
сборки. Реальная проверка двух подписок подтвердила распределение четырёх новых
виртуальных ключей `2/2`, продление 24-часовой привязки, оба режима Chat
Completions и Responses API, сохранение привязок после перезапуска, метрики по
каждой подписке и отсутствие секретов в журнале.

Исходные материалы:

- [последний выпуск LiteLLM](https://github.com/BerriAI/litellm/releases/latest);
- [PR с несколькими OAuth-профилями](https://github.com/BerriAI/litellm/pull/33680);
- [успешная публикация образа](https://github.com/vovkins/litellm/actions/runs/32416859368).
