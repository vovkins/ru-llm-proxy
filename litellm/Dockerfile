# Специализированный образ для корпоративного прокси, переподписывающего TLS.
FROM docker.litellm.ai/berriai/litellm:main-stable

COPY certs/ /tmp/proxy-certs/
RUN set -eu; \
    if find /tmp/proxy-certs -maxdepth 1 -type f -name '*.crt' | grep -q .; then \
        find /tmp/proxy-certs -maxdepth 1 -type f -name '*.crt' -exec awk 1 {} + > /tmp/proxy-ca.pem; \
        grep -q -- '-----BEGIN CERTIFICATE-----' /tmp/proxy-ca.pem; \
        openssl crl2pkcs7 -nocrl -certfile /tmp/proxy-ca.pem \
            | openssl pkcs7 -print_certs -noout >/dev/null; \
        SYSTEM_BUNDLE=/etc/ssl/certs/ca-certificates.crt; \
        CERTIFI_BUNDLE="$(/app/.venv/bin/python -c 'import certifi; print(certifi.where())')"; \
        cat /tmp/proxy-ca.pem >> "$SYSTEM_BUNDLE"; \
        openssl crl2pkcs7 -nocrl -certfile "$SYSTEM_BUNDLE" \
            | openssl pkcs7 -print_certs -noout >/dev/null; \
        if [ "$CERTIFI_BUNDLE" != "$SYSTEM_BUNDLE" ]; then \
            cat /tmp/proxy-ca.pem >> "$CERTIFI_BUNDLE"; \
            openssl crl2pkcs7 -nocrl -certfile "$CERTIFI_BUNDLE" \
                | openssl pkcs7 -print_certs -noout >/dev/null; \
        fi; \
    fi; \
    rm -rf /tmp/proxy-certs /tmp/proxy-ca.pem

ENV SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt \
    REQUESTS_CA_BUNDLE=/etc/ssl/certs/ca-certificates.crt \
    CURL_CA_BUNDLE=/etc/ssl/certs/ca-certificates.crt \
    NODE_EXTRA_CA_CERTS=/etc/ssl/certs/ca-certificates.crt
