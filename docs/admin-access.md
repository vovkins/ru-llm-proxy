# Admin Access and Operator RBAC

This document defines the production admin/operator access model for `ru-llm-proxy`.
It is intentionally separate from client setup docs: client credentials, upstream
provider credentials, and administrator credentials are different trust domains.

## Credential Boundaries

| Credential | Holder | Purpose | Production rule |
| --- | --- | --- | --- |
| LiteLLM virtual key / `RU_LLM_PROXY_TOKEN` | End users, apps, CI jobs | Calls `/v1/*` proxy APIs as a client | Issue per user/team/app with model and budget limits. Never use `LITELLM_MASTER_KEY` as a client token. |
| Upstream provider keys | Proxy runtime only | Calls Z.AI/OpenAI/Anthropic from the server-funded proxy | Store in secret manager or protected `.env`; do not place in client configs. |
| `LITELLM_MASTER_KEY` | Break-glass/admin automation only | Privileged LiteLLM admin API key | Treat as root-equivalent. Store in a secret manager, restrict CI access, rotate on operator departure or suspected exposure. |
| `UI_USERNAME` / `UI_PASSWORD` | Local/dev admin or protected operator boundary | LiteLLM Admin UI login in default OSS profile | Do not expose directly to the internet. Put `/ui` behind SSO/VPN/IP allowlist/mTLS or disable it. |
| OIDC/JWT | IdP-issued operator/client identity | SSO-backed proxy access where supported | Requires IdP/JWKS and deployment-specific LiteLLM support. It does not replace provider keys. |

## Production Exposure Rules

The default Docker Compose profile is convenient for local operations. It is not a
complete production admin boundary by itself.

In production, expose client traffic and admin traffic differently:

- `/v1/*` may be exposed to approved clients only when protected by LiteLLM
  virtual keys or a validated JWT/OIDC deployment.
- `/ui`, `/key/*`, `/user/*`, `/team/*`, `/model/*`, `/spend/*`, and other admin
  routes must not be exposed as plain public routes protected only by the shared
  `UI_USERNAME` / `UI_PASSWORD` or `LITELLM_MASTER_KEY`.
- Place Admin UI and admin API routes behind at least one operator boundary:
  corporate SSO/OIDC/SAML, VPN, IP allowlist, mTLS, zero-trust proxy, or an
  equivalent private network control.
- Prefer `DISABLE_ADMIN_UI=True` for API-only production deployments where the
  Admin UI is not required.
- Keep `LITELLM_MASTER_KEY` out of laptops, client config, screenshots, tickets,
  and normal smoke-test output.

Example API-only hardening:

```env
DISABLE_ADMIN_UI=True
```

After changing this value, recreate the LiteLLM container so the environment is
reloaded:

```bash
docker compose up -d --force-recreate --no-deps litellm
```

## OSS Baseline vs Enterprise RBAC

The repository's default runnable profile uses LiteLLM OSS-style primitives:

- `LITELLM_MASTER_KEY` for privileged admin API automation;
- `UI_USERNAME` / `UI_PASSWORD` for Admin UI login;
- LiteLLM virtual keys for users, teams, apps, and CI jobs;
- PostgreSQL persistence for LiteLLM users, keys, budgets, and spend.

This baseline is acceptable for local development and controlled internal
environments only when the surrounding network/SSO boundary restricts operator
access.

LiteLLM documents internal user roles such as `proxy_admin`,
`proxy_admin_viewer`, and `internal_user`, and org/team-scoped roles such as
`org_admin` and `team_admin`. LiteLLM also documents org/team-specific roles and
some team member permissions as premium/enterprise features. If those features
are available in the target deployment, use them to enforce least privilege
inside LiteLLM. If they are not available, enforce least privilege outside
LiteLLM with SSO groups, reverse-proxy routing rules, private admin networks,
separate break-glass secret access, and audited runbooks.

References:

- LiteLLM virtual keys: https://docs.litellm.ai/docs/proxy/virtual_keys
- LiteLLM Admin UI: https://docs.litellm.ai/docs/proxy/ui
- LiteLLM RBAC: https://docs.litellm.ai/docs/proxy/access_control
- LiteLLM audit logs: https://docs.litellm.ai/docs/proxy/multiple_admins
- LiteLLM public/private routes: https://docs.litellm.ai/docs/proxy/public_routes

## Operator Role Model

Use the following production role model even when LiteLLM itself cannot enforce
every split in the default OSS profile.

| Role | Typical access | Allowed operations | Notes |
| --- | --- | --- | --- |
| Platform admin | SSO admin group, break-glass `LITELLM_MASTER_KEY` access | Configure proxy, rotate secrets, manage models, manage all users/teams/keys | Keep the group small. Human use of the master key should be exceptional. |
| Key operator | Admin UI or controlled admin API workflow | Create, revoke, block, unblock, and regenerate virtual keys | Should not own upstream provider keys. |
| Budget/model-access operator | Admin UI or controlled admin API workflow | Change budgets, rate limits, allowed models, team access | Changes must be auditable and tied to a ticket/change request. |
| Auditor/read-only | Read-only Admin UI role where available, dashboards/logs otherwise | View usage, spend, key inventory, admin actions | In OSS-only deployments, provide read-only evidence through dashboards/log exports instead of shared admin credentials. |
| Break-glass admin | Sealed secret or privileged secret-manager role | Emergency disablement, master-key rotation, admin lockout recovery | Requires incident ticket, two-person approval where possible, and post-incident rotation. |
| CI/service account | Protected CI secret scoped to deployment repo | Bootstrap short-lived smoke/test keys, run deployment checks | Never print `LITELLM_MASTER_KEY`; prefer short-lived generated virtual keys for test calls. |

## Admin Operations

Treat these as privileged operations:

- create, update, regenerate, block, unblock, or delete virtual keys;
- create/update/delete LiteLLM users, teams, service accounts, or internal users;
- change budgets, rate limits, allowed models, aliases, or routing behavior;
- add/remove upstream models or provider credentials;
- rotate `LITELLM_MASTER_KEY`, `UI_PASSWORD`, provider keys, or database secrets;
- enable/disable Admin UI or change SSO/reverse-proxy policy;
- execute break-glass access or emergency disablement.

Client request audit and admin action audit are different things. Guardrail logs
such as `gateway_guardrail_audit` prove request-time security decisions; they do
not prove who changed a user's budget or created an admin key.

## Rotation and Emergency Revocation

### `LITELLM_MASTER_KEY`

Use a new high-entropy `sk-ru-...` value generated by `make setup` or a secret
manager. Store it in the production secret store, update the runtime secret, and
recreate the LiteLLM container. Verify:

- `make health` passes;
- Admin UI/API access works from the operator network only;
- normal client calls with virtual keys still work;
- old master key is no longer accepted.

Rotating the master key is not the same as revoking user virtual keys stored in
PostgreSQL. If a user/app token is compromised, block/delete that virtual key
through Admin UI or admin API.

### `UI_PASSWORD`

Update `UI_PASSWORD` in the secret store or `.env`, recreate the LiteLLM
container, verify login, and record the change. Rotate after operator departure,
suspected exposure, or shared-password use in non-production.

### Virtual Keys

For ordinary user/app compromise:

1. Block or delete the affected virtual key.
2. Issue a replacement key with the minimum required models, budget, and TTL.
3. Check usage/spend around the compromise window.
4. Record the incident or support ticket id.

### Break-glass

Break-glass access should be rare and auditable:

- require an incident/change record before access when possible;
- limit access to a small operator group;
- record the exact action taken;
- rotate any secret exposed during the event;
- close with a post-incident review.

## Admin Audit Requirements

Production deployments should capture admin activity from the strongest available
source:

- LiteLLM audit logs where available;
- reverse proxy / SSO access logs for `/ui` and admin API routes;
- CI job logs for bootstrap automation, with secrets redacted;
- GitOps or ticket records for config/model/provider changes;
- database backup/change records for LiteLLM key/user state.

Minimum fields for admin audit evidence:

- actor identity or service account;
- source boundary, such as SSO group, CI job, VPN, or break-glass role;
- action type and target, for example `key.block`, `budget.update`, `model.add`;
- timestamp;
- ticket/change/incident id when available;
- result: success, failure, rollback.

Do not log raw provider keys, raw virtual keys, `LITELLM_MASTER_KEY`,
`UI_PASSWORD`, or complete Authorization headers.

## Production Checklist

Before exposing a production instance:

- `LITELLM_MASTER_KEY`, `LITELLM_SALT_KEY`, provider keys, `UI_PASSWORD`, and
  PostgreSQL password are generated secrets, not placeholders.
- Users and client tools receive virtual keys or validated JWT/OIDC tokens, not
  the master key.
- Admin UI is either disabled with `DISABLE_ADMIN_UI=True` or protected by an
  operator-only SSO/VPN/IP allowlist/mTLS boundary.
- Admin API routes are not publicly reachable without the same operator boundary.
- Operator roles and break-glass ownership are documented for the deployment.
- Key/budget/model/admin changes have an audit source.
- Master key and UI credential rotation are tested in staging.
- CI/service accounts can create only short-lived test keys and cannot print
  admin secrets.
- Backups cover PostgreSQL because LiteLLM users, virtual keys, budgets, and
  spend live there.
