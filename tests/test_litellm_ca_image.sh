#!/usr/bin/env bash
set -euo pipefail

DOCKER_DESKTOP_BIN="/Applications/Docker.app/Contents/Resources/bin"
if [[ -d "${DOCKER_DESKTOP_BIN}" ]]; then
    export PATH="${DOCKER_DESKTOP_BIN}:${PATH}"
fi

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TEMP_DIR="$(mktemp -d)"
CONTEXT_DIR="${TEMP_DIR}/context"
IMAGE_NO_CA="ru-llm-proxy-litellm-ca-test:no-ca"
IMAGE_VALID_CA="ru-llm-proxy-litellm-ca-test:valid-ca"
IMAGE_INVALID_CA="ru-llm-proxy-litellm-ca-test:invalid-ca"
ANALYZER_NO_CA="ru-llm-proxy-analyzer-ca-test:no-ca"
ANALYZER_VALID_CA="ru-llm-proxy-analyzer-ca-test:valid-ca"
ANALYZER_INVALID_CA="ru-llm-proxy-analyzer-ca-test:invalid-ca"
CODEX_LB_NO_CA="ru-llm-proxy-codex-lb-ca-test:no-ca"
CODEX_LB_VALID_CA="ru-llm-proxy-codex-lb-ca-test:valid-ca"
CODEX_LB_INVALID_CA="ru-llm-proxy-codex-lb-ca-test:invalid-ca"

cleanup() {
    docker image rm -f \
        "${IMAGE_NO_CA}" "${IMAGE_VALID_CA}" "${IMAGE_INVALID_CA}" \
        "${ANALYZER_NO_CA}" "${ANALYZER_VALID_CA}" "${ANALYZER_INVALID_CA}" \
        "${CODEX_LB_NO_CA}" "${CODEX_LB_VALID_CA}" "${CODEX_LB_INVALID_CA}" \
        >/dev/null 2>&1 || true
    rm -rf "${TEMP_DIR}"
}
trap cleanup EXIT

mkdir -p \
    "${CONTEXT_DIR}/litellm" \
    "${CONTEXT_DIR}/presidio" \
    "${CONTEXT_DIR}/codex-lb" \
    "${CONTEXT_DIR}/certs"
cp "${ROOT_DIR}/litellm/Dockerfile" "${CONTEXT_DIR}/litellm/Dockerfile"
cp "${ROOT_DIR}/presidio/Dockerfile" "${CONTEXT_DIR}/presidio/Dockerfile"
cp "${ROOT_DIR}/codex-lb/Dockerfile" "${CONTEXT_DIR}/codex-lb/Dockerfile"
cp "${ROOT_DIR}/certs/README.md" "${CONTEXT_DIR}/certs/README.md"

echo "Building LiteLLM image without a custom CA"
docker build --file "${CONTEXT_DIR}/litellm/Dockerfile" --tag "${IMAGE_NO_CA}" "${CONTEXT_DIR}"

echo "Building Analyzer base image without a custom CA"
docker build \
    --file "${CONTEXT_DIR}/presidio/Dockerfile" \
    --target python-base \
    --tag "${ANALYZER_NO_CA}" \
    "${CONTEXT_DIR}"

echo "Building codex-lb image without a custom CA"
docker build --file "${CONTEXT_DIR}/codex-lb/Dockerfile" --tag "${CODEX_LB_NO_CA}" "${CONTEXT_DIR}"

for index in 1 2; do
    openssl req -x509 -newkey rsa:2048 -sha256 -days 1 -nodes \
        -subj "/CN=ru-llm-proxy-test-ca-${index}" \
        -keyout "${TEMP_DIR}/ca-${index}.key" \
        -out "${TEMP_DIR}/ca-${index}.crt" >/dev/null 2>&1
    cp "${TEMP_DIR}/ca-${index}.crt" "${CONTEXT_DIR}/certs/ca-${index}.crt"
done

# Reproduce a common broken-chain input: the first PEM has no trailing newline.
python3 -c "from pathlib import Path; path = Path('${CONTEXT_DIR}/certs/ca-1.crt'); path.write_bytes(path.read_bytes().rstrip(b'\\r\\n'))"

echo "Building LiteLLM image with multiple custom CAs"
docker build --file "${CONTEXT_DIR}/litellm/Dockerfile" --tag "${IMAGE_VALID_CA}" "${CONTEXT_DIR}"

echo "Building Analyzer base image with multiple custom CAs"
docker build \
    --file "${CONTEXT_DIR}/presidio/Dockerfile" \
    --target python-base \
    --tag "${ANALYZER_VALID_CA}" \
    "${CONTEXT_DIR}"

echo "Building codex-lb image with multiple custom CAs"
docker build --file "${CONTEXT_DIR}/codex-lb/Dockerfile" --tag "${CODEX_LB_VALID_CA}" "${CONTEXT_DIR}"

docker run --rm \
    --entrypoint sh \
    -v "${TEMP_DIR}:/tmp/test-certs:ro" \
    "${IMAGE_VALID_CA}" \
    -c '
        set -eu
        test "$SSL_CERT_FILE" = /etc/ssl/certs/ca-certificates.crt
        test "$REQUESTS_CA_BUNDLE" = /etc/ssl/certs/ca-certificates.crt
        test "$CURL_CA_BUNDLE" = /etc/ssl/certs/ca-certificates.crt
        test "$NODE_EXTRA_CA_CERTS" = /etc/ssl/certs/ca-certificates.crt
        CERTIFI_BUNDLE="$(/app/.venv/bin/python -c "import certifi; print(certifi.where())")"
        for cert in /tmp/test-certs/ca-1.crt /tmp/test-certs/ca-2.crt; do
            openssl verify -CAfile /etc/ssl/certs/ca-certificates.crt "$cert"
            openssl verify -CAfile "$CERTIFI_BUNDLE" "$cert"
        done
    '

docker run --rm \
    --entrypoint python \
    -v "${TEMP_DIR}:/tmp/test-certs:ro" \
    "${ANALYZER_VALID_CA}" \
    -c '
import os
import ssl
from pathlib import Path

assert os.environ["SSL_CERT_FILE"] == "/etc/ssl/certs/ca-certificates.crt"
bundle = Path(os.environ["SSL_CERT_FILE"]).read_text()
ssl.create_default_context(cafile=os.environ["SSL_CERT_FILE"])
for path in ("/tmp/test-certs/ca-1.crt", "/tmp/test-certs/ca-2.crt"):
    certificate = Path(path).read_text()
    assert "".join(certificate.split()) in "".join(bundle.split())
'

test "$(docker image inspect --format '{{.Config.User}}' "${CODEX_LB_VALID_CA}")" = "app"
docker run --rm \
    --entrypoint sh \
    -v "${TEMP_DIR}:/tmp/test-certs:ro" \
    "${CODEX_LB_VALID_CA}" \
    -c '
        set -eu
        test "$SSL_CERT_FILE" = /etc/ssl/certs/ca-certificates.crt
        test "$REQUESTS_CA_BUNDLE" = /etc/ssl/certs/ca-certificates.crt
        test "$CURL_CA_BUNDLE" = /etc/ssl/certs/ca-certificates.crt
        CERTIFI_BUNDLE="$(/opt/venv/bin/python -c "import certifi; print(certifi.where())")"
        for cert in /tmp/test-certs/ca-1.crt /tmp/test-certs/ca-2.crt; do
            openssl verify -CAfile /etc/ssl/certs/ca-certificates.crt "$cert"
            openssl verify -CAfile "$CERTIFI_BUNDLE" "$cert"
        done
    '

rm -f "${CONTEXT_DIR}/certs/ca-1.crt" "${CONTEXT_DIR}/certs/ca-2.crt"
printf '%s\n' 'not a PEM certificate' > "${CONTEXT_DIR}/certs/invalid.crt"

echo "Checking that an invalid custom CA fails the image build"
if docker build --file "${CONTEXT_DIR}/litellm/Dockerfile" --tag "${IMAGE_INVALID_CA}" "${CONTEXT_DIR}"; then
    echo "LiteLLM image unexpectedly accepted an invalid custom CA" >&2
    exit 1
fi

if docker build \
    --file "${CONTEXT_DIR}/presidio/Dockerfile" \
    --target python-base \
    --tag "${ANALYZER_INVALID_CA}" \
    "${CONTEXT_DIR}"; then
    echo "Analyzer image unexpectedly accepted an invalid custom CA" >&2
    exit 1
fi

if docker build --file "${CONTEXT_DIR}/codex-lb/Dockerfile" --tag "${CODEX_LB_INVALID_CA}" "${CONTEXT_DIR}"; then
    echo "codex-lb image unexpectedly accepted an invalid custom CA" >&2
    exit 1
fi

echo "Custom CA image checks passed"
