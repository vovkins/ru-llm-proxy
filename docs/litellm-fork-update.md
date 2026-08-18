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
| Версия образа | `ghcr.io/vovkins/litellm:1.97.0-multi-oauth.2` |
| Ссылка для развёртывания | `ghcr.io/vovkins/litellm@sha256:771cf8e9081b1d3cef7b85cb9066e23d17bbcfc19e9617971dda487ec3c18abd` |

Функциональные изменения находятся в коммитах `7eb8057161`, `271dc023c6` и
`cabdc3c36b`. Коммиты `9ae8528b79` и `672d970890` добавляют публикацию образа.
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

2. Перенесите только пять зафиксированных коммитов:

   ```bash
   git cherry-pick \
     7eb8057161e851b998ba6e744e2a4b78dcacbba8 \
     271dc023c667ddc9c3e202d881adde11aab2b406 \
     cabdc3c36b24678aa43b89c9161ee5ff75cbe0f9 \
     9ae8528b790448b20905f1da33cc9b5752c3e1ec \
     672d9708904afc5262c3a5c24612e8e37992bc92
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
  tests/test_litellm/proxy/auth/test_auth_utils.py

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
задачи #72. При регрессии верните предыдущий digest и пересоздайте только LiteLLM:

```bash
docker compose pull litellm
docker compose up -d --force-recreate --no-deps litellm
```

Предыдущий образ не удаляйте до завершения принятого срока наблюдения. Когда
официальный выпуск пройдёт те же проверки, переведите проект на его digest,
проверьте откат и только затем прекратите выпуск нашего ответвления.

## Проверка регламента

18 августа 2026 года последний стабильный выпуск `v1.97.0` всё ещё использовал
один глобальный OAuth-файл, а BerriAI/litellm#33680 оставался открытым. Пробный
перенос пяти коммитов на чистый `v1.97.0` прошёл без конфликтов. Итоговое дерево
`d08c8c291cca39f5d74913de4d5733c4c98f4190` совпало с опубликованной версией;
`git diff --check` и `actionlint` завершились успешно. Модульные проверки
функциональности (70 тестов) и контейнерные проверки обеих архитектур выполнены
при подготовке выпуска `1.97.0-multi-oauth.2`.

Исходные материалы:

- [последний выпуск LiteLLM](https://github.com/BerriAI/litellm/releases/latest);
- [PR с несколькими OAuth-профилями](https://github.com/BerriAI/litellm/pull/33680);
- [успешная публикация образа](https://github.com/vovkins/litellm/actions/runs/32162788952).
