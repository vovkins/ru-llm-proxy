#!/usr/bin/env bash
# Create a LiteLLM virtual key for clients of ru-llm-proxy.
set -euo pipefail

BASE_URL="${LITELLM_URL:-http://localhost:4000}"
ENV_FILE=".env"
KEY_ALIAS="${KEY_ALIAS:-client-local}"
MODELS="${MODELS:-standard}"
DURATION="${DURATION:-}"
BUDGET_DURATION="${BUDGET_DURATION:-}"
MAX_BUDGET="${MAX_BUDGET:-}"
RPM_LIMIT="${RPM_LIMIT:-}"
TPM_LIMIT="${TPM_LIMIT:-}"
USER_ID="${USER_ID:-}"
TEAM_ID="${TEAM_ID:-}"
METADATA_JSON="${METADATA_JSON:-{}}"

usage() {
    cat <<'EOF'
Usage: scripts/create_virtual_key.sh [options]

Creates a LiteLLM virtual key for clients. This key is for proxy access only;
it is not an upstream OpenAI, Anthropic, or Z.AI provider key.

Options:
  --base-url URL            LiteLLM proxy URL (default: http://localhost:4000)
  --env-file PATH           env file to load (default: .env)
  --alias NAME              key_alias value (default: client-local)
  --models LIST             comma-separated model aliases/access groups (default: standard)
  --duration DURATION       key validity, e.g. 30d or 12h
  --budget AMOUNT           max_budget for the key
  --budget-duration VALUE   budget reset duration, e.g. 30d
  --rpm-limit VALUE         per-key requests-per-minute limit
  --tpm-limit VALUE         per-key tokens-per-minute limit
  --user-id VALUE           LiteLLM user_id to attach
  --team-id VALUE           LiteLLM team_id to attach
  --metadata-json JSON      metadata object to attach
  -h, --help                show this help

Required:
  LITELLM_MASTER_KEY must be set in the environment or loaded from --env-file.

Example:
  scripts/create_virtual_key.sh \
    --alias codex-local \
    --models standard,openai \
    --duration 30d \
    --budget 50 \
    --metadata-json '{"owner":"platform","purpose":"local-coding"}'
EOF
}

while [ "$#" -gt 0 ]; do
    case "$1" in
        --base-url)
            BASE_URL="${2:?missing value for --base-url}"
            shift 2
            ;;
        --env-file)
            ENV_FILE="${2:?missing value for --env-file}"
            shift 2
            ;;
        --alias)
            KEY_ALIAS="${2:?missing value for --alias}"
            shift 2
            ;;
        --models)
            MODELS="${2:?missing value for --models}"
            shift 2
            ;;
        --duration)
            DURATION="${2:?missing value for --duration}"
            shift 2
            ;;
        --budget)
            MAX_BUDGET="${2:?missing value for --budget}"
            shift 2
            ;;
        --budget-duration)
            BUDGET_DURATION="${2:?missing value for --budget-duration}"
            shift 2
            ;;
        --rpm-limit)
            RPM_LIMIT="${2:?missing value for --rpm-limit}"
            shift 2
            ;;
        --tpm-limit)
            TPM_LIMIT="${2:?missing value for --tpm-limit}"
            shift 2
            ;;
        --user-id)
            USER_ID="${2:?missing value for --user-id}"
            shift 2
            ;;
        --team-id)
            TEAM_ID="${2:?missing value for --team-id}"
            shift 2
            ;;
        --metadata-json)
            METADATA_JSON="${2:?missing value for --metadata-json}"
            shift 2
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            echo "Unknown option: $1" >&2
            usage >&2
            exit 2
            ;;
    esac
done

if [ -f "$ENV_FILE" ]; then
    set -a
    # shellcheck disable=SC1090
    . "$ENV_FILE"
    set +a
fi

if ! command -v curl >/dev/null 2>&1; then
    echo "curl is required" >&2
    exit 1
fi

if ! command -v jq >/dev/null 2>&1; then
    echo "jq is required" >&2
    exit 1
fi

if [ -z "${LITELLM_MASTER_KEY:-}" ] || [ "${LITELLM_MASTER_KEY:-}" = "sk-replace-with-generated-key" ]; then
    echo "LITELLM_MASTER_KEY is required in environment or $ENV_FILE" >&2
    exit 1
fi

metadata=$(printf '%s' "$METADATA_JSON" | jq -c '.')

payload=$(
    jq -n \
        --arg key_alias "$KEY_ALIAS" \
        --arg models "$MODELS" \
        --arg duration "$DURATION" \
        --arg budget_duration "$BUDGET_DURATION" \
        --arg max_budget "$MAX_BUDGET" \
        --arg rpm_limit "$RPM_LIMIT" \
        --arg tpm_limit "$TPM_LIMIT" \
        --arg user_id "$USER_ID" \
        --arg team_id "$TEAM_ID" \
        --argjson metadata "$metadata" '
        {
          key_alias: $key_alias,
          models: (
            $models
            | split(",")
            | map(gsub("^\\s+|\\s+$"; ""))
            | map(select(length > 0))
          ),
          metadata: $metadata
        }
        + (if $duration != "" then {duration: $duration} else {} end)
        + (if $budget_duration != "" then {budget_duration: $budget_duration} else {} end)
        + (if $max_budget != "" then {max_budget: ($max_budget | tonumber)} else {} end)
        + (if $rpm_limit != "" then {rpm_limit: ($rpm_limit | tonumber)} else {} end)
        + (if $tpm_limit != "" then {tpm_limit: ($tpm_limit | tonumber)} else {} end)
        + (if $user_id != "" then {user_id: $user_id} else {} end)
        + (if $team_id != "" then {team_id: $team_id} else {} end)
        '
)

body_file=$(mktemp)
if ! http_status=$(
    curl -sS -o "$body_file" -w "%{http_code}" \
        -X POST "$BASE_URL/key/generate" \
        -H "Authorization: Bearer $LITELLM_MASTER_KEY" \
        -H "Content-Type: application/json" \
        -d "$payload"
); then
    rm -f "$body_file"
    echo "Failed to reach LiteLLM at $BASE_URL" >&2
    exit 1
fi

body=$(cat "$body_file")
rm -f "$body_file"

case "$http_status" in
    2??)
        ;;
    *)
        echo "LiteLLM /key/generate failed with HTTP $http_status" >&2
        printf '%s\n' "$body" >&2
        exit 1
        ;;
esac

client_key=$(printf '%s' "$body" | jq -r '.key // .token // .api_key // empty')
if [ -z "$client_key" ]; then
    echo "LiteLLM response did not contain a generated key" >&2
    printf '%s\n' "$body" >&2
    exit 1
fi

printf 'RU_LLM_PROXY_TOKEN=%s\n' "$client_key"
printf 'key_alias=%s\n' "$KEY_ALIAS"
printf 'models=%s\n' "$MODELS"
if [ -n "$DURATION" ]; then printf 'duration=%s\n' "$DURATION"; fi
if [ -n "$MAX_BUDGET" ]; then printf 'max_budget=%s\n' "$MAX_BUDGET"; fi
if [ -n "$BUDGET_DURATION" ]; then printf 'budget_duration=%s\n' "$BUDGET_DURATION"; fi
