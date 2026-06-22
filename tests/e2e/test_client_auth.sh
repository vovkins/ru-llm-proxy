#!/usr/bin/env bash
# Live client auth and protocol smoke tests.
set -euo pipefail

BASE_URL="${LITELLM_URL:-http://localhost:4000}"
ENV_FILE="${ENV_FILE:-.env}"
CHAT_MODEL="${CHAT_MODEL:-glm-5.1}"
RESPONSES_MODEL="${RESPONSES_MODEL:-}"
MESSAGES_MODEL="${MESSAGES_MODEL:-}"
DENIED_MODEL="${DENIED_MODEL:-glm-5.1}"
REQUIRE_ALL_PROTOCOLS="${REQUIRE_ALL_PROTOCOLS:-0}"

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

if ! command -v jq >/dev/null 2>&1; then
    echo "jq is required" >&2
    exit 1
fi

PASS=0
FAIL=0
SKIP=0

pass() {
    echo "  ✅ $1"
    PASS=$((PASS + 1))
}

fail() {
    echo "  ❌ $1"
    FAIL=$((FAIL + 1))
}

skip() {
    echo "  ⚠️  $1"
    SKIP=$((SKIP + 1))
}

missing_provider_key() {
    local description="$1"
    local secret_name="$2"

    if [ "$REQUIRE_ALL_PROTOCOLS" = "1" ]; then
        fail "$description required but $secret_name is not configured"
    else
        skip "$description skipped; $secret_name is not configured"
    fi
}

missing_smoke_model() {
    local description="$1"
    local env_name="$2"

    if [ "$REQUIRE_ALL_PROTOCOLS" = "1" ]; then
        fail "$description required but $env_name is not set to a live-validated proxy alias"
    else
        skip "$description skipped; set $env_name to a live-validated proxy alias"
    fi
}

has_configured_secret() {
    local name="$1"
    local value="${!name:-}"
    [ -n "$value" ] && [ "$value" != "***" ]
}

http_post() {
    local endpoint="$1"
    local token="$2"
    local payload="$3"
    local body_file status

    body_file=$(mktemp)
    if [ -n "$token" ]; then
        status=$(curl -sS -o "$body_file" -w "%{http_code}" \
            -X POST "$BASE_URL$endpoint" \
            -H "Authorization: Bearer $token" \
            -H "Content-Type: application/json" \
            -d "$payload" 2>/dev/null || true)
    else
        status=$(curl -sS -o "$body_file" -w "%{http_code}" \
            -X POST "$BASE_URL$endpoint" \
            -H "Content-Type: application/json" \
            -d "$payload" 2>/dev/null || true)
    fi
    if [ -z "$status" ]; then
        status="000"
    fi

    printf '%s\n' "$status"
    cat "$body_file"
    rm -f "$body_file"
}

http_get_with_separate_authorization() {
    local endpoint="$1"
    local proxy_token="$2"
    local other_auth="$3"
    local body_file status

    body_file=$(mktemp)
    status=$(curl -sS -o "$body_file" -w "%{http_code}" \
        -X GET "$BASE_URL$endpoint" \
        -H "x-litellm-api-key: Bearer $proxy_token" \
        -H "Authorization: Bearer $other_auth" \
        2>/dev/null || true)
    if [ -z "$status" ]; then
        status="000"
    fi

    printf '%s\n' "$status"
    cat "$body_file"
    rm -f "$body_file"
}

expect_rejected() {
    local description="$1"
    local status="$2"

    case "$status" in
        401|403)
            pass "$description rejected with HTTP $status"
            ;;
        400)
            pass "$description rejected with HTTP 400"
            ;;
        000)
            fail "$description could not reach $BASE_URL"
            ;;
        2??)
            fail "$description unexpectedly succeeded with HTTP $status"
            ;;
        *)
            fail "$description returned unexpected HTTP $status"
            ;;
    esac
}

expect_success() {
    local description="$1"
    local status="$2"
    local body="$3"

    case "$status" in
        2??)
            pass "$description succeeded with HTTP $status"
            ;;
        000)
            fail "$description could not reach $BASE_URL"
            ;;
        *)
            fail "$description failed with HTTP $status: $(printf '%s' "$body" | head -c 240)"
            ;;
    esac
}

create_smoke_key() {
    local alias="$1"
    local models="$2"
    local output key

    output=$(
        scripts/create_virtual_key.sh \
            --base-url "$BASE_URL" \
            --env-file "$ENV_FILE" \
            --alias "$alias" \
            --models "$models" \
            --duration "30m" \
            --metadata-json '{"purpose":"client-auth-smoke"}'
    )
    key=$(printf '%s\n' "$output" | awk -F= '$1 == "RU_LLM_PROXY_TOKEN" {print $2; exit}')
    if [ -z "$key" ]; then
        echo "Failed to parse generated virtual key" >&2
        printf '%s\n' "$output" >&2
        exit 1
    fi
    printf '%s' "$key"
}

echo ""
echo "🧪 ru-llm-proxy — Client Auth Smoke"
echo "==================================="
echo ""

if ! curl -sf "$BASE_URL/health/liveliness" >/dev/null 2>&1; then
    echo "LiteLLM is not reachable at $BASE_URL" >&2
    exit 1
fi

if [ -z "${LITELLM_MASTER_KEY:-}" ] || [ "${LITELLM_MASTER_KEY:-}" = "sk-replace-with-generated-key" ]; then
    echo "LITELLM_MASTER_KEY is required in environment or $ENV_FILE" >&2
    exit 1
fi

chat_payload='{"model":"'"$CHAT_MODEL"'","messages":[{"role":"user","content":"Reply with ok."}],"max_tokens":8}'

echo "📋 1. Ingress auth boundary"
no_token_result=$(http_post "/v1/chat/completions" "" "$chat_payload")
no_token_status=$(printf '%s\n' "$no_token_result" | sed -n '1p')
expect_rejected "missing token" "$no_token_status"

invalid_token_result=$(http_post "/v1/chat/completions" "sk-invalid-client-token" "$chat_payload")
invalid_token_status=$(printf '%s\n' "$invalid_token_result" | sed -n '1p')
expect_rejected "invalid token" "$invalid_token_status"

echo ""
echo "📋 2. Virtual key model access"
standard_key=$(create_smoke_key "smoke-standard-$(date +%Y%m%d%H%M%S)" "standard")
openai_key=$(create_smoke_key "smoke-openai-restricted-$(date +%Y%m%d%H%M%S)" "openai")

proxy_header_result=$(http_get_with_separate_authorization "/v1/models" "$standard_key" "non-litellm-auth-placeholder")
proxy_header_status=$(printf '%s\n' "$proxy_header_result" | sed -n '1p')
proxy_header_body=$(printf '%s\n' "$proxy_header_result" | sed '1d')
expect_success "x-litellm-api-key proxy auth while Authorization is occupied" "$proxy_header_status" "$proxy_header_body"

denied_result=$(http_post "/v1/chat/completions" "$openai_key" '{"model":"'"$DENIED_MODEL"'","messages":[{"role":"user","content":"Reply with ok."}],"max_tokens":8}')
denied_status=$(printf '%s\n' "$denied_result" | sed -n '1p')
expect_rejected "restricted key on disallowed model" "$denied_status"

if has_configured_secret ZAI_API_KEY; then
    chat_result=$(http_post "/v1/chat/completions" "$standard_key" "$chat_payload")
    chat_status=$(printf '%s\n' "$chat_result" | sed -n '1p')
    chat_body=$(printf '%s\n' "$chat_result" | sed '1d')
    expect_success "/v1/chat/completions with virtual key" "$chat_status" "$chat_body"
else
    missing_provider_key "/v1/chat/completions allowed-call" "ZAI_API_KEY"
fi

if has_configured_secret OPENAI_API_KEY; then
    if [ -n "$RESPONSES_MODEL" ]; then
        responses_payload='{"model":"'"$RESPONSES_MODEL"'","input":"Reply with ok.","max_output_tokens":16}'
        responses_result=$(http_post "/v1/responses" "$standard_key" "$responses_payload")
        responses_status=$(printf '%s\n' "$responses_result" | sed -n '1p')
        responses_body=$(printf '%s\n' "$responses_result" | sed '1d')
        expect_success "/v1/responses with virtual key" "$responses_status" "$responses_body"
    else
        missing_smoke_model "/v1/responses" "RESPONSES_MODEL"
    fi
else
    missing_provider_key "/v1/responses" "OPENAI_API_KEY"
fi

if has_configured_secret ANTHROPIC_API_KEY; then
    if [ -n "$MESSAGES_MODEL" ]; then
        messages_payload='{"model":"'"$MESSAGES_MODEL"'","max_tokens":16,"messages":[{"role":"user","content":"Reply with ok."}]}'
        messages_result=$(http_post "/v1/messages" "$standard_key" "$messages_payload")
        messages_status=$(printf '%s\n' "$messages_result" | sed -n '1p')
        messages_body=$(printf '%s\n' "$messages_result" | sed '1d')
        expect_success "/v1/messages with virtual key" "$messages_status" "$messages_body"
    else
        missing_smoke_model "/v1/messages" "MESSAGES_MODEL"
    fi
else
    missing_provider_key "/v1/messages" "ANTHROPIC_API_KEY"
fi

echo ""
echo "==================================="
echo "📊 Results: ${PASS} passed, ${FAIL} failed, ${SKIP} skipped"

if [ "$FAIL" -gt 0 ]; then
    exit 1
fi
