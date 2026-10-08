#!/usr/bin/env bash
set -euo pipefail
exec "${PYTHON_LOCAL:-python3}" scripts/deployment.py \
    --stack "${2:-}" --env-file "${3:-.env}" guard "${1:-}"
