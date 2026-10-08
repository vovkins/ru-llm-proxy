#!/usr/bin/env bash
set -euo pipefail
exec "${PYTHON_LOCAL:-python3}" scripts/deployment.py \
    --stack "${1:-}" --env-file "${2:-.env}" health "${3:-0}"
