#!/usr/bin/env bash
set -euo pipefail

operation="${1:-}"
stack="${2:-}"
env_file="${3:-.env}"

core_stack="litellm-presidio"
codex_lb_stack="litellm-presidio-codex-lb"

fail() {
    printf '❌ %s\n' "$1" >&2
    exit 2
}

validate_stack() {
    case "$stack" in
        "$core_stack"|"$codex_lb_stack") ;;
        *) fail "Неизвестный состав: ${stack:-<не указан>}" ;;
    esac
}

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

require_env_value() {
    local key="$1"
    local value
    value="$(get_env_value "$key" || true)"
    case "$value" in
        ""|"***"|sk-replace-with-generated-key|replace-with-generated-salt|replace-with-generated-ui-password)
            fail "В ${env_file} не настроена переменная ${key}. Выполните make setup STACK=${stack}."
            ;;
    esac
}

container_is_running() {
    docker inspect --format '{{.State.Running}}' "$1" 2>/dev/null | grep -qx true
}

validate_stack

case "$operation" in
    mutation)
        if [ "$stack" = "$core_stack" ] && container_is_running ru-llm-proxy-codex-lb; then
            fail "Сейчас запущен расширенный состав. Используйте STACK=${codex_lb_stack}, чтобы не оставить его контейнеры без управления."
        fi
        ;;
    preflight)
        [ -f "$env_file" ] || fail "Файл ${env_file} не найден. Выполните make setup STACK=${stack}."
        require_env_value LITELLM_MASTER_KEY
        require_env_value LITELLM_SALT_KEY
        require_env_value POSTGRES_PASSWORD
        require_env_value UI_PASSWORD
        if [ "$stack" = "$codex_lb_stack" ]; then
            require_env_value CODEX_LB_POSTGRES_PASSWORD
            require_env_value CODEX_LB_API_KEY
        fi
        ;;
    *)
        fail "Неизвестная операция проверки: ${operation:-<не указана>}"
        ;;
esac
