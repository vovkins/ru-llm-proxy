#!/usr/bin/env bash
set -euo pipefail

stack="${1:-}"
env_file="${2:-.env}"
wait_seconds="${3:-0}"
core_stack="litellm-presidio"
codex_lb_stack="litellm-presidio-codex-lb"

if ! [[ "$wait_seconds" =~ ^[0-9]+$ ]]; then
    printf '❌ Некорректное время ожидания: %s\n' "$wait_seconds" >&2
    exit 2
fi

get_env_value() {
    local key="$1"
    awk -v key="$key" '
        index($0, key "=") == 1 {
            sub("^[^=]*=", "")
            print
            exit
        }
    ' "$env_file"
}

case "$stack" in
    "$core_stack")
        compose=(docker compose --env-file "$env_file" -f docker-compose.yml)
        ;;
    "$codex_lb_stack")
        compose=(docker compose --env-file "$env_file" -f docker-compose.yml -f docker-compose.codex-lb.yml)
        ;;
    *)
        printf '❌ Неизвестный состав: %s\n' "${stack:-<не указан>}" >&2
        exit 2
        ;;
esac

failures=0
nginx_port="$(get_env_value NGINX_HTTP_PORT || true)"
litellm_port="$(get_env_value LITELLM_PORT || true)"
codex_lb_port="$(get_env_value CODEX_LB_PORT || true)"
nginx_port="${nginx_port:-80}"
litellm_port="${litellm_port:-4000}"
codex_lb_port="${codex_lb_port:-2455}"

if [ "$wait_seconds" -gt 0 ]; then
    elapsed=0
    while ! curl -fsS --max-time 2 \
        "http://127.0.0.1:${litellm_port}/health/liveliness" \
        >/dev/null 2>&1; do
        if [ "$elapsed" -ge "$wait_seconds" ]; then
            break
        fi
        sleep 2
        elapsed=$((elapsed + 2))
    done
fi

check_url() {
    local label="$1"
    local url="$2"
    printf '%-22s' "${label}:"
    if curl -fsS --max-time 10 "$url" >/dev/null 2>&1; then
        echo "✅ OK"
    else
        echo "❌ DOWN"
        failures=$((failures + 1))
    fi
}

check_exec() {
    local label="$1"
    shift
    printf '%-22s' "${label}:"
    if "$@" >/dev/null 2>&1; then
        echo "✅ OK"
    else
        echo "❌ DOWN"
        failures=$((failures + 1))
    fi
}

echo "=== Состав: ${stack} ==="
check_url "Nginx" "http://127.0.0.1:${nginx_port}/nginx-health"
check_url "LiteLLM Proxy" "http://127.0.0.1:${litellm_port}/health/liveliness"
check_url "Presidio Analyzer" "http://127.0.0.1:5001/api/v1/health"
check_exec "PostgreSQL LiteLLM" "${compose[@]}" exec -T db pg_isready -U litellm
check_exec "Redis" "${compose[@]}" exec -T redis redis-cli ping

if [ "$stack" = "$codex_lb_stack" ]; then
    check_url "codex-lb" "http://127.0.0.1:${codex_lb_port}/health/ready"
    check_exec "PostgreSQL codex-lb" "${compose[@]}" exec -T codex-lb-db pg_isready -U codex_lb -d codex_lb
fi

echo ""
if [ "$failures" -ne 0 ]; then
    printf '❌ Не прошли проверки: %d\n' "$failures" >&2
    exit 1
fi

echo "✅ Все компоненты выбранного состава готовы"
