#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
COMPOSE_FILE="$ROOT_DIR/tests/e2e/docker-compose.ner-proxy.yml"
PROJECT_NAME="${NER_PROXY_PROJECT:-ru-llm-proxy-ner-$$-${RANDOM}}"
MASK_PORT="${NER_PROXY_MASK_PORT:-14010}"
BLOCK_PORT="${NER_PROXY_BLOCK_PORT:-14011}"
ANALYZER_PORT="${NER_PROXY_ANALYZER_PORT:-15001}"
MASTER_KEY="sk-test-master"
CURL_CONNECT_TIMEOUT="${CURL_CONNECT_TIMEOUT:-2}"
CURL_MAX_TIME="${CURL_MAX_TIME:-30}"

IDENTITY_TEXT="Клиент Олег Волков, login=oleg.volkov, пароль=Mix3d-Value!, договор OV-2026/81."
ORG_TEXT="Для ООО Север в Туле заданы AUTH_TOKEN=mixed-auth-token-00073 и SECRET_KEY=mixed-secret-key-00084."
IDENTITY_VALUES=("Олег Волков" "oleg.volkov" "Mix3d-Value!" "OV-2026/81")
ORG_VALUES=("ООО Север" "Туле" "mixed-auth-token-00073" "mixed-secret-key-00084")
export NER_PROXY_CANARIES="Олег Волков,oleg.volkov,Mix3d-Value!,OV-2026/81,ООО Север,Туле,mixed-auth-token-00073,mixed-secret-key-00084"
export NER_PROXY_MASK_PORT="$MASK_PORT"
export NER_PROXY_BLOCK_PORT="$BLOCK_PORT"
export NER_PROXY_ANALYZER_PORT="$ANALYZER_PORT"

tmp_dir="$(mktemp -d)"
cleanup() {
    docker compose -p "$PROJECT_NAME" -f "$COMPOSE_FILE" down -v --remove-orphans >/dev/null 2>&1 || true
    rm -rf "$tmp_dir"
}
trap cleanup EXIT

wait_for_http() {
    local url="$1"
    local description="$2"
    local attempts="${3:-120}"

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
    docker compose -p "$PROJECT_NAME" -f "$COMPOSE_FILE" ps >&2 || true
    return 1
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

capture_to() {
    local output_file="$1"
    docker compose -p "$PROJECT_NAME" -f "$COMPOSE_FILE" exec -T mock-upstream \
        python - <<'PY' >"$output_file"
import urllib.request

with urllib.request.urlopen("http://127.0.0.1:8080/capture", timeout=5) as response:
    print(response.read().decode("utf-8"))
PY
}

expect_capture() {
    local file="$1"
    local key="$2"
    local expected="$3"
    python3 - "$file" "$key" "$expected" <<'PY'
import json
import sys

with open(sys.argv[1], encoding="utf-8") as stream:
    payload = json.load(stream)
actual = payload[sys.argv[2]]
expected_raw = sys.argv[3]
expected = {"true": True, "false": False}.get(expected_raw, int(expected_raw) if expected_raw.isdigit() else expected_raw)
if actual != expected:
    raise SystemExit(f"expected {sys.argv[2]}={expected!r}, got {actual!r}: {payload!r}")
PY
}

post_chat() {
    local port="$1"
    local text="$2"
    local output_file="$3"
    local payload_file="$tmp_dir/payload.json"
    python3 - "$text" "$payload_file" <<'PY'
import json
import sys

payload = {
    "model": "mock-chat",
    "messages": [{"role": "user", "content": sys.argv[1]}],
    "max_tokens": 128,
}
with open(sys.argv[2], "w", encoding="utf-8") as stream:
    json.dump(payload, stream, ensure_ascii=False)
PY
    curl -sS \
        --connect-timeout "$CURL_CONNECT_TIMEOUT" \
        --max-time "$CURL_MAX_TIME" \
        -o "$output_file" \
        -w "%{http_code}" \
        -H "Authorization: Bearer $MASTER_KEY" \
        -H "Content-Type: application/json" \
        --data-binary "@$payload_file" \
        "http://127.0.0.1:${port}/v1/chat/completions"
}

expect_restored_response() {
    local file="$1"
    shift
    python3 - "$file" "$@" <<'PY'
import json
import sys

with open(sys.argv[1], encoding="utf-8") as stream:
    payload = json.load(stream)
content = payload["choices"][0]["message"]["content"]
missing = [value for value in sys.argv[2:] if value not in content]
if missing:
    raise SystemExit(f"restored response is missing {missing!r}: {payload!r}")
if "<" in content and "_1>" in content:
    raise SystemExit(f"response still contains a PII placeholder: {content!r}")
PY
}

expect_safe_block_response() {
    local file="$1"
    shift
    python3 - "$file" "$@" <<'PY'
import json
import sys

with open(sys.argv[1], encoding="utf-8") as stream:
    payload = json.load(stream)
serialized = json.dumps(payload, ensure_ascii=False)
leaked = [value for value in sys.argv[2:] if value in serialized]
if leaked:
    raise SystemExit(f"block response leaked raw values {leaked!r}: {payload!r}")
PY
}

expect_metric_at_least() {
    local file="$1"
    local metric="$2"
    local minimum="$3"
    local label_name="${4:-}"
    local label_value="${5:-}"
    python3 - "$file" "$metric" "$minimum" "$label_name" "$label_value" <<'PY'
import re
import sys

path, expected_metric, minimum_raw, label_name, label_value = sys.argv[1:]
minimum = float(minimum_raw)
values = []
with open(path, encoding="utf-8") as stream:
    for raw_line in stream:
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        try:
            selector, raw_value = line.rsplit(None, 1)
            value = float(raw_value)
        except ValueError:
            continue
        metric_name = selector.split("{", 1)[0]
        if metric_name != expected_metric:
            continue
        labels = dict(
            re.findall(r'([a-zA-Z_][a-zA-Z0-9_]*)="((?:\\.|[^"])*)"', selector)
        )
        if label_name and labels.get(label_name) != label_value:
            continue
        values.append(value)

if not values or max(values) < minimum:
    raise SystemExit(
        f"expected {expected_metric} >= {minimum} "
        f"for {label_name}={label_value}, got {values!r}"
    )
PY
}

if ! docker image inspect "${ANALYZER_IMAGE:-ru-llm-proxy-presidio-analyzer:latest}" >/dev/null 2>&1; then
    echo "Analyzer image is missing; run 'make test-hf-model' first" >&2
    exit 1
fi

docker compose -p "$PROJECT_NAME" -f "$COMPOSE_FILE" up -d

wait_for_http "http://127.0.0.1:${ANALYZER_PORT}/api/v1/health" "Analyzer" 180
wait_for_http "http://127.0.0.1:${MASK_PORT}/health/liveliness" "mask proxy"
wait_for_http "http://127.0.0.1:${BLOCK_PORT}/health/liveliness" "block proxy"

health_file="$tmp_dir/analyzer-health.json"
curl -fsS "http://127.0.0.1:${ANALYZER_PORT}/api/v1/health" >"$health_file"
python3 - "$health_file" <<'PY'
import json
import sys

with open(sys.argv[1], encoding="utf-8") as stream:
    health = json.load(stream)
expected = {"status": "ok", "ner_state": "ready", "ner_warmed_up": True}
for key, value in expected.items():
    if health.get(key) != value:
        raise SystemExit(f"unexpected Analyzer health: {health!r}")
PY

run_mask_case() {
    local name="$1"
    local text="$2"
    shift 2
    local values=("$@")
    local response_file="$tmp_dir/${name}-response.json"
    local capture_file="$tmp_dir/${name}-capture.json"
    local status

    reset_capture
    status="$(post_chat "$MASK_PORT" "$text" "$response_file")"
    if [ "$status" != "200" ]; then
        echo "Expected mask request status 200, got $status" >&2
        cat "$response_file" >&2
        exit 1
    fi
    expect_restored_response "$response_file" "${values[@]}"
    capture_to "$capture_file"
    expect_capture "$capture_file" provider_requests 1
    expect_capture "$capture_file" provider_saw_canary false
    expect_capture "$capture_file" provider_saw_pii_placeholder true
}

run_mask_case identity "$IDENTITY_TEXT" "${IDENTITY_VALUES[@]}"
run_mask_case organization "$ORG_TEXT" "${ORG_VALUES[@]}"

reset_capture
block_response="$tmp_dir/block-response.json"
block_status="$(post_chat "$BLOCK_PORT" "$IDENTITY_TEXT" "$block_response")"
if [ "$block_status" != "422" ]; then
    echo "Expected block request status 422, got $block_status" >&2
    cat "$block_response" >&2
    exit 1
fi
expect_safe_block_response "$block_response" "${IDENTITY_VALUES[@]}"
block_capture="$tmp_dir/block-capture.json"
capture_to "$block_capture"
expect_capture "$block_capture" provider_requests 0
expect_capture "$block_capture" provider_saw_canary false

mask_metrics="$tmp_dir/mask-metrics.txt"
block_metrics="$tmp_dir/block-metrics.txt"
curl -fsS "http://127.0.0.1:${MASK_PORT}/metrics/" >"$mask_metrics"
curl -fsS "http://127.0.0.1:${BLOCK_PORT}/metrics/" >"$block_metrics"
expect_metric_at_least "$mask_metrics" ru_pii_guardrail_pre_calls_total 2 result masked
expect_metric_at_least "$mask_metrics" ru_pii_guardrail_post_calls_total 2 result restored
expect_metric_at_least "$mask_metrics" ru_pii_guardrail_analyzer_latency_seconds_count 2
expect_metric_at_least "$block_metrics" ru_pii_guardrail_pre_calls_total 1 result blocked
expect_metric_at_least "$block_metrics" ru_pii_guardrail_blocked_total 1 entity_type PERSON
expect_metric_at_least "$block_metrics" ru_pii_guardrail_analyzer_latency_seconds_count 1

proxy_logs="$tmp_dir/proxy-logs.txt"
docker compose -p "$PROJECT_NAME" -f "$COMPOSE_FILE" logs \
    litellm-mask litellm-block >"$proxy_logs"
python3 - "$proxy_logs" <<'PY'
import sys

with open(sys.argv[1], encoding="utf-8") as stream:
    logs = stream.read()
for forbidden in (
    "Prometheus metric already registered, using no-op",
    "Prometheus metric registration conflict",
):
    if forbidden in logs:
        raise SystemExit(f"unexpected metric registration failure: {forbidden}")
PY

echo "Real Analyzer proxy flow passed: mask/block and guardrail metrics verified"
