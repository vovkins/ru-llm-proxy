#!/usr/bin/env bash
set -euo pipefail

ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
MATRIX=${1:-help}
MATRIX_RESULTS_DIR=${LOAD_MATRIX_RESULTS_DIR:-$(mktemp -d /tmp/ru-llm-proxy-matrix-XXXXXX)}
MATRIX_RUN_TIME=${LOAD_MATRIX_RUN_TIME:-5m}
MATRIX_USERS=${LOAD_MATRIX_USERS:-400}
ANALYZER_MATRIX_LITELLM_REPLICAS=${LOAD_ANALYZER_MATRIX_LITELLM_REPLICAS:-2}

usage() {
    cat <<'EOF'
Usage: LOAD_ALLOW_SCALING_MATRIX=true tests/load/run_matrix.sh MODE

Modes:
  calibration  Check one Locust node and mock-provider without LiteLLM/Analyzer
  baseline     Run the progressive 1 Analyzer / 1 LiteLLM baseline
  analyzer     Compare Analyzer replicas from LOAD_ANALYZER_MATRIX (default: 1 2 4)
               with LOAD_ANALYZER_MATRIX_LITELLM_REPLICAS LiteLLM replicas (default: 2)
  litellm      Compare LiteLLM replicas from LOAD_LITELLM_MATRIX (default: 1 2 4)
  combined     Run LOAD_COMBINED_MATRIX pairs (default: 2:1 4:1 4:2)

Results are written outside the repository by default. Every run uses unique
synthetic input so the Analyzer result cache does not hide compute saturation.
The LiteLLM-only mode uses the lightweight analyzer simulator and therefore
does not verify masking restoration; combined modes always use the real model.
EOF
}

run_case() {
    name=$1
    profile=$2
    contour=$3
    analyzer_replicas=$4
    litellm_replicas=$5
    analyzer_backend=${6:-real}
    analyzer_url=http://load-analyzer-router:5001
    validate_mapping=$([ "$contour" = mock ] && printf true || printf false)
    if [ "$analyzer_backend" = mock ]; then
        analyzer_url=http://load-mock-upstream:8080
        validate_mapping=false
    fi
    results_dir="$MATRIX_RESULTS_DIR/$name"
    mkdir -p "$results_dir"
    echo "Running $name: Analyzer=$analyzer_replicas LiteLLM=$litellm_replicas"
    LOAD_RESULTS_DIR="$results_dir" \
    LOAD_REPORT_NODE="$name" \
    LOAD_CONTOUR="$contour" \
    LOAD_ANALYZER_REPLICAS="$analyzer_replicas" \
    LOAD_LITELLM_REPLICAS="$litellm_replicas" \
    LOAD_INPUT_VARIATION=unique \
    LOAD_ANALYZER_BACKEND="$analyzer_backend" \
    LOAD_ANALYZER_URL="$analyzer_url" \
    LOAD_VALIDATE_MAPPING="$validate_mapping" \
    LOAD_USERS="$MATRIX_USERS" \
    LOAD_RUN_TIME="$MATRIX_RUN_TIME" \
    "$ROOT/tests/load/run.sh" "$profile"
}

if [ "$MATRIX" = help ] || [ "$MATRIX" = -h ] || [ "$MATRIX" = --help ]; then
    usage
    exit 0
fi
if [ "${LOAD_ALLOW_SCALING_MATRIX:-false}" != true ]; then
    echo "Set LOAD_ALLOW_SCALING_MATRIX=true for an intentional scaling run" >&2
    exit 2
fi
case "$MATRIX_RESULTS_DIR" in
    /*) ;;
    *) echo "LOAD_MATRIX_RESULTS_DIR must be an absolute path" >&2; exit 2 ;;
esac
mkdir -p "$MATRIX_RESULTS_DIR"

case "$MATRIX" in
    calibration)
        run_case calibration steady mock-direct 0 0
        ;;
    baseline)
        run_case baseline stages mock 1 1
        ;;
    analyzer)
        for replicas in ${LOAD_ANALYZER_MATRIX:-1 2 4}; do
            run_case "analyzer-$replicas" steady mock "$replicas" \
                "$ANALYZER_MATRIX_LITELLM_REPLICAS"
        done
        ;;
    litellm)
        for replicas in ${LOAD_LITELLM_MATRIX:-1 2 4}; do
            run_case "litellm-$replicas" steady mock \
                1 "$replicas" mock
        done
        ;;
    combined)
        for pair in ${LOAD_COMBINED_MATRIX:-2:1 4:1 4:2}; do
            analyzer_replicas=${pair%%:*}
            litellm_replicas=${pair##*:}
            run_case "combined-$analyzer_replicas-$litellm_replicas" steady mock \
                "$analyzer_replicas" "$litellm_replicas"
        done
        ;;
    *)
        echo "Unknown matrix mode: $MATRIX" >&2
        usage >&2
        exit 2
        ;;
esac

python3 "$ROOT/tests/load/compare_runs.py" "$MATRIX_RESULTS_DIR"
echo "Scaling matrix reports: $MATRIX_RESULTS_DIR"
