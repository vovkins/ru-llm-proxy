#!/usr/bin/env bash
set -euo pipefail

env_file="${1:-.env}"

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

port="$(get_env_value CODEX_LB_PORT || true)"
port="${port:-2455}"
base_url="http://127.0.0.1:${port}"
compose=(
    docker compose
    --env-file "$env_file"
    -f docker-compose.yml
    -f docker-compose.codex-lb.yml
)

echo "🚀 Запуск компонентов первичной настройки codex-lb"
if ! "${compose[@]}" up -d --no-build codex-lb-db codex-lb nginx; then
    echo "❌ Не удалось запустить компоненты настройки. Сначала выполните make build." >&2
    exit 1
fi

echo "⏳ Ожидание готовности codex-lb"
ready=0
for _ in $(seq 1 60); do
    if curl -fsS --max-time 5 "${base_url}/health/ready" >/dev/null 2>&1; then
        ready=1
        break
    fi
    sleep 2
done

if [ "$ready" -ne 1 ]; then
    echo "❌ codex-lb не перешёл в состояние готовности. Проверьте make logs STACK=litellm-presidio-codex-lb." >&2
    exit 1
fi

echo "Панель codex-lb: ${base_url}/dashboard"
python3 scripts/setup_codex_lb.py --env-file "$env_file" --base-url "$base_url"
echo "✅ Расширенный состав настроен. Следующий шаг: make up STACK=litellm-presidio-codex-lb"
