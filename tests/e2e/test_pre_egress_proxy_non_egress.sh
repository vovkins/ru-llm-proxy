#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
COMPOSE_FILE="$ROOT_DIR/tests/e2e/docker-compose.pre-egress-proxy.yml"
PROJECT_NAME="${PRE_EGRESS_PROXY_PROJECT:-ru-llm-proxy-pre-egress-$$}"
PROXY_PORT="${PRE_EGRESS_PROXY_PORT:-14000}"
BASE_URL="http://localhost:${PROXY_PORT}"
MASTER_KEY="sk-test-master"
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

assert_pre_egress_blocked_error() {
    local file="$1"
    local expected_category="$2"
    local expected_rule="$3"

    python3 - "$file" "$expected_category" "$expected_rule" <<'PY'
import json
import sys

with open(sys.argv[1], encoding="utf-8") as fh:
    body = json.load(fh)

error = body.get("error")
if isinstance(error, dict):
    provider_fields = error.get("provider_specific_fields")
    if isinstance(provider_fields, dict) and isinstance(provider_fields.get("error"), dict):
        error = provider_fields["error"]
    elif isinstance(error.get("param"), dict):
        policy_param = error["param"].get("pre_egress_policy")
        if isinstance(policy_param, dict):
            error = {
                "message": error.get("message"),
                "type": error.get("type"),
                "code": policy_param.get("code"),
                "details": policy_param.get("details"),
            }

if not isinstance(error, dict) and isinstance(body.get("detail"), dict):
    detail = body["detail"]
    error = detail.get("error", detail)

if not isinstance(error, dict):
    print("Expected JSON error object", file=sys.stderr)
    print(json.dumps(body, ensure_ascii=False), file=sys.stderr)
    sys.exit(1)

details = error.get("details")
expected = {
    "message": "Request contains configuration or log data and was blocked by pre-egress policy.",
    "type": "pre_egress_policy_violation",
    "code": "pre_egress_policy_blocked",
    "details": {
        "categories": [sys.argv[2]],
        "rules": [sys.argv[3]],
    },
}
for key in ("message", "type", "code"):
    if error.get(key) != expected[key]:
        print(f"Expected error.{key}={expected[key]!r}, got {error.get(key)!r}", file=sys.stderr)
        print(json.dumps(body, ensure_ascii=False), file=sys.stderr)
        sys.exit(1)

if details != expected["details"]:
    print("Expected structured pre-egress details", file=sys.stderr)
    print(json.dumps(body, ensure_ascii=False), file=sys.stderr)
    sys.exit(1)
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

expect_no_provider_posts() {
    local file="$1"

    expect_json_value "$file" provider_requests 0
    expect_json_value "$file" provider_request_paths '[]'
}

run_clean_case() {
    local name="$1"
    local path="$2"
    local payload="$3"
    local expected_paths="$4"
    local body_file="$tmp_dir/${name}.json"
    local capture_file="$tmp_dir/${name}-capture.json"
    local status

    reset_capture
    status="$(post_json "$path" "$payload" "$body_file")"
    if [ "$status" != "200" ]; then
        echo "Expected $name status 200, got $status" >&2
        cat "$body_file" >&2
        exit 1
    fi

    capture_counts "$capture_file"
    expect_json_value "$capture_file" analyzer_requests 1
    expect_json_value "$capture_file" provider_requests 1
    expect_json_value "$capture_file" provider_request_paths "$expected_paths"
}

run_blocked_case() {
    local name="$1"
    local path="$2"
    local payload="$3"
    local expected_category="$4"
    local expected_rule="$5"
    local forbidden="$6"
    local body_file="$tmp_dir/${name}.json"
    local capture_file="$tmp_dir/${name}-capture.json"
    local status

    reset_capture
    status="$(post_json "$path" "$payload" "$body_file")"
    if [ "$status" != "422" ]; then
        echo "Expected $name status 422, got $status" >&2
        cat "$body_file" >&2
        exit 1
    fi

    assert_pre_egress_blocked_error "$body_file" "$expected_category" "$expected_rule"
    if grep -Fq -- "$forbidden" "$body_file"; then
        echo "Blocked response leaked raw value for $name" >&2
        cat "$body_file" >&2
        exit 1
    fi

    capture_counts "$capture_file"
    expect_json_value "$capture_file" analyzer_requests 0
    expect_no_provider_posts "$capture_file"
}

docker compose -p "$PROJECT_NAME" -f "$COMPOSE_FILE" up -d

wait_for_http "$BASE_URL/health/liveliness" "LiteLLM proxy"
docker compose -p "$PROJECT_NAME" -f "$COMPOSE_FILE" exec -T mock-upstream \
    python - <<'PY' >/dev/null
import urllib.request

with urllib.request.urlopen("http://127.0.0.1:8080/health", timeout=5):
    pass
PY

run_clean_case \
    "clean-chat" \
    "/v1/chat/completions" \
    '{"model":"mock-chat","messages":[{"role":"user","content":"Summarize the deployment plan."}]}' \
    '["/v1/chat/completions"]'

run_clean_case \
    "clean-responses" \
    "/v1/responses" \
    '{"model":"mock-chat","input":[{"role":"user","content":[{"type":"input_text","text":"Summarize the deployment plan."}]}]}' \
    '["/v1/responses"]'

run_clean_case \
    "clean-messages" \
    "/v1/messages" \
    '{"model":"mock-claude","max_tokens":16,"messages":[{"role":"user","content":"Summarize the deployment plan."}]}' \
    '["/v1/messages"]'

run_blocked_case \
    "blocked-chat-env" \
    "/v1/chat/completions" \
    '{"model":"mock-chat","messages":[{"role":"user","content":"API_KEY=sk-test-secret\nPASSWORD=local-password"}]}' \
    "config" \
    "env_secret_assignment" \
    "local-password"

run_blocked_case \
    "blocked-responses-env" \
    "/v1/responses" \
    '{"model":"mock-chat","input":"DATABASE_URL=postgresql://user:pass@db.example/app"}' \
    "config" \
    "env_secret_assignment" \
    "user:pass"

run_blocked_case \
    "blocked-messages-tool-result" \
    "/v1/messages" \
    '{"model":"mock-claude","max_tokens":16,"messages":[{"role":"user","content":[{"type":"tool_result","tool_use_id":"toolu_1","content":"API_KEY=sk-test-secret\nPASSWORD=local-password"}]}]}' \
    "config" \
    "env_secret_assignment" \
    "local-password"

echo "pre-egress proxy non-egress smoke passed"
