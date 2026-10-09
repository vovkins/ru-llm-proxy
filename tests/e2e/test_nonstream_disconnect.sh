#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
PROJECT="ru-llm-proxy-disconnect-$$"
TEMP_DIR="$(mktemp -d)"
export DISCONNECT_CONFIG="$TEMP_DIR/config.yaml"
export PRE_EGRESS_PROXY_PORT=127.0.0.1:0
COMPOSE=(docker compose -p "$PROJECT" -f "$ROOT_DIR/tests/e2e/docker-compose.pre-egress-proxy.yml" -f "$ROOT_DIR/tests/e2e/docker-compose.disconnect.yml")
cleanup() {
    "${COMPOSE[@]}" down -v --remove-orphans >/dev/null 2>&1 || true
    rm -rf "$TEMP_DIR"
}
trap cleanup EXIT

variants=(false true)
case "${1:-}" in
    "") ;;
    --stream-only) variants=(true) ;;
    *) echo "Usage: $0 [--stream-only]" >&2; exit 2 ;;
esac
for cancellation in "${variants[@]}"; do
    python3 - "$DISCONNECT_CONFIG" "$cancellation" <<'PY'
import json
import pathlib
import sys

output, enabled = sys.argv[1:]
config = {
    "model_list": [
        {"model_name": name, "litellm_params": {
            "model": "openai/mock-chat", "api_base": "http://mock-upstream:8080/v1",
            "api_key": "os.environ/OPENAI_API_KEY", "max_retries": 0,
            "timeout": 1 if name == "mock-timeout" else 30,
        }} for name in ("mock-chat", "mock-timeout")
    ],
    "guardrails": [
        {"guardrail_name": f"ru-pii-mask-{mode}", "litellm_params": {
            "guardrail": "disconnect_observer.ObservedGuardrail",
            "mode": f"{mode}_call", "default_on": True,
        }} for mode in ("pre", "post")
    ],
    "general_settings": {"master_key": "os.environ/LITELLM_MASTER_KEY",
                         "cancel_on_disconnect": enabled == "true"},
    "router_settings": {"num_retries": 1, "retry_after": 0, "timeout": 30},
    "litellm_settings": {"callbacks": ["litellm_guardrails.upstream_stream_adapter"]},
}
pathlib.Path(output).write_text(json.dumps(config))
PY
    "${COMPOSE[@]}" up -d --wait --wait-timeout 120
    for attempt in $(seq 1 90); do
        if "${COMPOSE[@]}" exec -T litellm python -c \
            "import urllib.request; urllib.request.urlopen('http://localhost:4000/health/liveliness', timeout=2)" >/dev/null 2>&1; then
            break
        fi
        if [ "$attempt" = 90 ]; then
            "${COMPOSE[@]}" logs litellm
            exit 1
        fi
        sleep 1
    done
    if [ "$cancellation" = false ]; then
        "${COMPOSE[@]}" exec -T litellm python /workspace/tests/e2e/check_nonstream_disconnect.py --baseline
    else
        "${COMPOSE[@]}" exec -T litellm python /workspace/tests/e2e/check_preprocessing_budget.py
        "${COMPOSE[@]}" exec -T litellm python /workspace/tests/e2e/check_upstream_transport.py
        if [ "${1:-}" != --stream-only ]; then
            "${COMPOSE[@]}" exec -T litellm python /workspace/tests/e2e/check_nonstream_disconnect.py
        fi
        stream_status=0
        "${COMPOSE[@]}" exec -T litellm python /workspace/tests/e2e/check_stream_disconnect.py || stream_status=$?
        "${COMPOSE[@]}" stop -t 15 litellm
    fi
    "${COMPOSE[@]}" logs litellm > "$TEMP_DIR/litellm.log"
    if grep -Fq '+79031234567' "$TEMP_DIR/litellm.log"; then
        echo "Sensitive value in proxy logs" >&2
        exit 1
    fi
    if [ "$cancellation" = true ] && [ "${1:-}" != --stream-only ]; then
        grep -Fq 'pii_guardrail_failure_cleanup_failed' "$TEMP_DIR/litellm.log"
    fi
    if [ "$cancellation" = true ]; then
        grep -Fq 'upstream_http_pool_closed' "$TEMP_DIR/litellm.log"
    fi
    if [ "${stream_status:-0}" != 0 ]; then
        exit "$stream_status"
    fi
    "${COMPOSE[@]}" down -v --remove-orphans
done
