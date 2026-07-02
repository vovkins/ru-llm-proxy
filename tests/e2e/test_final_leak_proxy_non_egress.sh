#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
COMPOSE_FILE="$ROOT_DIR/tests/e2e/docker-compose.pre-egress-proxy.yml"
PROJECT_NAME="${FINAL_LEAK_PROXY_PROJECT:-ru-llm-proxy-final-leak-$$}"
PROXY_PORT="${FINAL_LEAK_PROXY_PORT:-${PRE_EGRESS_PROXY_PORT:-14001}}"
export PRE_EGRESS_PROXY_PORT="$PROXY_PORT"
BASE_URL="http://localhost:${PROXY_PORT}"
MASTER_KEY="sk-test-master"
CANARY="RU_PROXY_FINAL_CANARY"
RAW_PHONE="+79031234567"
PRIVATE_KEY_MARKER="-----BEGIN PRIVATE KEY-----"
CURL_CONNECT_TIMEOUT="${CURL_CONNECT_TIMEOUT:-2}"
CURL_MAX_TIME="${CURL_MAX_TIME:-20}"

tmp_dir="$(mktemp -d)"
cleanup() {
    docker compose -p "$PROJECT_NAME" -f "$COMPOSE_FILE" down -v >/dev/null 2>&1 || true
    rm -rf "$tmp_dir"
}
trap cleanup EXIT

json_get() {
    local file="$1"
    local key="$2"
    python3 - "$file" "$key" <<'PY'
import json
import sys

with open(sys.argv[1], encoding="utf-8") as fh:
    value = json.load(fh)
for part in sys.argv[2].split("."):
    value = value[part]
print(json.dumps(value))
PY
}

wait_for_http() {
    local url="$1"
    local description="$2"
    local attempts="${3:-60}"

    for _ in $(seq 1 "$attempts"); do
        if curl -fsS \
            --connect-timeout "$CURL_CONNECT_TIMEOUT" \
            --max-time "$CURL_MAX_TIME" \
            "$url" >/dev/null 2>&1; then
            return 0
        fi
        sleep 1
    done

    echo "Timed out waiting for $description at $url" >&2
    return 1
}

capture_counts() {
    local output_file="$1"
    docker compose -p "$PROJECT_NAME" -f "$COMPOSE_FILE" exec -T mock-upstream \
        python - <<'PY' >"$output_file"
import urllib.request

with urllib.request.urlopen("http://127.0.0.1:8080/capture", timeout=5) as response:
    print(response.read().decode("utf-8"))
PY
}

reset_capture() {
    docker compose -p "$PROJECT_NAME" -f "$COMPOSE_FILE" exec -T mock-upstream \
        python - <<'PY' >/dev/null
import urllib.request

request = urllib.request.Request(
    "http://127.0.0.1:8080/capture/reset",
    data=b"{}",
    method="POST",
)
with urllib.request.urlopen(request, timeout=5):
    pass
PY
}

post_json() {
    local path="$1"
    local payload="$2"
    local body_file="$3"

    curl -sS \
        --connect-timeout "$CURL_CONNECT_TIMEOUT" \
        --max-time "$CURL_MAX_TIME" \
        -o "$body_file" \
        -w "%{http_code}" \
        -H "Authorization: Bearer $MASTER_KEY" \
        -H "Content-Type: application/json" \
        -d "$payload" \
        "$BASE_URL$path"
}

expect_json_value() {
    local file="$1"
    local key="$2"
    local expected="$3"
    local actual
    actual="$(json_get "$file" "$key")"
    if [ "$actual" != "$expected" ]; then
        echo "Expected $key=$expected, got $actual" >&2
        cat "$file" >&2
        exit 1
    fi
}

expect_safe_block_body() {
    local body_file="$1"
    local forbidden="$2"
    expect_json_value "$body_file" error.code '"final_payload_leak_check_blocked"'
    if grep -Fq -- "$forbidden" "$body_file"; then
        echo "Blocked response leaked raw final-check value" >&2
        cat "$body_file" >&2
        exit 1
    fi
}

docker compose -p "$PROJECT_NAME" -f "$COMPOSE_FILE" up -d

wait_for_http "$BASE_URL/health/liveliness" "LiteLLM proxy"
docker compose -p "$PROJECT_NAME" -f "$COMPOSE_FILE" exec -T mock-upstream \
    python - <<'PY' >/dev/null
import urllib.request

with urllib.request.urlopen("http://127.0.0.1:8080/health", timeout=5):
    pass
PY

reset_capture
clean_body="$tmp_dir/clean.json"
clean_status="$(post_json "/v1/chat/completions" '{"model":"mock-chat","messages":[{"role":"user","content":"Summarize the deployment plan."}]}' "$clean_body")"
if [ "$clean_status" != "200" ]; then
    echo "Expected clean prompt status 200, got $clean_status" >&2
    cat "$clean_body" >&2
    exit 1
fi
clean_capture="$tmp_dir/clean-capture.json"
capture_counts "$clean_capture"
expect_json_value "$clean_capture" analyzer_requests 1
expect_json_value "$clean_capture" provider_requests 1

reset_capture
canary_body="$tmp_dir/canary.json"
canary_status="$(post_json "/v1/chat/completions" '{"model":"mock-chat","messages":[{"role":"user","content":"Summarize RU_PROXY_FINAL_CANARY"}]}' "$canary_body")"
if [ "$canary_status" != "422" ]; then
    echo "Expected canary prompt status 422, got $canary_status" >&2
    cat "$canary_body" >&2
    exit 1
fi
expect_safe_block_body "$canary_body" "$CANARY"
canary_capture="$tmp_dir/canary-capture.json"
capture_counts "$canary_capture"
expect_json_value "$canary_capture" analyzer_requests 1
expect_json_value "$canary_capture" analyzer_saw_canary true
expect_json_value "$canary_capture" provider_requests 0
expect_json_value "$canary_capture" provider_saw_canary false

reset_capture
responses_body="$tmp_dir/responses-canary.json"
responses_status="$(post_json "/v1/responses" '{"model":"mock-chat","input":[{"role":"user","content":[{"type":"input_text","text":"RU_PROXY_FINAL_CANARY"}]}]}' "$responses_body")"
if [ "$responses_status" != "422" ]; then
    echo "Expected Responses canary status 422, got $responses_status" >&2
    cat "$responses_body" >&2
    exit 1
fi
expect_safe_block_body "$responses_body" "$CANARY"
responses_capture="$tmp_dir/responses-canary-capture.json"
capture_counts "$responses_capture"
expect_json_value "$responses_capture" analyzer_requests 1
expect_json_value "$responses_capture" analyzer_saw_canary true
expect_json_value "$responses_capture" provider_requests 0
expect_json_value "$responses_capture" provider_saw_canary false

reset_capture
tool_schema_body="$tmp_dir/tool-schema-canary.json"
tool_schema_payload='{"model":"mock-chat","messages":[{"role":"user","content":"Use the tool."}],"tools":[{"type":"function","function":{"name":"lookup_account","description":"RU_PROXY_FINAL_CANARY","parameters":{"type":"object","properties":{"account_id":{"type":"string","description":"Account id"}}}}}]}'
tool_schema_status="$(post_json "/v1/chat/completions" "$tool_schema_payload" "$tool_schema_body")"
if [ "$tool_schema_status" != "422" ]; then
    echo "Expected tool schema canary status 422, got $tool_schema_status" >&2
    cat "$tool_schema_body" >&2
    exit 1
fi
expect_safe_block_body "$tool_schema_body" "$CANARY"
tool_schema_capture="$tmp_dir/tool-schema-canary-capture.json"
capture_counts "$tool_schema_capture"
expect_json_value "$tool_schema_capture" analyzer_requests 1
expect_json_value "$tool_schema_capture" analyzer_saw_canary false
expect_json_value "$tool_schema_capture" provider_requests 0
expect_json_value "$tool_schema_capture" provider_saw_canary false

reset_capture
private_key_body="$tmp_dir/private-key.json"
private_key_payload='{"model":"mock-chat","messages":[{"role":"user","content":"-----BEGIN PRIVATE KEY-----\nredacted\n-----END PRIVATE KEY-----"}]}'
private_key_status="$(post_json "/v1/chat/completions" "$private_key_payload" "$private_key_body")"
if [ "$private_key_status" != "422" ]; then
    echo "Expected private-key marker status 422, got $private_key_status" >&2
    cat "$private_key_body" >&2
    exit 1
fi
expect_safe_block_body "$private_key_body" "$PRIVATE_KEY_MARKER"
private_key_capture="$tmp_dir/private-key-capture.json"
capture_counts "$private_key_capture"
expect_json_value "$private_key_capture" analyzer_requests 1
expect_json_value "$private_key_capture" provider_requests 0
expect_json_value "$private_key_capture" provider_saw_private_key_marker false

reset_capture
masked_body="$tmp_dir/masked.json"
masked_status="$(post_json "/v1/chat/completions" '{"model":"mock-chat","messages":[{"role":"user","content":"Мой телефон +79031234567"}]}' "$masked_body")"
if [ "$masked_status" != "200" ]; then
    echo "Expected masked PII prompt status 200, got $masked_status" >&2
    cat "$masked_body" >&2
    exit 1
fi
masked_capture="$tmp_dir/masked-capture.json"
capture_counts "$masked_capture"
expect_json_value "$masked_capture" analyzer_requests 1
expect_json_value "$masked_capture" provider_requests 1
expect_json_value "$masked_capture" provider_saw_raw_phone false
expect_json_value "$masked_capture" provider_saw_phone_placeholder true
if grep -Fq -- "$RAW_PHONE" "$masked_body"; then
    echo "Masked response leaked raw phone" >&2
    cat "$masked_body" >&2
    exit 1
fi

echo "final payload leak-check proxy non-egress smoke passed"
