#!/usr/bin/env bash
# Live guardrails smoke tests for non-streaming and streaming LiteLLM paths.
set -euo pipefail

BASE_URL="${LITELLM_URL:-http://localhost:4000}"
ENV_FILE="${ENV_FILE:-.env}"
CHAT_MODEL="${CHAT_MODEL:-glm-5.1}"

if [ -f "$ENV_FILE" ]; then
    set -a
    # shellcheck disable=SC1090
    . "$ENV_FILE"
    set +a
fi

if ! command -v curl >/dev/null 2>&1; then
    echo "curl is required" >&2
    exit 1
fi

if ! command -v docker >/dev/null 2>&1; then
    echo "docker is required for Redis cleanup verification" >&2
    exit 1
fi

if ! curl -sf "$BASE_URL/health/liveliness" >/dev/null 2>&1; then
    echo "LiteLLM is not reachable at $BASE_URL" >&2
    exit 1
fi

PASS=0
FAIL=0
TMP_DIR=$(mktemp -d)
trap 'rm -rf "$TMP_DIR"' EXIT

pass() {
    echo "  ✅ $1"
    PASS=$((PASS + 1))
}

fail() {
    echo "  ❌ $1"
    FAIL=$((FAIL + 1))
}

create_smoke_key() {
    local output key

    output=$(
        bash scripts/create_virtual_key.sh \
            --base-url "$BASE_URL" \
            --env-file "$ENV_FILE" \
            --alias "guardrails-smoke-$(date +%Y%m%d%H%M%S)" \
            --models "standard,zai" \
            --duration "30m" \
            --metadata-json '{"purpose":"guardrails-smoke"}'
    )
    key=$(printf '%s\n' "$output" | awk -F= '$1 == "RU_LLM_PROXY_TOKEN" {print $2; exit}')
    if [ -z "$key" ]; then
        echo "Failed to parse generated virtual key" >&2
        exit 1
    fi
    printf '%s' "$key"
}

mapping_count() {
    docker compose exec -T redis redis-cli --scan --pattern 'pii_mapping:*' |
        awk 'END {print NR}'
}

mapping_keys() {
    docker compose exec -T redis redis-cli --scan --pattern 'pii_mapping:*' | sort
}

run_chat_completion() {
    local label="$1"
    local payload="$2"
    local mode="$3"
    local error_file
    local curl_exit

    HEADERS_FILE="$TMP_DIR/$label.headers"
    BODY_FILE="$TMP_DIR/$label.body"
    error_file="$TMP_DIR/$label.err"

    set +e
    if [ "$mode" = "stream" ]; then
        HTTP_STATUS=$(
            curl -sS --no-buffer -D "$HEADERS_FILE" -o "$BODY_FILE" -w "%{http_code}" \
                "$BASE_URL/v1/chat/completions" \
                -H "Authorization: Bearer $RU_LLM_PROXY_TOKEN" \
                -H "Accept: text/event-stream" \
                -H "Content-Type: application/json" \
                -d "$payload" 2>"$error_file"
        )
        curl_exit=$?
    else
        HTTP_STATUS=$(
            curl -sS -D "$HEADERS_FILE" -o "$BODY_FILE" -w "%{http_code}" \
                "$BASE_URL/v1/chat/completions" \
                -H "Authorization: Bearer $RU_LLM_PROXY_TOKEN" \
                -H "Content-Type: application/json" \
                -d "$payload" 2>"$error_file"
        )
        curl_exit=$?
    fi
    set -e
    CURL_EXIT=$curl_exit

    if [ -z "$HTTP_STATUS" ]; then
        HTTP_STATUS="000"
    fi
}

expect_http_success() {
    local label="$1"

    if [ "${CURL_EXIT:-1}" -ne 0 ]; then
        fail "$label curl failed with exit $CURL_EXIT"
        return
    fi

    case "$HTTP_STATUS" in
        2??) pass "$label returned HTTP $HTTP_STATUS" ;;
        000)
            fail "$label could not reach $BASE_URL"
            ;;
        *)
            fail "$label returned HTTP $HTTP_STATUS"
            ;;
    esac
}

expect_guardrails_header() {
    local label="$1"
    local header_value

    header_value=$(
        awk 'tolower($0) ~ /^x-litellm-applied-guardrails:/ {gsub(/\r/, "", $0); print $0; exit}' "$HEADERS_FILE"
    )
    if [ -n "$header_value" ] &&
        printf '%s\n' "$header_value" | grep -q 'ru-pii-mask-pre' &&
        printf '%s\n' "$header_value" | grep -q 'ru-pii-mask-post'; then
        pass "$label returned x-litellm-applied-guardrails"
    else
        fail "$label did not return expected x-litellm-applied-guardrails"
    fi
}

expect_stream_events() {
    if grep -qi '^event:[[:space:]]*error' "$BODY_FILE" ||
        grep -qi '^data:.*"error"' "$BODY_FILE"; then
        fail "streaming response emitted SSE error event"
    elif grep -q '^data:' "$BODY_FILE" &&
        grep -q '^data:[[:space:]]*\[DONE\]' "$BODY_FILE"; then
        pass "streaming response emitted SSE data events"
    else
        fail "streaming response did not emit complete SSE data events"
    fi
}

echo ""
echo "🛡️  ru-llm-proxy — Guardrails Smoke"
echo "==================================="
echo ""

RU_LLM_PROXY_TOKEN=$(create_smoke_key)
export RU_LLM_PROXY_TOKEN

before_keys_file="$TMP_DIR/redis-before.keys"
after_keys_file="$TMP_DIR/redis-after.keys"
mapping_keys > "$before_keys_file"
before_mappings=$(mapping_count)
echo "Redis PII mappings before smoke: $before_mappings"

non_stream_payload='{"model":"'"$CHAT_MODEL"'","guardrails":["ru-pii-mask-pre","ru-pii-mask-post"],"messages":[{"role":"user","content":"Проверь текст: Иван Иванов, телефон +79031234567"}],"max_tokens":40}'
run_chat_completion "non-stream" "$non_stream_payload" "non-stream"
expect_http_success "non-streaming guardrails request"
expect_guardrails_header "non-streaming guardrails request"

stream_payload='{"model":"'"$CHAT_MODEL"'","stream":true,"guardrails":["ru-pii-mask-pre","ru-pii-mask-post"],"messages":[{"role":"user","content":"Проверь текст: Иван Иванов, телефон +79031234567"}],"max_tokens":40}'
run_chat_completion "stream" "$stream_payload" "stream"
expect_http_success "streaming guardrails request"
expect_guardrails_header "streaming guardrails request"
expect_stream_events

mapping_keys > "$after_keys_file"
after_mappings=$(mapping_count)
echo "Redis PII mappings after smoke:  $after_mappings"
new_mapping_keys=$(comm -13 "$before_keys_file" "$after_keys_file")
if [ -z "$new_mapping_keys" ]; then
    pass "Redis PII mapping set did not grow after completed stream"
else
    fail "Redis PII mapping set grew after completed stream"
fi

echo ""
echo "Results: $PASS passed, $FAIL failed"
if [ "$FAIL" -gt 0 ]; then
    exit 1
fi
