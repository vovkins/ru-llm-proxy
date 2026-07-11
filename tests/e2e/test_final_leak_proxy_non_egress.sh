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

expect_no_provider_posts() {
    local file="$1"

    expect_json_value "$file" provider_requests 0
    expect_json_value "$file" provider_request_paths '[]'
}

expect_provider_paths() {
    local file="$1"
    local expected="$2"

    expect_json_value "$file" provider_request_paths "$expected"
}

expect_safe_block_body() {
    local body_file="$1"
    local forbidden="$2"
    local code
    code="$(json_get "$body_file" error.code)"
    if [ "$code" != '"final_payload_leak_check_blocked"' ]; then
        if [ "$code" != '"422"' ] || ! grep -Fq "confirmed raw leak marker" "$body_file"; then
            echo "Expected final payload leak-check block, got error.code=$code" >&2
            cat "$body_file" >&2
            exit 1
        fi
    fi
    if grep -Fq -- "$forbidden" "$body_file"; then
        echo "Blocked response leaked raw final-check value" >&2
        cat "$body_file" >&2
        exit 1
    fi
}

expect_final_block_no_provider() {
    local label="$1"
    local path="$2"
    local payload="$3"
    local forbidden="$4"
    local expected_analyzer_requests="$5"
    local expected_analyzer_saw_canary="$6"

    reset_capture
    local body_file="$tmp_dir/${label}.json"
    local status
    status="$(post_json "$path" "$payload" "$body_file")"
    if [ "$status" != "422" ]; then
        echo "Expected $label status 422, got $status" >&2
        cat "$body_file" >&2
        exit 1
    fi
    expect_safe_block_body "$body_file" "$forbidden"
    local capture_file="$tmp_dir/${label}-capture.json"
    capture_counts "$capture_file"
    expect_json_value "$capture_file" analyzer_requests "$expected_analyzer_requests"
    expect_json_value "$capture_file" analyzer_saw_canary "$expected_analyzer_saw_canary"
    expect_no_provider_posts "$capture_file"
    expect_json_value "$capture_file" provider_saw_canary false
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
expect_provider_paths "$clean_capture" '["/v1/chat/completions"]'

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
expect_no_provider_posts "$canary_capture"
expect_json_value "$canary_capture" provider_saw_canary false

reset_capture
chat_image_url_body="$tmp_dir/chat-image-url-canary.json"
chat_image_url_payload='{"model":"mock-chat","messages":[{"role":"user","content":[{"type":"text","text":"Clean prompt"},{"type":"image_url","image_url":{"url":"RU_PROXY_FINAL_CANARY"}}]}]}'
chat_image_url_status="$(post_json "/v1/chat/completions" "$chat_image_url_payload" "$chat_image_url_body")"
if [ "$chat_image_url_status" != "422" ]; then
    echo "Expected chat image_url canary status 422, got $chat_image_url_status" >&2
    cat "$chat_image_url_body" >&2
    exit 1
fi
expect_safe_block_body "$chat_image_url_body" "$CANARY"
chat_image_url_capture="$tmp_dir/chat-image-url-canary-capture.json"
capture_counts "$chat_image_url_capture"
expect_json_value "$chat_image_url_capture" analyzer_requests 1
expect_json_value "$chat_image_url_capture" analyzer_saw_canary false
expect_no_provider_posts "$chat_image_url_capture"
expect_json_value "$chat_image_url_capture" provider_saw_canary false

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
expect_no_provider_posts "$responses_capture"
expect_json_value "$responses_capture" provider_saw_canary false

reset_capture
responses_image_url_body="$tmp_dir/responses-image-url-canary.json"
responses_image_url_payload='{"model":"mock-chat","input":[{"role":"user","content":[{"type":"input_text","text":"Clean prompt"},{"type":"input_image","image_url":"RU_PROXY_FINAL_CANARY"}]}]}'
responses_image_url_status="$(post_json "/v1/responses" "$responses_image_url_payload" "$responses_image_url_body")"
if [ "$responses_image_url_status" != "422" ]; then
    echo "Expected Responses input_image canary status 422, got $responses_image_url_status" >&2
    cat "$responses_image_url_body" >&2
    exit 1
fi
expect_safe_block_body "$responses_image_url_body" "$CANARY"
responses_image_url_capture="$tmp_dir/responses-image-url-canary-capture.json"
capture_counts "$responses_image_url_capture"
expect_json_value "$responses_image_url_capture" analyzer_requests 1
expect_json_value "$responses_image_url_capture" analyzer_saw_canary false
expect_no_provider_posts "$responses_image_url_capture"
expect_json_value "$responses_image_url_capture" provider_saw_canary false

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
expect_no_provider_posts "$tool_schema_capture"
expect_json_value "$tool_schema_capture" provider_saw_canary false

reset_capture
tool_schema_key_body="$tmp_dir/tool-schema-key-canary.json"
tool_schema_key_payload='{"model":"mock-chat","messages":[{"role":"user","content":"Use the tool."}],"tools":[{"type":"function","function":{"name":"lookup_account","description":"Lookup account","parameters":{"type":"object","properties":{"RU_PROXY_FINAL_CANARY":{"type":"string","description":"Account id"}}}}}]}'
tool_schema_key_status="$(post_json "/v1/chat/completions" "$tool_schema_key_payload" "$tool_schema_key_body")"
if [ "$tool_schema_key_status" != "422" ]; then
    echo "Expected tool schema key canary status 422, got $tool_schema_key_status" >&2
    cat "$tool_schema_key_body" >&2
    exit 1
fi
expect_safe_block_body "$tool_schema_key_body" "$CANARY"
tool_schema_key_capture="$tmp_dir/tool-schema-key-canary-capture.json"
capture_counts "$tool_schema_key_capture"
expect_json_value "$tool_schema_key_capture" analyzer_requests 1
expect_json_value "$tool_schema_key_capture" analyzer_saw_canary false
expect_no_provider_posts "$tool_schema_key_capture"
expect_json_value "$tool_schema_key_capture" provider_saw_canary false

reset_capture
extra_body_body="$tmp_dir/extra-body-canary.json"
extra_body_payload='{"model":"mock-chat","messages":[{"role":"user","content":"Use provider options."}],"extra_body":{"providerOptions":{"trace":"RU_PROXY_FINAL_CANARY"}}}'
extra_body_status="$(post_json "/v1/chat/completions" "$extra_body_payload" "$extra_body_body")"
if [ "$extra_body_status" != "422" ]; then
    echo "Expected extra_body canary status 422, got $extra_body_status" >&2
    cat "$extra_body_body" >&2
    exit 1
fi
expect_safe_block_body "$extra_body_body" "$CANARY"
extra_body_capture="$tmp_dir/extra-body-canary-capture.json"
capture_counts "$extra_body_capture"
expect_json_value "$extra_body_capture" analyzer_requests 1
expect_json_value "$extra_body_capture" analyzer_saw_canary false
expect_no_provider_posts "$extra_body_capture"
expect_json_value "$extra_body_capture" provider_saw_canary false

reset_capture
stop_body="$tmp_dir/stop-canary.json"
stop_payload='{"model":"mock-chat","messages":[{"role":"user","content":"Clean prompt."}],"stop":["RU_PROXY_FINAL_CANARY"]}'
stop_status="$(post_json "/v1/chat/completions" "$stop_payload" "$stop_body")"
if [ "$stop_status" != "422" ]; then
    echo "Expected stop canary status 422, got $stop_status" >&2
    cat "$stop_body" >&2
    exit 1
fi
expect_safe_block_body "$stop_body" "$CANARY"
stop_capture="$tmp_dir/stop-canary-capture.json"
capture_counts "$stop_capture"
expect_json_value "$stop_capture" analyzer_requests 1
expect_json_value "$stop_capture" analyzer_saw_canary false
expect_no_provider_posts "$stop_capture"
expect_json_value "$stop_capture" provider_saw_canary false

reset_capture
messages_stop_sequences_body="$tmp_dir/messages-stop-sequences-canary.json"
messages_stop_sequences_payload='{"model":"mock-claude","max_tokens":16,"messages":[{"role":"user","content":"Clean prompt."}],"stop_sequences":["RU_PROXY_FINAL_CANARY"]}'
messages_stop_sequences_status="$(post_json "/v1/messages" "$messages_stop_sequences_payload" "$messages_stop_sequences_body")"
if [ "$messages_stop_sequences_status" != "422" ]; then
    echo "Expected Anthropic stop_sequences canary status 422, got $messages_stop_sequences_status" >&2
    cat "$messages_stop_sequences_body" >&2
    exit 1
fi
expect_safe_block_body "$messages_stop_sequences_body" "$CANARY"
messages_stop_sequences_capture="$tmp_dir/messages-stop-sequences-canary-capture.json"
capture_counts "$messages_stop_sequences_capture"
expect_json_value "$messages_stop_sequences_capture" analyzer_requests 1
expect_json_value "$messages_stop_sequences_capture" analyzer_saw_canary false
expect_no_provider_posts "$messages_stop_sequences_capture"
expect_json_value "$messages_stop_sequences_capture" provider_saw_canary false

extra_body_key_secret_payload='{"model":"mock-chat","messages":[{"role":"user","content":"Use provider options."}],"extra_body":{"DATABASE_URL":"postgres://user:pass@db.local/app"}}'
expect_final_block_no_provider "extra-body-key-secret" "/v1/chat/completions" "$extra_body_key_secret_payload" "user:pass" 1 false

extra_body_password_payload='{"model":"mock-chat","messages":[{"role":"user","content":"Use provider options."}],"extra_body":{"PASSWORD":"local-password"}}'
expect_final_block_no_provider "extra-body-password" "/v1/chat/completions" "$extra_body_password_payload" "local-password" 1 false

prompt_cache_key_payload='{"model":"mock-chat","messages":[{"role":"user","content":"Clean prompt."}],"prompt_cache_key":"RU_PROXY_FINAL_CANARY"}'
expect_final_block_no_provider "prompt-cache-key-canary" "/v1/chat/completions" "$prompt_cache_key_payload" "$CANARY" 1 false

safety_identifier_payload='{"model":"mock-chat","messages":[{"role":"user","content":"Clean prompt."}],"safety_identifier":"RU_PROXY_FINAL_CANARY"}'
expect_final_block_no_provider "safety-identifier-canary" "/v1/chat/completions" "$safety_identifier_payload" "$CANARY" 1 false

web_search_options_payload='{"model":"mock-chat","messages":[{"role":"user","content":"Clean prompt."}],"web_search_options":{"user_location":{"type":"approximate","city":"RU_PROXY_FINAL_CANARY"}}}'
expect_final_block_no_provider "web-search-options-canary" "/v1/chat/completions" "$web_search_options_payload" "$CANARY" 1 false

user_payload='{"model":"mock-chat","messages":[{"role":"user","content":"Clean prompt."}],"user":"RU_PROXY_FINAL_CANARY"}'
expect_final_block_no_provider "user-canary" "/v1/chat/completions" "$user_payload" "$CANARY" 1 false

metadata_payload='{"model":"mock-claude","max_tokens":16,"messages":[{"role":"user","content":"Clean prompt."}],"metadata":{"user_id":"RU_PROXY_FINAL_CANARY"}}'
expect_final_block_no_provider "messages-metadata-canary" "/v1/messages" "$metadata_payload" "$CANARY" 1 false

reset_capture
tool_schema_secret_body="$tmp_dir/tool-schema-secret.json"
tool_schema_secret_payload='{"model":"mock-chat","messages":[{"role":"user","content":"Use the tool."}],"tools":[{"type":"function","function":{"name":"lookup_account","description":"PASSWORD=local-password","parameters":{"type":"object","properties":{"account_id":{"type":"string","description":"Account id"}}}}}]}'
tool_schema_secret_status="$(post_json "/v1/chat/completions" "$tool_schema_secret_payload" "$tool_schema_secret_body")"
if [ "$tool_schema_secret_status" != "422" ]; then
    echo "Expected tool schema secret status 422, got $tool_schema_secret_status" >&2
    cat "$tool_schema_secret_body" >&2
    exit 1
fi
expect_safe_block_body "$tool_schema_secret_body" "local-password"
tool_schema_secret_capture="$tmp_dir/tool-schema-secret-capture.json"
capture_counts "$tool_schema_secret_capture"
expect_json_value "$tool_schema_secret_capture" analyzer_requests 1
expect_json_value "$tool_schema_secret_capture" analyzer_saw_canary false
expect_no_provider_posts "$tool_schema_secret_capture"
expect_json_value "$tool_schema_secret_capture" provider_saw_canary false

reset_capture
messages_tool_use_body="$tmp_dir/messages-tool-use-canary.json"
messages_tool_use_payload='{"model":"mock-claude","max_tokens":16,"messages":[{"role":"assistant","content":[{"type":"tool_use","id":"toolu_1","name":"lookup_account","input":{"query":"RU_PROXY_FINAL_CANARY"}}]}]}'
messages_tool_use_status="$(post_json "/v1/messages" "$messages_tool_use_payload" "$messages_tool_use_body")"
if [ "$messages_tool_use_status" != "422" ]; then
    echo "Expected Anthropic tool_use canary status 422, got $messages_tool_use_status" >&2
    cat "$messages_tool_use_body" >&2
    exit 1
fi
expect_safe_block_body "$messages_tool_use_body" "$CANARY"
messages_tool_use_capture="$tmp_dir/messages-tool-use-canary-capture.json"
capture_counts "$messages_tool_use_capture"
expect_json_value "$messages_tool_use_capture" analyzer_requests 0
expect_json_value "$messages_tool_use_capture" analyzer_saw_canary false
expect_no_provider_posts "$messages_tool_use_capture"
expect_json_value "$messages_tool_use_capture" provider_saw_canary false

reset_capture
messages_tool_use_name_body="$tmp_dir/messages-tool-use-name-canary.json"
messages_tool_use_name_payload='{"model":"mock-claude","max_tokens":16,"messages":[{"role":"assistant","content":[{"type":"tool_use","id":"toolu_1","name":"RU_PROXY_FINAL_CANARY","input":{"query":"clean"}}]}]}'
messages_tool_use_name_status="$(post_json "/v1/messages" "$messages_tool_use_name_payload" "$messages_tool_use_name_body")"
if [ "$messages_tool_use_name_status" != "422" ]; then
    echo "Expected Anthropic tool_use name canary status 422, got $messages_tool_use_name_status" >&2
    cat "$messages_tool_use_name_body" >&2
    exit 1
fi
expect_safe_block_body "$messages_tool_use_name_body" "$CANARY"
messages_tool_use_name_capture="$tmp_dir/messages-tool-use-name-canary-capture.json"
capture_counts "$messages_tool_use_name_capture"
expect_json_value "$messages_tool_use_name_capture" analyzer_requests 0
expect_json_value "$messages_tool_use_name_capture" analyzer_saw_canary false
expect_no_provider_posts "$messages_tool_use_name_capture"
expect_json_value "$messages_tool_use_name_capture" provider_saw_canary false

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
expect_no_provider_posts "$private_key_capture"
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
expect_provider_paths "$masked_capture" '["/v1/chat/completions"]'
expect_json_value "$masked_capture" provider_saw_raw_phone false
expect_json_value "$masked_capture" provider_saw_phone_placeholder true
if grep -Fq -- "$RAW_PHONE" "$masked_body"; then
    echo "Masked response leaked raw phone" >&2
    cat "$masked_body" >&2
    exit 1
fi

echo "final payload leak-check proxy non-egress smoke passed"
