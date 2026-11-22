#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
export PATH="/Applications/Docker.app/Contents/Resources/bin:$PATH"
export PRE_EGRESS_PROXY_PORT="${REQUEST_ID_TEST_PORT:-14918}"
export PRE_EGRESS_PROXY_PII_MODE=mask
project="ru-guardrail-request-id-$$"
tmp_dir="$(mktemp -d)"
compose=(docker compose --project-name "$project"
    -f "$ROOT_DIR/tests/e2e/docker-compose.pre-egress-proxy.yml"
    -f "$ROOT_DIR/tests/e2e/docker-compose.request-id.yml")
checker=/workspace/tests/e2e/check_guardrail_request_id.py

cleanup() {
    "${compose[@]}" down -v >/dev/null 2>&1 || true
    rm -rf "$tmp_dir"
}
trap cleanup EXIT

run() {
    echo "Checking guardrail request IDs: $1"
    "${compose[@]}" exec -T mock-upstream python "$checker" "$1" >> "$tmp_dir/receipts.jsonl"
}

verify_audit() {
    "${compose[@]}" logs --no-color --no-log-prefix litellm > "$tmp_dir/audit.log"
    python3 "$ROOT_DIR/tests/e2e/check_guardrail_request_id.py" \
        verify-audit "$tmp_dir/receipts.jsonl" "$tmp_dir/audit.log"
}

redis_acl() {
    "${compose[@]}" exec -T redis redis-cli ACL SETUSER default "$1" | grep -qx OK
}

"${compose[@]}" up -d --wait --wait-timeout 120
run mask

redis_acl -setex
run mapping-save
redis_acl +setex

redis_acl -get
run mapping-load
redis_acl +get
verify_audit

export PRE_EGRESS_PROXY_PII_MODE=block
"${compose[@]}" up -d --wait --wait-timeout 120
"${compose[@]}" up -d --force-recreate --no-deps --wait nginx
: > "$tmp_dir/receipts.jsonl"
run block
verify_audit
