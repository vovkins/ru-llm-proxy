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
print(value)
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
import json
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

post_chat() {
    local payload="$1"
    local body_file="$2"

    curl -sS \
        --connect-timeout "$CURL_CONNECT_TIMEOUT" \
        --max-time "$CURL_MAX_TIME" \
        -o "$body_file" \
        -w "%{http_code}" \
        -H "Authorization: Bearer $MASTER_KEY" \
        -H "Content-Type: application/json" \
        -d "$payload" \
        "$BASE_URL/v1/chat/completions"
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
clean_status="$(post_chat '{"model":"mock-chat","messages":[{"role":"user","content":"Summarize the deployment plan."}]}' "$clean_body")"
if [ "$clean_status" != "200" ]; then
    echo "Expected clean prompt status 200, got $clean_status" >&2
    cat "$clean_body" >&2
    exit 1
fi

clean_capture="$tmp_dir/clean-capture.json"
capture_counts "$clean_capture"
if [ "$(json_get "$clean_capture" analyzer_requests)" != "1" ]; then
    echo "Expected clean prompt to reach analyzer once" >&2
    cat "$clean_capture" >&2
    exit 1
fi
if [ "$(json_get "$clean_capture" provider_requests)" != "1" ]; then
    echo "Expected clean prompt to reach provider once" >&2
    cat "$clean_capture" >&2
    exit 1
fi

reset_capture
blocked_body="$tmp_dir/blocked.json"
blocked_payload='{"model":"mock-chat","messages":[{"role":"user","content":"API_KEY=sk-test-secret\nPASSWORD=local-password"}]}'
blocked_status="$(post_chat "$blocked_payload" "$blocked_body")"
if [ "$blocked_status" != "422" ]; then
    echo "Expected blocked payload status 422, got $blocked_status" >&2
    cat "$blocked_body" >&2
    exit 1
fi
if [ "$(json_get "$blocked_body" error.code)" != "pre_egress_policy_blocked" ]; then
    echo "Expected pre_egress_policy_blocked error code" >&2
    cat "$blocked_body" >&2
    exit 1
fi
if grep -q "sk-test-secret\\|local-password" "$blocked_body"; then
    echo "Blocked response leaked raw secret value" >&2
    cat "$blocked_body" >&2
    exit 1
fi

blocked_capture="$tmp_dir/blocked-capture.json"
capture_counts "$blocked_capture"
if [ "$(json_get "$blocked_capture" analyzer_requests)" != "0" ]; then
    echo "Blocked payload unexpectedly reached analyzer" >&2
    cat "$blocked_capture" >&2
    exit 1
fi
if [ "$(json_get "$blocked_capture" provider_requests)" != "0" ]; then
    echo "Blocked payload unexpectedly reached provider" >&2
    cat "$blocked_capture" >&2
    exit 1
fi

echo "pre-egress proxy non-egress smoke passed"
