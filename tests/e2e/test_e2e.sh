#!/usr/bin/env bash
# Live smoke tests for ru-llm-proxy.
# Requires: running services (make up), curl, jq, and a configured LLM provider key.
# Deterministic guardrail masking/unmasking is covered by test_guardrail_flow.py.
set -euo pipefail

BASE_URL="${LITELLM_URL:-http://localhost:4000}"
ANALYZER_URL="${ANALYZER_URL:-http://localhost:5001}"
MASTER_KEY="${LITELLM_MASTER_KEY:-}"

if ! command -v jq &>/dev/null; then
    echo "❌ jq is required: apt install jq"
    exit 1
fi

PASS=0
FAIL=0
TOTAL=0

assert_contains() {
    local description="$1" haystack="$2" needle="$3"
    TOTAL=$((TOTAL + 1))
    if echo "$haystack" | grep -qF "$needle"; then
        echo "  ✅ $description"
        PASS=$((PASS + 1))
    else
        echo "  ❌ $description"
        echo "     Expected to find: $needle"
        FAIL=$((FAIL + 1))
    fi
}

assert_not_contains() {
    local description="$1" haystack="$2" needle="$3"
    TOTAL=$((TOTAL + 1))
    if echo "$haystack" | grep -qF "$needle"; then
        echo "  ❌ $description"
        echo "     Expected NOT to find: $needle"
        FAIL=$((FAIL + 1))
    else
        echo "  ✅ $description"
        PASS=$((PASS + 1))
    fi
}

extract_message_text() {
    jq -r '
        (.choices[0].message // {}) |
        if ((.content // "") != "") then .content
        elif ((.reasoning_content // "") != "") then .reasoning_content
        else "" end
    '
}

# =====================================================
echo ""
echo "🧪 ru-llm-proxy — Live Smoke Tests"
echo "====================================="
echo ""

# --- 1. Health checks ---
echo "📋 1. Health Checks"

TOTAL=$((TOTAL + 1))
health=$(curl -sf "$ANALYZER_URL/api/v1/health" 2>/dev/null || echo "")
if echo "$health" | grep -q '"status":"ok"'; then
    echo "  ✅ Presidio Analyzer is healthy"
    PASS=$((PASS + 1))
else
    echo "  ❌ Presidio Analyzer is NOT healthy"
    FAIL=$((FAIL + 1))
fi

TOTAL=$((TOTAL + 1))
if curl -sf "$BASE_URL/health/liveliness" >/dev/null 2>&1; then
    echo "  ✅ LiteLLM Proxy is alive"
    PASS=$((PASS + 1))
else
    echo "  ❌ LiteLLM Proxy is NOT healthy"
    FAIL=$((FAIL + 1))
fi

echo ""

# --- 2. Presidio PII detection ---
echo "📋 2. Presidio PII Detection"

test_text="Меня зовут Иван Иванов, телефон +79031234567, ИНН 7707083893"

TOTAL=$((TOTAL + 1))
analyze_result=$(curl -sf "$ANALYZER_URL/api/v1/analyze" \
    -H "Content-Type: application/json" \
    -d "{\"text\": \"$test_text\", \"language\": \"ru\"}" 2>/dev/null || echo "{}")

phone_found=$(echo "$analyze_result" | jq -r '.entities[] | select(.entity_type == "PHONE_NUMBER") | .text' 2>/dev/null | head -1)
if [ -n "$phone_found" ]; then
    echo "  ✅ Phone detected: $phone_found"
    PASS=$((PASS + 1))
else
    echo "  ❌ Phone NOT detected in: $test_text"
    FAIL=$((FAIL + 1))
fi

TOTAL=$((TOTAL + 1))
inn_found=$(echo "$analyze_result" | jq -r '.entities[] | select(.entity_type == "RU_INN") | .text' 2>/dev/null | head -1)
if [ -n "$inn_found" ]; then
    echo "  ✅ INN detected: $inn_found"
    PASS=$((PASS + 1))
else
    echo "  ❌ INN NOT detected in: $test_text"
    FAIL=$((FAIL + 1))
fi

TOTAL=$((TOTAL + 1))
person_found=$(echo "$analyze_result" | jq -r '.entities[] | select(.entity_type == "PERSON") | .text' 2>/dev/null | head -1)
if [ -n "$person_found" ]; then
    echo "  ✅ Person detected: $person_found"
    PASS=$((PASS + 1))
else
    echo "  ❌ Person NOT detected in: $test_text"
    FAIL=$((FAIL + 1))
fi

echo ""

# --- 3. LiteLLM basic call (no PII) ---
echo "📋 3. LiteLLM Basic Call (no PII)"

TOTAL=$((TOTAL + 1))
basic_response=$(curl -sf "$BASE_URL/chat/completions" \
    -H "Authorization: Bearer $MASTER_KEY" \
    -H "Content-Type: application/json" \
    -d '{"model":"glm-5.1","messages":[{"role":"user","content":"Say hello in Russian, one sentence only"}],"max_tokens":30}' 2>/dev/null || echo "{}")

basic_content=$(echo "$basic_response" | extract_message_text 2>/dev/null || true)
if [ -n "$basic_content" ]; then
    echo "  ✅ LLM responded: ${basic_content:0:80}..."
    PASS=$((PASS + 1))
else
    echo "  ❌ LLM did not respond"
    echo "     Response: $basic_response"
    FAIL=$((FAIL + 1))
fi

echo ""

# --- 4. LiteLLM with PII (live smoke) ---
echo "📋 4. LiteLLM with PII (live smoke)"

pii_request='{"model":"glm-5.1","messages":[{"role":"user","content":"Перепиши: Клиент Иванов Иван, телефон +79031234567, ИНН 7707083893, проживает г. Москва, ул. Тверская, д. 1. Перепиши это как краткую справку."}],"max_tokens":100}'

TOTAL=$((TOTAL + 1))
pii_response=$(curl -sf "$BASE_URL/chat/completions" \
    -H "Authorization: Bearer $MASTER_KEY" \
    -H "Content-Type: application/json" \
    -d "$pii_request" 2>/dev/null || echo "{}")

pii_content=$(echo "$pii_response" | extract_message_text 2>/dev/null || true)
if [ -n "$pii_content" ]; then
    echo "  ✅ LLM responded to PII request"
    PASS=$((PASS + 1))
    echo "     Response: ${pii_content:0:120}..."
else
    echo "  ❌ LLM did not respond to PII request"
    echo "     Response: $pii_response"
    FAIL=$((FAIL + 1))
fi

if echo "$pii_content" | grep -q "7707083893"; then
    echo "  ℹ️  Response contains original INN after post-call processing"
else
    echo "  ℹ️  INN not found in live response; the provider may have paraphrased or omitted it"
    echo "     Deterministic guardrail behavior is verified by make test-flow"
fi

echo ""

# --- Summary ---
echo "====================================="
echo "📊 Results: $PASS/$TOTAL passed, $FAIL failed"
if [ "$FAIL" -gt 0 ]; then
    echo "❌ Some tests failed"
    exit 1
else
    echo "✅ All tests passed!"
    exit 0
fi
