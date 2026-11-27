#!/usr/bin/env bash
set -euo pipefail
root="$(cd "$(dirname "$0")/../.." && pwd)"
export PRE_EGRESS_PROXY_PORT=127.0.0.1:0
compose=(docker compose -p "ru-guardrail-ui-$$" -f "$root/tests/e2e/docker-compose.pre-egress-proxy.yml"
         -f "$root/tests/e2e/docker-compose.guardrail-ui.yml")
node -e 'require("playwright")'
node "$root/tests/e2e/check_guardrail_card.cjs" "$root/litellm/dashboard/ru_guardrail_card.js"
trap '"${compose[@]}" down -v --remove-orphans >/dev/null' EXIT

run_suite() {
    export STORE_PROMPTS_IN_SPEND_LOGS="$1"
    "${compose[@]}" up --build -d --wait
    base="http://$("${compose[@]}" port litellm 4000)"
    curl --retry 60 --retry-delay 1 --retry-all-errors -fsS "$base/health/liveliness" >/dev/null
    python3 "$root/tests/e2e/check_guardrail_spend.py" "$base" "${compose[@]}"
    node "$root/tests/e2e/check_guardrail_ui.cjs" "$base"
    if test "$STORE_PROMPTS_IN_SPEND_LOGS" = false; then
        # Confirm storage, independently of the UI API, has only redacted responses.
        count=$("${compose[@]}" exec -T db psql -qtAX -U postgres -c "
            SELECT count(*) FROM \"LiteLLM_SpendLogs\",
            LATERAL jsonb_array_elements(metadata::jsonb->'guardrail_information') AS card
            WHERE card->>'guardrail_mode' = 'pre_call'
              AND card->>'guardrail_response' = 'REDACTED_BY_LITELM'
              AND card::text !~ 'UI_FIXTURE_|ui_private_value';")
        test "$count" -ge 5
    else
        # Unredacted mode may retain the safe response object, never the dictionary ID/source.
        count=$("${compose[@]}" exec -T db psql -qtAX -U postgres -c "
            SELECT count(*) FROM \"LiteLLM_SpendLogs\",
            LATERAL jsonb_array_elements(metadata::jsonb->'guardrail_information') AS card
            WHERE card->>'guardrail_name' = 'test-dictionary'
              AND card->'guardrail_response'->>'policy_result' = 'dictionary_substituted'
              AND card::text !~ 'ui_private_value';")
        test "$count" -ge 1
    fi
    "${compose[@]}" down -v --remove-orphans >/dev/null
}

run_suite false
run_suite true
