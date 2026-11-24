#!/usr/bin/env bash
set -euo pipefail
root="$(cd "$(dirname "$0")/../.." && pwd)"
export PRE_EGRESS_PROXY_PORT=127.0.0.1:0
compose=(docker compose -p "ru-guardrail-ui-$$" -f "$root/tests/e2e/docker-compose.pre-egress-proxy.yml"
         -f "$root/tests/e2e/docker-compose.guardrail-ui.yml")
node -e 'require("playwright")'
trap '"${compose[@]}" down -v --remove-orphans >/dev/null' EXIT
"${compose[@]}" up --build -d --wait
base="http://$("${compose[@]}" port litellm 4000)"
curl --retry 60 --retry-delay 1 --retry-all-errors -fsS "$base/health/liveliness" >/dev/null
node "$root/tests/e2e/check_guardrail_ui.cjs" "$base"
# Confirm that storage, independently of the UI API, has only redacted responses.
count=$("${compose[@]}" exec -T db psql -qtAX -U postgres -c "
    SELECT count(*) FROM \"LiteLLM_SpendLogs\",
    LATERAL jsonb_array_elements(metadata::jsonb->'guardrail_information') AS card
    WHERE card->>'guardrail_mode' = 'pre_call'
      AND card->>'guardrail_response' = 'REDACTED_BY_LITELM'
      AND card::text !~ 'UI_FIXTURE_|ui_private_value';")
test "$count" = 4
