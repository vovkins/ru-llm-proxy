# Thin image over the official LiteLLM image: bakes the corporate proxy root CA into the
# venv's certifi bundle (the interpreter LiteLLM actually runs — see the /app/.venv path in
# tracebacks) so outbound TLS to providers through the re-signing proxy is trusted, instead of
# disabling verification. Build context is the repo root — see docker-compose.yml.
FROM docker.litellm.ai/berriai/litellm:main-stable

# Copy the whole certs/ directory (always present — see certs/README.md) and append the CA
# only if one is supplied, so the image also builds with no proxy CA (open-internet builds
# keep stock certifi).
COPY certs/ /tmp/proxy-certs/
RUN if ls /tmp/proxy-certs/*.crt >/dev/null 2>&1; then \
        cat /tmp/proxy-certs/*.crt \
            >> "$(/app/.venv/bin/python -c 'import certifi; print(certifi.where())')"; \
    fi && rm -rf /tmp/proxy-certs
