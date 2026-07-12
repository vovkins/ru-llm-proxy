# Административный доступ и роли операторов

Документ описывает production-модель административного и операторского доступа
для `ru-llm-proxy`. Он намеренно отделён от клиентских гайдов: клиентские
токены, ключи upstream-провайдеров и административные учётные данные относятся
к разным trust domains. Сгруппированный справочник переменных окружения:
[configuration.md](configuration.md).

## Границы учётных данных

| Учётные данные | Владелец | Назначение | Правило для production |
| --- | --- | --- | --- |
| LiteLLM virtual key / `RU_LLM_PROXY_TOKEN` | Пользователи, приложения, CI jobs | Клиентские вызовы `/v1/*` proxy API | Выдавайте отдельно на пользователя, команду или приложение с лимитами моделей и бюджета. Никогда не используйте `LITELLM_MASTER_KEY` как клиентский токен. |
| Upstream provider keys | Только runtime прокси | Вызовы Z.AI/OpenAI/Anthropic из server-funded proxy | Храните в secret manager или защищённом `.env`; не кладите в клиентские конфиги. |
| `LITELLM_MASTER_KEY` | Только break-glass/admin automation | Привилегированный LiteLLM admin API key | Считайте root-equivalent секретом. Храните в secret manager, ограничивайте доступ CI, ротируйте при уходе оператора или подозрении на утечку. |
| `UI_USERNAME` / `UI_PASSWORD` | Local/dev admin или защищённая operator boundary | Вход в LiteLLM Admin UI в дефолтном OSS-профиле | Не публикуйте напрямую в интернет. Закрывайте `/ui` через SSO/VPN/IP allowlist/mTLS или отключайте UI. |
| OIDC/JWT | Идентичность оператора или клиента из IdP | SSO-backed доступ к proxy, где это поддержано | Требует IdP/JWKS и deployment-specific поддержки LiteLLM. Не заменяет provider keys. |

## Правила публикации в production

Дефолтный Docker Compose profile удобен для локальной эксплуатации. Сам по себе
он не является полноценной production admin boundary.

В production разделяйте публикацию клиентского и административного трафика:

- `/v1/*` можно открывать одобренным клиентам только при защите через LiteLLM
  virtual keys или валидированный JWT/OIDC deployment.
- `/ui`, `/key/*`, `/user/*`, `/team/*`, `/model/*`, `/spend/*`, and other admin
  routes нельзя публиковать как обычные public routes, защищённые только общими
  `UI_USERNAME` / `UI_PASSWORD` or `LITELLM_MASTER_KEY`.
- Размещайте Admin UI и admin API routes как минимум за одной operator boundary:
  corporate SSO/OIDC/SAML, VPN, IP allowlist, mTLS, zero-trust proxy или
  эквивалентный private network control.
- Для API-only production deployments, где Admin UI не нужен, предпочитайте
  `DISABLE_ADMIN_UI=True`.
- Не допускайте попадания `LITELLM_MASTER_KEY` на рабочие станции, в клиентские
  конфиги, скриншоты, тикеты и обычный вывод smoke-тестов.

Пример API-only hardening:

```env
DISABLE_ADMIN_UI=True
```

После изменения значения пересоздайте контейнер LiteLLM, чтобы окружение
перечиталось:

```bash
docker compose up -d --force-recreate --no-deps litellm
```

## OSS-база и Enterprise RBAC

Дефолтный runnable profile репозитория использует LiteLLM OSS-style primitives:

- `LITELLM_MASTER_KEY` для привилегированной admin API automation;
- `UI_USERNAME` / `UI_PASSWORD` для входа в Admin UI;
- LiteLLM virtual keys для пользователей, команд, приложений и CI jobs;
- PostgreSQL persistence для LiteLLM users, keys, budgets и spend.

Такой baseline приемлем для локальной разработки и контролируемых внутренних
сред только тогда, когда внешняя network/SSO boundary ограничивает operator
access.

LiteLLM documents internal user roles such as `proxy_admin`,
`proxy_admin_viewer`, and `internal_user`, and org/team-scoped roles such as
`org_admin` and `team_admin`. LiteLLM также описывает org/team-specific roles и
часть team member permissions как premium/enterprise features. Если эти функции
доступны в целевом deployment, используйте их для least privilege внутри
LiteLLM. Если они недоступны, обеспечивайте least privilege вне LiteLLM через
SSO groups, reverse-proxy routing rules, private admin networks, отдельный
break-glass secret access и аудируемые runbooks.

Ссылки:

- LiteLLM virtual keys: https://docs.litellm.ai/docs/proxy/virtual_keys
- LiteLLM Admin UI: https://docs.litellm.ai/docs/proxy/ui
- LiteLLM RBAC: https://docs.litellm.ai/docs/proxy/access_control
- LiteLLM audit logs: https://docs.litellm.ai/docs/proxy/multiple_admins
- LiteLLM public/private routes: https://docs.litellm.ai/docs/proxy/public_routes

## Модель операторских ролей

Используйте следующую production role model даже там, где сам LiteLLM не может
обеспечить каждое разделение в дефолтном OSS-профиле.

| Роль | Типичный доступ | Разрешённые операции | Примечания |
| --- | --- | --- | --- |
| Platform admin | SSO admin group, break-glass доступ к `LITELLM_MASTER_KEY` | Настраивать proxy, ротировать secrets, управлять models и всеми users/teams/keys | Держите группу небольшой. Ручное использование master key должно быть исключением. |
| Key operator | Admin UI или controlled admin API workflow | Создавать, отзывать, блокировать, разблокировать и перевыпускать virtual keys | Не должен владеть upstream provider keys. |
| Budget/model-access operator | Admin UI или controlled admin API workflow | Менять budgets, rate limits, allowed models и team access | Изменения должны быть аудируемыми и привязанными к ticket/change request. |
| Auditor/read-only | Read-only Admin UI role, где доступно; иначе dashboards/logs | Смотреть usage, spend, key inventory и admin actions | В OSS-only deployments давайте read-only evidence через dashboards/log exports, а не через общие admin credentials. |
| Break-glass admin | Sealed secret или privileged secret-manager role | Emergency disablement, master-key rotation, admin lockout recovery | Требует incident ticket, по возможности two-person approval и post-incident rotation. |
| CI/service account | Protected CI secret, ограниченный deployment repo | Bootstrap short-lived smoke/test keys и deployment checks | Никогда не печатайте `LITELLM_MASTER_KEY`; для test calls предпочитайте short-lived generated virtual keys. |

## Административные операции

Считайте привилегированными такие операции:

- создание, изменение, перевыпуск, блокировка, разблокировка или удаление virtual keys;
- создание, изменение или удаление LiteLLM users, teams, service accounts или internal users;
- изменение budgets, rate limits, allowed models, aliases или routing behavior;
- добавление и удаление upstream models или provider credentials;
- ротация `LITELLM_MASTER_KEY`, `UI_PASSWORD`, provider keys или database secrets;
- включение/отключение Admin UI или изменение SSO/reverse-proxy policy;
- break-glass access или emergency disablement.

Client request audit и admin action audit — разные вещи. Guardrail logs вроде
`gateway_guardrail_audit` доказывают request-time security decisions, но не
показывают, кто изменил budget пользователя или создал admin key.

## Ротация и экстренный отзыв

### `LITELLM_MASTER_KEY`

Используйте новое high-entropy значение `sk-ru-...`, сгенерированное через
`make setup` или secret manager. Сохраните его в production secret store,
обновите runtime secret и пересоздайте контейнер LiteLLM. Проверьте:

- `make health` проходит;
- Admin UI/API доступен только из operator network;
- обычные client calls с virtual keys продолжают работать;
- старый master key больше не принимается.

Ротация master key не равна отзыву user virtual keys, которые хранятся в
PostgreSQL. Если скомпрометирован токен пользователя или приложения,
заблокируйте или удалите соответствующий virtual key через Admin UI или admin API.

### `UI_PASSWORD`

Обновите `UI_PASSWORD` в secret store или `.env`, пересоздайте контейнер LiteLLM,
проверьте вход и зафиксируйте изменение. Ротируйте пароль после ухода оператора,
подозрения на утечку или использования общего пароля в non-production.

### Virtual keys

При обычной компрометации пользователя или приложения:

1. Заблокируйте или удалите затронутый virtual key.
2. Выпустите replacement key с минимально нужными models, budget и TTL.
3. Проверьте usage/spend вокруг окна компрометации.
4. Зафиксируйте incident или support ticket id.

### Break-glass доступ

Break-glass доступ должен быть редким и проверяемым:

- по возможности требуйте incident/change record до доступа;
- ограничивайте доступ небольшой operator group;
- фиксируйте точное выполненное действие;
- ротируйте любой secret, раскрытый во время события;
- закрывайте событие post-incident review.

## Требования к аудиту администрирования

Production deployments должны фиксировать admin activity из самого сильного
доступного источника:

- LiteLLM audit logs, где они доступны;
- reverse proxy / SSO access logs для `/ui` и admin API routes;
- CI job logs для bootstrap automation, с редактированием секретов;
- GitOps или ticket records для изменений config/model/provider;
- database backup/change records для LiteLLM key/user state.

Минимальные поля admin audit evidence:

- actor identity или service account;
- source boundary, например SSO group, CI job, VPN или break-glass role;
- action type и target, например `key.block`, `budget.update`, `model.add`;
- timestamp;
- ticket/change/incident id, если он есть;
- result: success, failure, rollback.

Не логируйте raw provider keys, raw virtual keys, `LITELLM_MASTER_KEY`,
`UI_PASSWORD` или полные Authorization headers.

## Production checklist

Перед публикацией production instance:

- `LITELLM_MASTER_KEY`, `LITELLM_SALT_KEY`, provider keys, `UI_PASSWORD`, and
  PostgreSQL password являются сгенерированными секретами, а не placeholders.
- Пользователи и клиентские инструменты получают virtual keys или validated
  JWT/OIDC tokens, а не master key.
- Admin UI либо отключён через `DISABLE_ADMIN_UI=True`, либо защищён
  operator-only SSO/VPN/IP allowlist/mTLS boundary.
- Admin API routes не доступны публично без той же operator boundary.
- Operator roles и break-glass ownership задокументированы для deployment.
- Изменения key/budget/model/admin имеют audit source.
- Ротация master key и UI credentials проверена в staging.
- CI/service accounts могут создавать только short-lived test keys и не могут
  печатать admin secrets.
- Backups покрывают PostgreSQL, потому что LiteLLM users, virtual keys, budgets
  и spend хранятся там.
