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

assert_regulated_topic_blocked_error() {
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
        policy_param = error["param"].get("regulated_topic_policy")
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
    "message": "Request contains regulated internal compliance content and was blocked by regulated-topic policy.",
    "type": "regulated_topic_policy_violation",
    "code": "regulated_topic_policy_blocked",
    "details": {
        "categories": [sys.argv[2]],
        "rules": [sys.argv[3]],
        "actions": ["block"],
    },
}
for key in ("message", "type", "code"):
    if error.get(key) != expected[key]:
        print(f"Expected error.{key}={expected[key]!r}, got {error.get(key)!r}", file=sys.stderr)
        print(json.dumps(body, ensure_ascii=False), file=sys.stderr)
        sys.exit(1)

if details != expected["details"]:
    print("Expected structured regulated-topic details", file=sys.stderr)
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

post_json_with_headers() {
    local path="$1"
    local payload="$2"
    local body_file="$3"
    local headers_file="$4"

    curl -sS \
        --connect-timeout "$CURL_CONNECT_TIMEOUT" \
        --max-time "$CURL_MAX_TIME" \
        -D "$headers_file" \
        -o "$body_file" \
        -w "%{http_code}" \
        -H "Authorization: Bearer $MASTER_KEY" \
        -H "Content-Type: application/json" \
        -d "$payload" \
        "$BASE_URL$path"
}

set_analyzer_overload() {
    local reason="$1"
    local retry_after="$2"
    docker compose -p "$PROJECT_NAME" -f "$COMPOSE_FILE" exec -T mock-upstream \
        python - "$reason" "$retry_after" <<'PY' >/dev/null
import json
import sys
import urllib.request

payload = json.dumps(
    {"reason": sys.argv[1], "retry_after_seconds": int(sys.argv[2])}
).encode("utf-8")
request = urllib.request.Request(
    "http://127.0.0.1:8080/analyzer/overload",
    data=payload,
    headers={"Content-Type": "application/json"},
    method="POST",
)
with urllib.request.urlopen(request, timeout=5):
    pass
PY
}

recover_analyzer() {
    docker compose -p "$PROJECT_NAME" -f "$COMPOSE_FILE" exec -T mock-upstream \
        python - <<'PY' >/dev/null
import urllib.request

request = urllib.request.Request(
    "http://127.0.0.1:8080/analyzer/recover",
    data=b"{}",
    method="POST",
)
with urllib.request.urlopen(request, timeout=5):
    pass
PY
}

assert_analyzer_overload_response() {
    local body_file="$1"
    local headers_file="$2"
    local expected_reason="$3"
    local expected_retry_after="$4"

    python3 - "$body_file" "$headers_file" "$expected_reason" "$expected_retry_after" <<'PY'
import json
import sys

body_path, headers_path, expected_reason, expected_retry_after = sys.argv[1:]
with open(body_path, encoding="utf-8") as stream:
    body = json.load(stream)
with open(headers_path, encoding="iso-8859-1") as stream:
    headers = stream.read().lower()

def find_overload(value):
    if isinstance(value, dict):
        if value.get("code") == "analyzer_overloaded":
            return value
        for child in value.values():
            found = find_overload(child)
            if found is not None:
                return found
    elif isinstance(value, list):
        for child in value:
            found = find_overload(child)
            if found is not None:
                return found
    return None

error = find_overload(body)
if error is None:
    raise SystemExit(f"structured analyzer_overloaded error is missing: {body!r}")
details = error.get("details")
if not isinstance(details, dict) or details.get("reason") != expected_reason:
    raise SystemExit(f"unexpected overload details: {body!r}")
if details.get("retry_after_seconds") != int(expected_retry_after):
    raise SystemExit(f"unexpected retry delay in body: {body!r}")
if f"retry-after: {expected_retry_after}\n" not in headers:
    raise SystemExit(f"Retry-After header is missing: {headers!r}")
PY
}

expect_no_pii_mappings() {
    local keys
    keys="$(
        docker compose -p "$PROJECT_NAME" -f "$COMPOSE_FILE" exec -T redis \
            redis-cli --raw --scan --pattern 'pii_mapping:*'
    )"
    if [ -n "$keys" ]; then
        echo "Unexpected pii_mapping keys after Analyzer overload: $keys" >&2
        exit 1
    fi
}

expect_no_provider_posts() {
    local file="$1"

    expect_json_value "$file" provider_requests 0
    expect_json_value "$file" provider_request_paths '[]'
}

assert_litellm_logs_do_not_contain() {
    local forbidden="$1"
    local logs_file="$tmp_dir/litellm-logs.txt"

    docker compose -p "$PROJECT_NAME" -f "$COMPOSE_FILE" logs --no-color litellm >"$logs_file"
    if grep -Fq -- "$forbidden" "$logs_file"; then
        echo "LiteLLM logs leaked forbidden value: $forbidden" >&2
        exit 1
    fi
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
    assert_litellm_logs_do_not_contain "$forbidden"
}

run_regulated_topic_blocked_case() {
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

    assert_regulated_topic_blocked_error "$body_file" "$expected_category" "$expected_rule"
    if grep -Fq -- "$forbidden" "$body_file"; then
        echo "Blocked response leaked raw regulated-topic value for $name" >&2
        cat "$body_file" >&2
        exit 1
    fi

    capture_counts "$capture_file"
    expect_json_value "$capture_file" analyzer_requests 0
    expect_no_provider_posts "$capture_file"
    assert_litellm_logs_do_not_contain "$forbidden"
}

run_analyzer_overload_case() {
    local reason="$1"
    local retry_after="$2"
    local probe="$3"
    local body_file="$tmp_dir/overload-${reason}.json"
    local headers_file="$tmp_dir/overload-${reason}.headers"
    local capture_file="$tmp_dir/overload-${reason}-capture.json"
    local status

    reset_capture
    set_analyzer_overload "$reason" "$retry_after"
    status="$(post_json_with_headers \
        "/v1/chat/completions" \
        "{\"model\":\"mock-chat\",\"messages\":[{\"role\":\"user\",\"content\":\"$probe\"}]}" \
        "$body_file" \
        "$headers_file")"
    if [ "$status" != "503" ]; then
        echo "Expected Analyzer overload status 503, got $status" >&2
        cat "$body_file" >&2
        exit 1
    fi
    assert_analyzer_overload_response \
        "$body_file" "$headers_file" "$reason" "$retry_after"
    capture_counts "$capture_file"
    expect_json_value "$capture_file" analyzer_requests 1
    expect_no_provider_posts "$capture_file"
    expect_no_pii_mappings
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
    '{"model":"mock-chat","input":[{"role":"user","content":[{"type":"input_text","text":"Summarize the release plan."}]}]}' \
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
    "blocked-messages-system" \
    "/v1/messages" \
    '{"model":"mock-claude","max_tokens":16,"system":"API_KEY=sk-system-secret\nPASSWORD=local-password","messages":[{"role":"user","content":"Summarize the deployment plan."}]}' \
    "config" \
    "env_secret_assignment" \
    "local-password"

run_blocked_case \
    "blocked-messages-tool-result" \
    "/v1/messages" \
    '{"model":"mock-claude","max_tokens":16,"messages":[{"role":"user","content":[{"type":"tool_result","tool_use_id":"toolu_1","content":"API_KEY=sk-test-secret\nPASSWORD=local-password"}]}]}' \
    "config" \
    "env_secret_assignment" \
    "local-password"

run_blocked_case \
    "blocked-chat-access-log" \
    "/v1/chat/completions" \
    '{"model":"mock-chat","messages":[{"role":"user","content":"10.0.0.5 - - [10/Jul/2026:10:00:00 +0000] \"POST /internal/secret HTTP/1.1\" 401 123 \"-\" \"curl/8.0\""}]}' \
    "log" \
    "log_or_stacktrace_payload" \
    "/internal/secret"

run_regulated_topic_blocked_case \
    "blocked-chat-regulated-topic" \
    "/v1/chat/completions" \
    '{"model":"mock-chat","messages":[{"role":"user","content":"Describe internal sanctions screening watchlist matching logic and threshold settings."}]}' \
    "sanctions_screening" \
    "sanctions_watchlist_matching" \
    "watchlist matching logic"

run_analyzer_overload_case "queue_full" 2 "Capacity probe alpha."
run_analyzer_overload_case "queue_timeout" 3 "Capacity probe beta."

recover_analyzer
run_clean_case \
    "clean-after-overload" \
    "/v1/chat/completions" \
    '{"model":"mock-chat","messages":[{"role":"user","content":"Capacity recovery probe."}]}' \
    '["/v1/chat/completions"]'

echo "pre-egress proxy non-egress smoke passed"
