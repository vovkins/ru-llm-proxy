#!/bin/sh
set -eu

if [ "${USE_DDTRACE:-false}" = "true" ]; then
    export DD_TRACE_OPENAI_ENABLED="False"
    exec ddtrace-run python -m litellm_guardrails.server "$@"
fi
exec python -m litellm_guardrails.server "$@"
