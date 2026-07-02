#!/usr/bin/env bash
# Generate and maintain local .env secrets for ru-llm-proxy.
set -euo pipefail

ENV_FILE="${1:-.env}"
EXAMPLE_FILE="${2:-.env.example}"

random_urlsafe() {
    local bytes="${1:-48}"
    openssl rand -base64 "$bytes" | tr '+/' '-_' | tr -d '=\n'
}

get_env_value() {
    local key="$1"
    awk -v key="$key" '
        index($0, key "=") == 1 {
            sub("^[^=]*=", "")
            print
            exit
        }
    ' "$ENV_FILE"
}

set_env_value() {
    local key="$1"
    local value="$2"
    local escaped="$value"
    escaped="${escaped//\\/\\\\}"
    escaped="${escaped//&/\\&}"
    escaped="${escaped//|/\\|}"

    if grep -q "^${key}=" "$ENV_FILE"; then
        sed -i.bak -e "s|^${key}=.*|${key}=${escaped}|" "$ENV_FILE"
        rm -f "${ENV_FILE}.bak"
    else
        if [ -s "$ENV_FILE" ] && [ -n "$(tail -c 1 "$ENV_FILE")" ]; then
            printf "\n" >> "$ENV_FILE"
        fi
        printf "%s=%s\n" "$key" "$value" >> "$ENV_FILE"
    fi
}

ensure_secret() {
    local key="$1"
    local generated="$2"
    shift 2

    local current
    current="$(get_env_value "$key" || true)"

    if [ -z "$current" ]; then
        set_env_value "$key" "$generated"
        return 0
    fi

    local placeholder
    for placeholder in "$@"; do
        if [ "$current" = "$placeholder" ]; then
            set_env_value "$key" "$generated"
            return 0
        fi
    done

    return 1
}

ensure_key_exists() {
    local key="$1"
    local value="${2:-}"

    if ! grep -q "^${key}=" "$ENV_FILE"; then
        set_env_value "$key" "$value"
    fi
}

if [ ! -f "$ENV_FILE" ]; then
    echo "Создание ${ENV_FILE} из шаблона..."
    cp "$EXAMPLE_FILE" "$ENV_FILE"
fi

echo "Проверка локальных секретов..."

master_key="sk-ru-$(random_urlsafe 48)"
salt_key="$(random_urlsafe 48)"
db_password="$(random_urlsafe 32)"
ui_password="$(random_urlsafe 32)"

ensure_secret "LITELLM_MASTER_KEY" "$master_key" "sk-replace-with-generated-key" "***" || true
ensure_secret "LITELLM_SALT_KEY" "$salt_key" "replace-with-generated-salt" "***" || true

db_password_changed=0
if ensure_secret "POSTGRES_PASSWORD" "$db_password" "***"; then
    db_password_changed=1
fi

ensure_secret "UI_USERNAME" "admin" "replace-with-generated-ui-username" "***" || true
ensure_secret "UI_PASSWORD" "$ui_password" "replace-with-generated-ui-password" "***" || true
ensure_key_exists "ZAI_API_KEY_2" ""
ensure_key_exists "LITELLM_ROUTING_TEST_KEY" ""
ensure_key_exists "PRESIDIO_ANALYZER_WORKERS" "1"
ensure_key_exists "PRESIDIO_ANALYZER_CONCURRENCY_LIMIT" "1"
ensure_key_exists "PRESIDIO_ANALYZER_QUEUE_LIMIT" "8"
ensure_key_exists "PRESIDIO_ANALYZER_QUEUE_TIMEOUT_SECONDS" "0.25"
ensure_key_exists "PRESIDIO_ANALYZER_DETECT_BARE_INN_BY_CHECKSUM" "true"
ensure_key_exists "PII_GUARDRAIL_MODE" "mask"
ensure_key_exists "PRE_EGRESS_POLICY_MODE" "block"
ensure_key_exists "FINAL_PAYLOAD_LEAK_CHECK_MODE" "block"
ensure_key_exists "FINAL_PAYLOAD_LEAK_CHECK_CANARIES" ""
ensure_key_exists "PII_GUARDRAIL_FAILURE_MODE" "fail_open"
ensure_key_exists "PII_MAPPING_TTL_SECONDS" "3600"
ensure_key_exists "PII_GUARDRAIL_REDIS_MAX_CONNECTIONS" "20"
ensure_key_exists "PII_GUARDRAIL_REDIS_SOCKET_CONNECT_TIMEOUT_SECONDS" "1.0"
ensure_key_exists "PII_GUARDRAIL_REDIS_SOCKET_TIMEOUT_SECONDS" "2.0"
ensure_key_exists "PII_GUARDRAIL_ANALYZER_TIMEOUT_SECONDS" "30.0"
ensure_key_exists "PII_GUARDRAIL_ANALYZER_CONNECT_TIMEOUT_SECONDS" "5.0"
ensure_key_exists "PII_GUARDRAIL_ANALYZER_MAX_CONNECTIONS" "20"
ensure_key_exists "PII_GUARDRAIL_ANALYZER_MAX_KEEPALIVE_CONNECTIONS" "10"
ensure_key_exists "RESPONSES_MODEL" ""
ensure_key_exists "MESSAGES_MODEL" ""

actual_db_password="$(get_env_value "POSTGRES_PASSWORD" || true)"
current_db_url="$(get_env_value "LITELLM_DB_URL" || true)"
if [ "$db_password_changed" = "1" ] || [ -z "$current_db_url" ] || [ "$current_db_url" = "postgresql://litellm:litellm@db:5432/litellm" ]; then
    set_env_value "LITELLM_DB_URL" "postgresql://litellm:${actual_db_password}@db:5432/litellm"
fi

echo ""
echo "✅ ${ENV_FILE} готов"
echo "⚠️  Заполните API-ключ основного провайдера в ${ENV_FILE}:"
echo "   ZAI_API_KEY=***"
echo "   ZAI_API_KEY_2=...  # опционально, второй аккаунт/deployment для sticky routing"
echo ""
echo "LiteLLM Admin UI будет доступен по адресу /ui."
echo "Логин и пароль сохранены в ${ENV_FILE}: UI_USERNAME и UI_PASSWORD."
