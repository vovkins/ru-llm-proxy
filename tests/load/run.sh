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
LOAD_INPUT_VARIATION=${LOAD_INPUT_VARIATION:-repeat}
LOAD_ANALYZER_BACKEND=${LOAD_ANALYZER_BACKEND:-real}
LOAD_ANALYZER_URL=${LOAD_ANALYZER_URL:-http://load-analyzer-router:5001}
LOAD_ANALYZER_REPLICAS_WAS_SET=${LOAD_ANALYZER_REPLICAS+x}
LOAD_LITELLM_REPLICAS_WAS_SET=${LOAD_LITELLM_REPLICAS+x}
LOAD_ANALYZER_CONCURRENCY_LIMIT_WAS_SET=${LOAD_ANALYZER_CONCURRENCY_LIMIT+x}
LOAD_ANALYZER_QUEUE_LIMIT_WAS_SET=${LOAD_ANALYZER_QUEUE_LIMIT+x}
LOAD_ANALYZER_QUEUE_TIMEOUT_SECONDS_WAS_SET=${LOAD_ANALYZER_QUEUE_TIMEOUT_SECONDS+x}
LOAD_ANALYZER_MAX_CONNECTIONS_WAS_SET=${LOAD_GUARDRAIL_ANALYZER_MAX_CONNECTIONS+x}
LOAD_ANALYZER_MAX_KEEPALIVE_WAS_SET=${LOAD_GUARDRAIL_ANALYZER_MAX_KEEPALIVE_CONNECTIONS+x}
LOAD_REDIS_MAX_CONNECTIONS_WAS_SET=${LOAD_GUARDRAIL_REDIS_MAX_CONNECTIONS+x}
LOAD_ANALYZER_REPLICAS=${LOAD_ANALYZER_REPLICAS:-1}
LOAD_LITELLM_REPLICAS=${LOAD_LITELLM_REPLICAS:-1}
LOAD_ANALYZER_CONCURRENCY_LIMIT=${LOAD_ANALYZER_CONCURRENCY_LIMIT:-1}
LOAD_ANALYZER_QUEUE_LIMIT=${LOAD_ANALYZER_QUEUE_LIMIT:-8}
LOAD_GUARDRAIL_ANALYZER_MAX_CONNECTIONS=${LOAD_GUARDRAIL_ANALYZER_MAX_CONNECTIONS:-20}
LOAD_GUARDRAIL_ANALYZER_MAX_KEEPALIVE_CONNECTIONS=${LOAD_GUARDRAIL_ANALYZER_MAX_KEEPALIVE_CONNECTIONS:-10}
LOAD_GUARDRAIL_REDIS_MAX_CONNECTIONS=${LOAD_GUARDRAIL_REDIS_MAX_CONNECTIONS:-20}
LOAD_ANALYZER_CPUS=${LOAD_ANALYZER_CPUS:-4.0}
LOAD_ANALYZER_MEMORY=${LOAD_ANALYZER_MEMORY:-4g}
LOAD_LITELLM_CPUS=${LOAD_LITELLM_CPUS:-2.0}
LOAD_LITELLM_MEMORY=${LOAD_LITELLM_MEMORY:-2g}
LOAD_DB_CPUS=${LOAD_DB_CPUS:-1.0}
LOAD_DB_MEMORY=${LOAD_DB_MEMORY:-1g}
LOAD_REDIS_CPUS=${LOAD_REDIS_CPUS:-1.0}
LOAD_REDIS_MEMORY=${LOAD_REDIS_MEMORY:-512m}
LOAD_MOCK_CPUS=${LOAD_MOCK_CPUS:-1.0}
LOAD_MOCK_MEMORY=${LOAD_MOCK_MEMORY:-512m}
LOAD_ROUTER_CPUS=${LOAD_ROUTER_CPUS:-1.0}
LOAD_ROUTER_MEMORY=${LOAD_ROUTER_MEMORY:-256m}
LOAD_STOP_TIMEOUT_SECONDS=${LOAD_STOP_TIMEOUT_SECONDS:-}
LOAD_STATEFUL_CHECKS=${LOAD_STATEFUL_CHECKS:-false}
LOAD_STATEFUL_CHURN_DURATION_SECONDS=${LOAD_STATEFUL_CHURN_DURATION_SECONDS:-120}
LOAD_STATEFUL_CHURN_CONCURRENCY=${LOAD_STATEFUL_CHURN_CONCURRENCY:-4}
LOAD_STATEFUL_REVOCATION_TIMEOUT_SECONDS=${LOAD_STATEFUL_REVOCATION_TIMEOUT_SECONDS:-8}
LOAD_STATEFUL_REVOCATION_REQUIRED_DENIALS=${LOAD_STATEFUL_REVOCATION_REQUIRED_DENIALS:-4}
LOAD_RESILIENCE_CHECKS=${LOAD_RESILIENCE_CHECKS:-false}
LOAD_RESILIENCE_SCENARIOS=${LOAD_RESILIENCE_SCENARIOS:-analyzer,litellm,redis,postgres}
LOAD_RESILIENCE_INITIAL_DELAY_SECONDS=${LOAD_RESILIENCE_INITIAL_DELAY_SECONDS:-430}
LOAD_RESILIENCE_DOWNTIME_SECONDS=${LOAD_RESILIENCE_DOWNTIME_SECONDS:-8}
LOAD_RESILIENCE_BETWEEN_SECONDS=${LOAD_RESILIENCE_BETWEEN_SECONDS:-150}
LOAD_RESILIENCE_RECOVERY_TIMEOUT_SECONDS=${LOAD_RESILIENCE_RECOVERY_TIMEOUT_SECONDS:-120}
LOAD_RESILIENCE_USER_RECOVERY_TIMEOUT_SECONDS=${LOAD_RESILIENCE_USER_RECOVERY_TIMEOUT_SECONDS:-1200}
LOAD_RESILIENCE_RECOVERY_GRACE_SECONDS=${LOAD_RESILIENCE_RECOVERY_GRACE_SECONDS:-15}
LOAD_CANCELLATION_OBSERVE_TIMEOUT_SECONDS=${LOAD_CANCELLATION_OBSERVE_TIMEOUT_SECONDS:-15}
LOAD_CANCELLATION_IMMEDIATE_TIMEOUT_SECONDS=${LOAD_CANCELLATION_IMMEDIATE_TIMEOUT_SECONDS:-5}
LOAD_CANCELLATION_EXPIRY_GRACE_SECONDS=${LOAD_CANCELLATION_EXPIRY_GRACE_SECONDS:-10}
stats_pid=""
churn_pid=""
fault_pid=""
keys_created=false
stack_started=false
compose_scaffold_created=false

usage() {
    cat <<'EOF'
Usage: tests/load/run.sh [smoke|steady|stages|burst|streams|context|stateful|resilience]

The default mock contour builds an isolated LiteLLM, PostgreSQL, Redis,
Analyzer and mock-provider stack. LOAD_CONTOUR=mock-direct calibrates Locust
against the mock provider without LiteLLM or Analyzer. Set LOAD_CONTOUR=existing
only for an explicitly authorized low-volume real-provider run.

Important overrides:
  LOAD_USERS, LOAD_RUN_TIME, LOAD_PACE_SECONDS, LOAD_CONTEXT_SIZES
  LOAD_API=chat|responses|mixed
  LOAD_CONTEXT_MODE=full-history|one-shot|previous-response|encrypted-state|mixed
  LOAD_STREAM=true|false|mixed
  LOAD_INPUT_VARIATION=repeat|unique
  LOAD_ANALYZER_BACKEND=real|mock
  LOAD_ANALYZER_REPLICAS, LOAD_LITELLM_REPLICAS
  LOAD_ANALYZER_CONCURRENCY_LIMIT, LOAD_ANALYZER_QUEUE_LIMIT
  LOAD_ANALYZER_QUEUE_TIMEOUT_SECONDS
  LOAD_GUARDRAIL_ANALYZER_MAX_CONNECTIONS
  LOAD_GUARDRAIL_ANALYZER_MAX_KEEPALIVE_CONNECTIONS
  LOAD_GUARDRAIL_REDIS_MAX_CONNECTIONS
  LOAD_ANALYZER_CPUS, LOAD_ANALYZER_MEMORY
  LOAD_LITELLM_CPUS, LOAD_LITELLM_MEMORY
  LOAD_KEEP_STACK=true
  LOAD_STATEFUL_CHURN_DURATION_SECONDS, LOAD_STATEFUL_CHURN_CONCURRENCY
  LOAD_STATEFUL_REVOCATION_TIMEOUT_SECONDS
  LOAD_RESILIENCE_SCENARIOS, LOAD_RESILIENCE_INITIAL_DELAY_SECONDS
  LOAD_RESILIENCE_DOWNTIME_SECONDS, LOAD_RESILIENCE_BETWEEN_SECONDS
  LOAD_RESILIENCE_RECOVERY_TIMEOUT_SECONDS
  LOAD_RESILIENCE_USER_RECOVERY_TIMEOUT_SECONDS

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
        LOAD_STOP_TIMEOUT_SECONDS=${LOAD_STOP_TIMEOUT_SECONDS:-1300}
        ;;
    stateful)
        LOAD_PROFILE=steady
        LOAD_USERS=${LOAD_USERS:-400}
        LOAD_RUN_TIME=${LOAD_RUN_TIME:-18m}
        LOAD_CONTEXT_SIZES=${LOAD_CONTEXT_SIZES:-1000}
        LOAD_API=${LOAD_API:-mixed}
        LOAD_CONTEXT_MODE=${LOAD_CONTEXT_MODE:-mixed}
        LOAD_STREAM=${LOAD_STREAM:-mixed}
        LOAD_PACE_SECONDS=${LOAD_PACE_SECONDS:-15}
        LOAD_INPUT_VARIATION=${LOAD_INPUT_VARIATION:-repeat}
        if [ -z "$LOAD_ANALYZER_REPLICAS_WAS_SET" ]; then LOAD_ANALYZER_REPLICAS=4; fi
        if [ -z "$LOAD_LITELLM_REPLICAS_WAS_SET" ]; then LOAD_LITELLM_REPLICAS=2; fi
        if [ -z "$LOAD_ANALYZER_CONCURRENCY_LIMIT_WAS_SET" ]; then LOAD_ANALYZER_CONCURRENCY_LIMIT=1; fi
        if [ -z "$LOAD_ANALYZER_QUEUE_LIMIT_WAS_SET" ]; then LOAD_ANALYZER_QUEUE_LIMIT=200; fi
        if [ -z "$LOAD_ANALYZER_QUEUE_TIMEOUT_SECONDS_WAS_SET" ]; then LOAD_ANALYZER_QUEUE_TIMEOUT_SECONDS=30; fi
        if [ -z "$LOAD_ANALYZER_MAX_CONNECTIONS_WAS_SET" ]; then LOAD_GUARDRAIL_ANALYZER_MAX_CONNECTIONS=200; fi
        if [ -z "$LOAD_ANALYZER_MAX_KEEPALIVE_WAS_SET" ]; then LOAD_GUARDRAIL_ANALYZER_MAX_KEEPALIVE_CONNECTIONS=100; fi
        if [ -z "$LOAD_REDIS_MAX_CONNECTIONS_WAS_SET" ]; then LOAD_GUARDRAIL_REDIS_MAX_CONNECTIONS=200; fi
        LOAD_VALIDATE_MAPPING=true
        LOAD_REQUIRE_STREAM_RESTORATION=false
        LOAD_STATEFUL_CHECKS=true
        LOAD_GUARDRAIL_ANALYZER_TIMEOUT_SECONDS=${LOAD_GUARDRAIL_ANALYZER_TIMEOUT_SECONDS:-90}
        LOAD_READ_TIMEOUT_SECONDS=${LOAD_READ_TIMEOUT_SECONDS:-150}
        LOAD_STOP_TIMEOUT_SECONDS=${LOAD_STOP_TIMEOUT_SECONDS:-150}
        LOAD_SPAWN_RATE=${LOAD_SPAWN_RATE:-0.5}
        LOAD_EXIT_CODE_ON_ERROR=1
        ;;
    resilience)
        LOAD_PROFILE=steady
        LOAD_USERS=${LOAD_USERS:-400}
        LOAD_RUN_TIME=${LOAD_RUN_TIME:-45m}
        LOAD_CONTEXT_SIZES=${LOAD_CONTEXT_SIZES:-1000}
        LOAD_API=${LOAD_API:-mixed}
        LOAD_CONTEXT_MODE=${LOAD_CONTEXT_MODE:-mixed}
        LOAD_STREAM=${LOAD_STREAM:-mixed}
        LOAD_PACE_SECONDS=${LOAD_PACE_SECONDS:-15}
        LOAD_INPUT_VARIATION=${LOAD_INPUT_VARIATION:-repeat}
        if [ -z "$LOAD_ANALYZER_REPLICAS_WAS_SET" ]; then LOAD_ANALYZER_REPLICAS=6; fi
        if [ -z "$LOAD_LITELLM_REPLICAS_WAS_SET" ]; then LOAD_LITELLM_REPLICAS=2; fi
        if [ -z "$LOAD_ANALYZER_CONCURRENCY_LIMIT_WAS_SET" ]; then LOAD_ANALYZER_CONCURRENCY_LIMIT=1; fi
        if [ -z "$LOAD_ANALYZER_QUEUE_LIMIT_WAS_SET" ]; then LOAD_ANALYZER_QUEUE_LIMIT=200; fi
        if [ -z "$LOAD_ANALYZER_QUEUE_TIMEOUT_SECONDS_WAS_SET" ]; then LOAD_ANALYZER_QUEUE_TIMEOUT_SECONDS=30; fi
        if [ -z "$LOAD_ANALYZER_MAX_CONNECTIONS_WAS_SET" ]; then LOAD_GUARDRAIL_ANALYZER_MAX_CONNECTIONS=200; fi
        if [ -z "$LOAD_ANALYZER_MAX_KEEPALIVE_WAS_SET" ]; then LOAD_GUARDRAIL_ANALYZER_MAX_KEEPALIVE_CONNECTIONS=100; fi
        if [ -z "$LOAD_REDIS_MAX_CONNECTIONS_WAS_SET" ]; then LOAD_GUARDRAIL_REDIS_MAX_CONNECTIONS=200; fi
        LOAD_VALIDATE_MAPPING=true
        LOAD_REQUIRE_STREAM_RESTORATION=false
        LOAD_RESILIENCE_CHECKS=true
        LOAD_GUARDRAIL_ANALYZER_TIMEOUT_SECONDS=${LOAD_GUARDRAIL_ANALYZER_TIMEOUT_SECONDS:-90}
        LOAD_READ_TIMEOUT_SECONDS=${LOAD_READ_TIMEOUT_SECONDS:-150}
        LOAD_STOP_TIMEOUT_SECONDS=${LOAD_STOP_TIMEOUT_SECONDS:-150}
        LOAD_SPAWN_RATE=${LOAD_SPAWN_RATE:-0.4}
        LOAD_PII_MAPPING_TTL_SECONDS=${LOAD_PII_MAPPING_TTL_SECONDS:-15}
        LOAD_MOCK_FAILURE_DELAY_SECONDS=${LOAD_MOCK_FAILURE_DELAY_SECONDS:-60}
        LOAD_EXIT_CODE_ON_ERROR=0
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
LOAD_STOP_TIMEOUT_SECONDS=${LOAD_STOP_TIMEOUT_SECONDS:-30}
LOAD_ANALYZER_QUEUE_TIMEOUT_SECONDS=${LOAD_ANALYZER_QUEUE_TIMEOUT_SECONDS:-1}
LOAD_PII_MAPPING_TTL_SECONDS=${LOAD_PII_MAPPING_TTL_SECONDS:-7200}

export LOAD_PROFILE LOAD_USERS LOAD_RUN_TIME LOAD_CONTEXT_SIZES
export LOAD_PACE_SECONDS LOAD_STAGE_DURATION_SECONDS LOAD_RESULTS_DIR LOAD_REPORT_NODE
export LOAD_API LOAD_CONTEXT_MODE LOAD_STREAM LOAD_MOCK_STREAM_HOLD_SECONDS
export LOAD_MASTER_KEY LOAD_VALIDATE_MAPPING
export LOAD_REQUIRE_STREAM_RESTORATION LOAD_ANALYZER_QUEUE_TIMEOUT_SECONDS
export LOAD_GUARDRAIL_ANALYZER_TIMEOUT_SECONDS LOAD_READ_TIMEOUT_SECONDS
export LOAD_CONTOUR LOAD_INPUT_VARIATION LOAD_ANALYZER_REPLICAS LOAD_LITELLM_REPLICAS
export LOAD_ANALYZER_BACKEND LOAD_ANALYZER_URL
export LOAD_ANALYZER_CONCURRENCY_LIMIT LOAD_ANALYZER_QUEUE_LIMIT
export LOAD_GUARDRAIL_ANALYZER_MAX_CONNECTIONS
export LOAD_GUARDRAIL_ANALYZER_MAX_KEEPALIVE_CONNECTIONS
export LOAD_GUARDRAIL_REDIS_MAX_CONNECTIONS
export LOAD_ANALYZER_CPUS LOAD_ANALYZER_MEMORY LOAD_LITELLM_CPUS LOAD_LITELLM_MEMORY
export LOAD_DB_CPUS LOAD_DB_MEMORY LOAD_REDIS_CPUS LOAD_REDIS_MEMORY
export LOAD_MOCK_CPUS LOAD_MOCK_MEMORY LOAD_ROUTER_CPUS LOAD_ROUTER_MEMORY
export LOAD_STOP_TIMEOUT_SECONDS
export LOAD_STATEFUL_CHECKS LOAD_STATEFUL_CHURN_DURATION_SECONDS
export LOAD_STATEFUL_CHURN_CONCURRENCY
export LOAD_RESILIENCE_CHECKS LOAD_RESILIENCE_SCENARIOS
export LOAD_RESILIENCE_INITIAL_DELAY_SECONDS LOAD_RESILIENCE_DOWNTIME_SECONDS
export LOAD_RESILIENCE_BETWEEN_SECONDS LOAD_RESILIENCE_RECOVERY_TIMEOUT_SECONDS
export LOAD_RESILIENCE_RECOVERY_GRACE_SECONDS LOAD_PII_MAPPING_TTL_SECONDS
export LOAD_RESILIENCE_USER_RECOVERY_TIMEOUT_SECONDS
export LOAD_CANCELLATION_OBSERVE_TIMEOUT_SECONDS
export LOAD_CANCELLATION_IMMEDIATE_TIMEOUT_SECONDS
export LOAD_CANCELLATION_EXPIRY_GRACE_SECONDS LOAD_MOCK_FAILURE_DELAY_SECONDS

stop_stats() {
    if [ -n "$stats_pid" ] && kill -0 "$stats_pid" 2>/dev/null; then
        kill "$stats_pid" 2>/dev/null || true
        wait "$stats_pid" 2>/dev/null || true
    fi
}

stop_churn() {
    if [ -n "$churn_pid" ] && kill -0 "$churn_pid" 2>/dev/null; then
        kill "$churn_pid" 2>/dev/null || true
        wait "$churn_pid" 2>/dev/null || true
    fi
}

stop_fault_injector() {
    if [ -n "$fault_pid" ] && kill -0 "$fault_pid" 2>/dev/null; then
        kill "$fault_pid" 2>/dev/null || true
        wait "$fault_pid" 2>/dev/null || true
    fi
}

collect_stack_artifacts() {
    if [ "$stack_started" != true ]; then
        return
    fi
    for mock_service in load-mock-upstream load-mock-upstream-a load-mock-upstream-b; do
        mock_id=$("${COMPOSE[@]}" ps -q "$mock_service" | head -n 1)
        if [ -n "$mock_id" ]; then
            docker exec "$mock_id" python -c \
            "import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:8080/capture').read().decode())" \
            > "$LOAD_RESULTS_DIR/$mock_service-capture.json" 2>/dev/null || true
        fi
    done
    replica=0
    for container_id in $("${COMPOSE[@]}" ps -q load-presidio-analyzer); do
        replica=$((replica + 1))
        docker exec "$container_id" python -c \
            "import urllib.request; text=urllib.request.urlopen('http://127.0.0.1:5001/metrics').read().decode(); print('\n'.join(line for line in text.splitlines() if line.startswith(('ru_presidio_analyzer_', '# HELP ru_presidio_analyzer_', '# TYPE ru_presidio_analyzer_'))))" \
            > "$LOAD_RESULTS_DIR/analyzer-metrics-$replica.prom" 2>/dev/null || true
    done
    replica=0
    for container_id in $("${COMPOSE[@]}" ps -q load-litellm); do
        replica=$((replica + 1))
        docker exec "$container_id" python -c \
            "import urllib.request; text=urllib.request.urlopen('http://127.0.0.1:4000/metrics').read().decode(); print('\n'.join(line for line in text.splitlines() if line.startswith(('ru_pii_guardrail_', '# HELP ru_pii_guardrail_', '# TYPE ru_pii_guardrail_'))))" \
            > "$LOAD_RESULTS_DIR/guardrail-metrics-$replica.prom" 2>/dev/null || true
    done
}

cleanup() {
    exit_status=$?
    trap - EXIT INT TERM
    set +e
    stop_fault_injector
    stop_churn
    stop_stats
    collect_stack_artifacts
    python3 "$ROOT/tests/load/summarize_run.py" "$LOAD_RESULTS_DIR" || true
    if [ "$keys_created" = true ]; then
        "${COMPOSE[@]}" --profile load run --rm --no-deps \
            load-key-manager delete || true
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

case "$LOAD_ANALYZER_BACKEND" in
    real|mock) ;;
    *) echo "LOAD_ANALYZER_BACKEND must be real or mock" >&2; exit 2 ;;
esac
if [ "$LOAD_ANALYZER_BACKEND" = mock ] && [ "$LOAD_CONTOUR" != mock ]; then
    echo "The mock Analyzer backend is available only in LOAD_CONTOUR=mock" >&2
    exit 2
fi
if [ "$LOAD_STATEFUL_CHECKS" = true ] && [ "$LOAD_CONTOUR" != mock ]; then
    echo "Stateful checks are available only in LOAD_CONTOUR=mock" >&2
    exit 2
fi
if [ "$LOAD_RESILIENCE_CHECKS" = true ] && [ "$LOAD_CONTOUR" != mock ]; then
    echo "Resilience checks are available only in LOAD_CONTOUR=mock" >&2
    exit 2
fi
if [ "$LOAD_STATEFUL_CHECKS" = true ] && [ "$LOAD_RESILIENCE_CHECKS" = true ]; then
    echo "Stateful and resilience checks cannot run in the same invocation" >&2
    exit 2
fi

if [ "$LOAD_CONTOUR" = "mock" ]; then
    case "$LOAD_ANALYZER_REPLICAS:$LOAD_LITELLM_REPLICAS" in
        *[!0-9:]*|0:*|*:0) echo "Replica counts must be positive integers" >&2; exit 2 ;;
    esac
    echo "Building and starting the isolated mock-provider load contour..."
    stack_started=true
    "${COMPOSE[@]}" up -d --build \
        --scale "load-presidio-analyzer=$LOAD_ANALYZER_REPLICAS" \
        --scale "load-litellm=$LOAD_LITELLM_REPLICAS" \
        load-db load-redis load-mock-upstream load-mock-upstream-a \
        load-mock-upstream-b load-presidio-analyzer \
        load-analyzer-router load-litellm load-litellm-router

    keys_created=true
    "${COMPOSE[@]}" --profile load run --rm --no-deps load-key-manager create \
        --count "$LOAD_USERS"

    container_ids=$("${COMPOSE[@]}" ps -q \
        load-db load-redis load-mock-upstream load-mock-upstream-a \
        load-mock-upstream-b load-presidio-analyzer \
        load-analyzer-router load-litellm load-litellm-router)
    if [ -z "$container_ids" ]; then
        echo "No load-contour containers found for Docker statistics" >&2
        exit 1
    fi
    python3 "$ROOT/tests/load/sample_metrics.py" \
        --output-dir "$LOAD_RESULTS_DIR" --interval 1 &
    stats_pid=$!

    compose_run_args=(--no-TTY --no-deps)
    locust_args=(--headless --host http://load-litellm-router:4000)

    if [ "$LOAD_STATEFUL_CHECKS" = true ]; then
        stateful_base_url="http://127.0.0.1:${LOAD_LITELLM_PORT:-14020}"
        python3 "$ROOT/tests/load/stateful_checks.py" preflight \
            --base-url "$stateful_base_url" \
            --compose-file "$COMPOSE_FILE" \
            --results-dir "$LOAD_RESULTS_DIR" \
            --expected-users "$LOAD_USERS"
        stateful_logs_since=$(date -u +"%Y-%m-%dT%H:%M:%SZ")
        python3 "$ROOT/tests/load/stateful_checks.py" churn \
            --base-url "$stateful_base_url" \
            --compose-file "$COMPOSE_FILE" \
            --results-dir "$LOAD_RESULTS_DIR" \
            --churn-duration-seconds "$LOAD_STATEFUL_CHURN_DURATION_SECONDS" \
            --churn-concurrency "$LOAD_STATEFUL_CHURN_CONCURRENCY" \
            --revocation-timeout-seconds "$LOAD_STATEFUL_REVOCATION_TIMEOUT_SECONDS" \
            --revocation-required-denials "$LOAD_STATEFUL_REVOCATION_REQUIRED_DENIALS" &
        churn_pid=$!
    fi
    if [ "$LOAD_RESILIENCE_CHECKS" = true ]; then
        resilience_base_url="http://127.0.0.1:${LOAD_LITELLM_PORT:-14020}"
        python3 "$ROOT/tests/load/stateful_checks.py" preflight \
            --base-url "$resilience_base_url" \
            --compose-file "$COMPOSE_FILE" \
            --results-dir "$LOAD_RESULTS_DIR" \
            --expected-users "$LOAD_USERS"
        python3 "$ROOT/tests/load/resilience_checks.py" cancel \
            --base-url "$resilience_base_url" \
            --compose-file "$COMPOSE_FILE" \
            --results-dir "$LOAD_RESULTS_DIR" \
            --mapping-ttl-seconds "$LOAD_PII_MAPPING_TTL_SECONDS" \
            --cancellation-observe-timeout-seconds "$LOAD_CANCELLATION_OBSERVE_TIMEOUT_SECONDS" \
            --cancellation-immediate-timeout-seconds "$LOAD_CANCELLATION_IMMEDIATE_TIMEOUT_SECONDS" \
            --cancellation-expiry-grace-seconds "$LOAD_CANCELLATION_EXPIRY_GRACE_SECONDS"
        python3 "$ROOT/tests/load/resilience_checks.py" inject \
            --base-url "$resilience_base_url" \
            --compose-file "$COMPOSE_FILE" \
            --results-dir "$LOAD_RESULTS_DIR" \
            --scenarios "$LOAD_RESILIENCE_SCENARIOS" \
            --initial-delay-seconds "$LOAD_RESILIENCE_INITIAL_DELAY_SECONDS" \
            --downtime-seconds "$LOAD_RESILIENCE_DOWNTIME_SECONDS" \
            --between-seconds "$LOAD_RESILIENCE_BETWEEN_SECONDS" \
            --recovery-timeout-seconds "$LOAD_RESILIENCE_RECOVERY_TIMEOUT_SECONDS" \
            --user-recovery-timeout-seconds "$LOAD_RESILIENCE_USER_RECOVERY_TIMEOUT_SECONDS" \
            --expected-users "$LOAD_USERS" &
        fault_pid=$!
    fi
elif [ "$LOAD_CONTOUR" = "mock-direct" ]; then
    echo "Starting direct mock-provider calibration contour..."
    stack_started=true
    "${COMPOSE[@]}" up -d load-mock-upstream
    "${COMPOSE[@]}" --profile load run --rm --no-deps \
        load-key-manager create-local --count "$LOAD_USERS"
    python3 "$ROOT/tests/load/sample_metrics.py" \
        --output-dir "$LOAD_RESULTS_DIR" --interval 1 &
    stats_pid=$!
    compose_run_args=(
        --no-TTY
        --no-deps
        -e LOAD_TARGET_URL=http://load-mock-upstream:8080
    )
    locust_args=(--headless --host http://load-mock-upstream:8080)
else
    if [ "$LOAD_CONTOUR" != "existing" ]; then
        echo "LOAD_CONTOUR must be mock, mock-direct or existing" >&2
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
load_status=0
set +e
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
    --stop-timeout "$LOAD_STOP_TIMEOUT_SECONDS" \
    --exit-code-on-error "$LOAD_EXIT_CODE_ON_ERROR"
load_status=$?
set -e

if [ "$LOAD_STATEFUL_CHECKS" = true ]; then
    churn_status=0
    set +e
    wait "$churn_pid"
    churn_status=$?
    churn_pid=""
    set -e

    validation_status=0
    python3 "$ROOT/tests/load/stateful_checks.py" validate \
        --compose-file "$COMPOSE_FILE" \
        --results-dir "$LOAD_RESULTS_DIR" \
        --expected-users "$LOAD_USERS" \
        --logs-since "$stateful_logs_since" || validation_status=$?
    if [ "$churn_status" -ne 0 ] || [ "$validation_status" -ne 0 ]; then
        exit 1
    fi
fi

if [ "$LOAD_RESILIENCE_CHECKS" = true ]; then
    fault_status=0
    set +e
    wait "$fault_pid"
    fault_status=$?
    fault_pid=""
    set -e

    validation_status=0
    python3 "$ROOT/tests/load/resilience_checks.py" validate \
        --compose-file "$COMPOSE_FILE" \
        --results-dir "$LOAD_RESULTS_DIR" \
        --expected-users "$LOAD_USERS" \
        --scenarios "$LOAD_RESILIENCE_SCENARIOS" \
        --recovery-grace-seconds "$LOAD_RESILIENCE_RECOVERY_GRACE_SECONDS" \
        --mapping-ttl-seconds "$LOAD_PII_MAPPING_TTL_SECONDS" \
        --cancellation-expiry-grace-seconds "$LOAD_CANCELLATION_EXPIRY_GRACE_SECONDS" \
        || validation_status=$?
    if [ "$fault_status" -ne 0 ] || [ "$validation_status" -ne 0 ]; then
        exit 1
    fi
fi

exit "$load_status"
