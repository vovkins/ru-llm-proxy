# Специализированный образ для корпоративного прокси, переподписывающего TLS.
FROM docker.litellm.ai/berriai/litellm:main-stable

COPY certs/ /tmp/proxy-certs/
RUN set -eu; \
    if find /tmp/proxy-certs -maxdepth 1 -type f -name '*.crt' | grep -q .; then \
        CERTIFI_BUNDLE="$(/app/.venv/bin/python -c 'import certifi; print(certifi.where())')"; \
        find /tmp/proxy-certs -maxdepth 1 -type f -name '*.crt' -exec cat {} \; >> "$CERTIFI_BUNDLE"; \
    fi; \
    rm -rf /tmp/proxy-certs
