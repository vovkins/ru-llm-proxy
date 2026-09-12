#!/usr/bin/env bash
set -euo pipefail

ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
DOCKER_DESKTOP_BIN=/Applications/Docker.app/Contents/Resources/bin
if [ -x "$DOCKER_DESKTOP_BIN/docker-credential-desktop" ]; then
    export PATH="$DOCKER_DESKTOP_BIN:$PATH"
fi
COMPOSE_FILE="$ROOT/tests/load/docker-compose.yml"
COMPOSE=(docker compose -f "$COMPOSE_FILE")
MODE=${1:-smoke}
LOAD_CONTOUR=${LOAD_CONTOUR:-mock}
LOAD_KEEP_STACK=${LOAD_KEEP_STACK:-false}
LOAD_RESULTS_DIR=${LOAD_RESULTS_DIR:-$(mktemp -d /tmp/ru-llm-proxy-load-XXXXXX)}
LOAD_REPORT_NODE=${LOAD_REPORT_NODE:-local}
LOAD_MASTER_KEY=sk-load-test-master
LOAD_STAGE_DURATION_SECONDS=${LOAD_STAGE_DURATION_SECONDS:-120}
if [ -z "${LOAD_VALIDATE_MAPPING+x}" ]; then
    if [ "$LOAD_CONTOUR" = "mock" ]; then
        LOAD_VALIDATE_MAPPING=true
    else
        LOAD_VALIDATE_MAPPING=false
    fi
fi
LOAD_REQUIRE_STREAM_RESTORATION=${LOAD_REQUIRE_STREAM_RESTORATION:-false}
LOAD_EXIT_CODE_ON_ERROR=${LOAD_EXIT_CODE_ON_ERROR:-}
stats_pid=""
keys_created=false
stack_started=false
compose_scaffold_created=false

usage() {
    cat <<'EOF'
Usage: tests/load/run.sh [smoke|steady|stages|burst|streams|context]

The default mock contour builds an isolated LiteLLM, PostgreSQL, Redis,
Analyzer and mock-provider stack. Set LOAD_CONTOUR=existing only for an
explicitly authorized low-volume run against an existing real-provider stack.

Important overrides:
  LOAD_USERS, LOAD_RUN_TIME, LOAD_PACE_SECONDS, LOAD_CONTEXT_SIZES
  LOAD_API=chat|responses|mixed
  LOAD_CONTEXT_MODE=full-history|one-shot|previous-response|encrypted-state|mixed
  LOAD_STREAM=true|false|mixed
  LOAD_KEEP_STACK=true

Existing contour requirements:
  LOAD_ALLOW_REAL_PROVIDER=true
  LOAD_TARGET_URL=http://host.docker.internal:4000
  LOAD_EXTERNAL_KEY_FILE=/absolute/path/to/keys.json
EOF
}

case "$MODE" in
    smoke)
        LOAD_PROFILE=smoke
        LOAD_USERS=${LOAD_USERS:-8}
        LOAD_RUN_TIME=${LOAD_RUN_TIME:-30s}
        LOAD_CONTEXT_SIZES=${LOAD_CONTEXT_SIZES:-1000,1001}
        LOAD_ANALYZER_QUEUE_TIMEOUT_SECONDS=${LOAD_ANALYZER_QUEUE_TIMEOUT_SECONDS:-30}
        LOAD_SPAWN_RATE=${LOAD_SPAWN_RATE:-4}
        LOAD_EXIT_CODE_ON_ERROR=${LOAD_EXIT_CODE_ON_ERROR:-1}
        ;;
    steady)
        LOAD_PROFILE=steady
        LOAD_USERS=${LOAD_USERS:-400}
        LOAD_RUN_TIME=${LOAD_RUN_TIME:-5m}
        LOAD_CONTEXT_SIZES=${LOAD_CONTEXT_SIZES:-1000}
        LOAD_PACE_SECONDS=${LOAD_PACE_SECONDS:-15}
        LOAD_SPAWN_RATE=${LOAD_SPAWN_RATE:-20}
        ;;
    stages)
        LOAD_PROFILE=stages
        LOAD_USERS=400
        LOAD_STAGE_DURATION_SECONDS=${LOAD_STAGE_DURATION_SECONDS:-120}
        LOAD_RUN_TIME=${LOAD_RUN_TIME:-$((LOAD_STAGE_DURATION_SECONDS * 4 + 15))s}
        LOAD_CONTEXT_SIZES=${LOAD_CONTEXT_SIZES:-1000}
        LOAD_PACE_SECONDS=15
        LOAD_SPAWN_RATE=${LOAD_SPAWN_RATE:-100}
        ;;
    burst)
        LOAD_PROFILE=burst
        LOAD_USERS=400
        LOAD_RUN_TIME=${LOAD_RUN_TIME:-45s}
        LOAD_CONTEXT_SIZES=${LOAD_CONTEXT_SIZES:-1000}
        LOAD_SPAWN_RATE=${LOAD_SPAWN_RATE:-400}
        ;;
    streams)
        LOAD_PROFILE=streams
        LOAD_USERS=400
        LOAD_RUN_TIME=${LOAD_RUN_TIME:-75s}
        LOAD_CONTEXT_SIZES=${LOAD_CONTEXT_SIZES:-1000}
        LOAD_STREAM=true
        LOAD_MOCK_STREAM_HOLD_SECONDS=${LOAD_MOCK_STREAM_HOLD_SECONDS:-45}
        LOAD_SPAWN_RATE=${LOAD_SPAWN_RATE:-100}
        ;;
    context)
        LOAD_PROFILE=context
        LOAD_USERS=${LOAD_USERS:-1}
        LOAD_RUN_TIME=${LOAD_RUN_TIME:-60m}
        LOAD_CONTEXT_SIZES=${LOAD_CONTEXT_SIZES:-1000,8000,50000,128000,256000,512000,1000000}
        LOAD_API=${LOAD_API:-responses}
        LOAD_CONTEXT_MODE=${LOAD_CONTEXT_MODE:-one-shot}
        LOAD_STREAM=${LOAD_STREAM:-false}
        LOAD_PACE_SECONDS=${LOAD_PACE_SECONDS:-1}
        LOAD_GUARDRAIL_ANALYZER_TIMEOUT_SECONDS=${LOAD_GUARDRAIL_ANALYZER_TIMEOUT_SECONDS:-1200}
        LOAD_READ_TIMEOUT_SECONDS=${LOAD_READ_TIMEOUT_SECONDS:-1300}
        LOAD_SPAWN_RATE=${LOAD_SPAWN_RATE:-1}
        ;;
    -h|--help|help)
        usage
        exit 0
        ;;
    *)
        echo "Unknown load mode: $MODE" >&2
        usage >&2
        exit 2
        ;;
esac

LOAD_PACE_SECONDS=${LOAD_PACE_SECONDS:-15}
LOAD_API=${LOAD_API:-mixed}
LOAD_CONTEXT_MODE=${LOAD_CONTEXT_MODE:-mixed}
LOAD_STREAM=${LOAD_STREAM:-mixed}
LOAD_MOCK_STREAM_HOLD_SECONDS=${LOAD_MOCK_STREAM_HOLD_SECONDS:-0}
LOAD_EXIT_CODE_ON_ERROR=${LOAD_EXIT_CODE_ON_ERROR:-0}

export LOAD_PROFILE LOAD_USERS LOAD_RUN_TIME LOAD_CONTEXT_SIZES
export LOAD_PACE_SECONDS LOAD_STAGE_DURATION_SECONDS LOAD_RESULTS_DIR LOAD_REPORT_NODE
export LOAD_API LOAD_CONTEXT_MODE LOAD_STREAM LOAD_MOCK_STREAM_HOLD_SECONDS
export LOAD_MASTER_KEY LOAD_VALIDATE_MAPPING
export LOAD_REQUIRE_STREAM_RESTORATION LOAD_ANALYZER_QUEUE_TIMEOUT_SECONDS
export LOAD_GUARDRAIL_ANALYZER_TIMEOUT_SECONDS LOAD_READ_TIMEOUT_SECONDS

stop_stats() {
    if [ -n "$stats_pid" ] && kill -0 "$stats_pid" 2>/dev/null; then
        kill "$stats_pid" 2>/dev/null || true
        wait "$stats_pid" 2>/dev/null || true
    fi
}

collect_stack_artifacts() {
    if [ "$stack_started" != true ]; then
        return
    fi
    "${COMPOSE[@]}" exec -T load-mock-upstream python -c \
        "import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:8080/capture').read().decode())" \
        > "$LOAD_RESULTS_DIR/mock-provider-capture.json" 2>/dev/null || true
    "${COMPOSE[@]}" exec -T load-presidio-analyzer python -c \
        "import urllib.request; text=urllib.request.urlopen('http://127.0.0.1:5001/metrics').read().decode(); print('\n'.join(line for line in text.splitlines() if line.startswith(('ru_presidio_analyzer_', '# HELP ru_presidio_analyzer_', '# TYPE ru_presidio_analyzer_'))))" \
        > "$LOAD_RESULTS_DIR/analyzer-metrics.prom" 2>/dev/null || true
    "${COMPOSE[@]}" exec -T load-litellm python -c \
        "import urllib.request; text=urllib.request.urlopen('http://127.0.0.1:4000/metrics').read().decode(); print('\n'.join(line for line in text.splitlines() if line.startswith(('ru_pii_guardrail_', '# HELP ru_pii_guardrail_', '# TYPE ru_pii_guardrail_'))))" \
        > "$LOAD_RESULTS_DIR/guardrail-metrics.prom" 2>/dev/null || true
}

cleanup() {
    exit_status=$?
    trap - EXIT INT TERM
    set +e
    stop_stats
    collect_stack_artifacts
    if [ "$keys_created" = true ]; then
        "${COMPOSE[@]}" --profile load run --rm load-key-manager delete || true
    fi
    if [ "$stack_started" = true ] && [ "$LOAD_KEEP_STACK" != true ]; then
        "${COMPOSE[@]}" --profile load down -v --remove-orphans || true
    elif [ "$compose_scaffold_created" = true ]; then
        "${COMPOSE[@]}" --profile load down -v --remove-orphans || true
    fi
    echo "Load-test reports: $LOAD_RESULTS_DIR"
    exit "$exit_status"
}
trap cleanup EXIT INT TERM

case "$LOAD_RESULTS_DIR" in
    /*) ;;
    *) echo "LOAD_RESULTS_DIR must be an absolute path" >&2; exit 2 ;;
esac
mkdir -p "$LOAD_RESULTS_DIR"

if [ "$LOAD_CONTOUR" = "mock" ]; then
    echo "Building and starting the isolated mock-provider load contour..."
    stack_started=true
    "${COMPOSE[@]}" up -d --build \
        load-db load-redis load-mock-upstream load-presidio-analyzer load-litellm

    keys_created=true
    "${COMPOSE[@]}" --profile load run --rm load-key-manager create \
        --count "$LOAD_USERS"

    container_ids=$("${COMPOSE[@]}" ps -q \
        load-db load-redis load-mock-upstream load-presidio-analyzer load-litellm)
    if [ -z "$container_ids" ]; then
        echo "No load-contour containers found for Docker statistics" >&2
        exit 1
    fi
    (
        while true; do
            sampled_at=$(date +%s)
            docker stats --no-stream --format '{{json .}}' $container_ids 2>/dev/null \
                | while IFS= read -r sample; do
                    printf '{"sampled_at":%s,"docker":%s}\n' "$sampled_at" "$sample"
                done
            sleep 1
        done
    ) > "$LOAD_RESULTS_DIR/docker-stats.jsonl" &
    stats_pid=$!

    compose_run_args=(--no-TTY)
    locust_args=(--headless --host http://load-litellm:4000)
else
    if [ "$LOAD_CONTOUR" != "existing" ]; then
        echo "LOAD_CONTOUR must be mock or existing" >&2
        exit 2
    fi
    if [ "${LOAD_ALLOW_REAL_PROVIDER:-false}" != true ]; then
        echo "Set LOAD_ALLOW_REAL_PROVIDER=true for the existing contour" >&2
        exit 2
    fi
    if [ -z "${LOAD_TARGET_URL:-}" ] || [ -z "${LOAD_EXTERNAL_KEY_FILE:-}" ]; then
        echo "LOAD_TARGET_URL and LOAD_EXTERNAL_KEY_FILE are required" >&2
        exit 2
    fi
    if [ ! -f "$LOAD_EXTERNAL_KEY_FILE" ]; then
        echo "LOAD_EXTERNAL_KEY_FILE does not exist" >&2
        exit 2
    fi
    case "$LOAD_EXTERNAL_KEY_FILE" in
        /*) ;;
        *) echo "LOAD_EXTERNAL_KEY_FILE must be an absolute path" >&2; exit 2 ;;
    esac
    compose_scaffold_created=true
    compose_run_args=(
        --no-TTY
        --no-deps
        -v "$LOAD_EXTERNAL_KEY_FILE:/external/keys.json:ro"
        -e LOAD_KEY_FILE=/external/keys.json
        -e LOAD_TARGET_URL="$LOAD_TARGET_URL"
    )
    locust_args=(
        --headless --host "$LOAD_TARGET_URL"
    )
fi

echo "Running $MODE with $LOAD_USERS users; reports: $LOAD_RESULTS_DIR"
"${COMPOSE[@]}" --profile load run --rm \
    "${compose_run_args[@]}" \
    -e LOAD_REPORT_NODE="$LOAD_REPORT_NODE" \
    load-generator \
    "${locust_args[@]}" \
    --users "$LOAD_USERS" \
    --spawn-rate "$LOAD_SPAWN_RATE" \
    --run-time "$LOAD_RUN_TIME" \
    --csv /results/locust \
    --html /results/locust.html \
    --only-summary \
    --exit-code-on-error "$LOAD_EXIT_CODE_ON_ERROR"
