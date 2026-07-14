# Python 3.11 base (3.12 incompatible with deeppavlov's numpy dependency)
FROM python:3.11-slim AS base

# Trust the corporate proxy's root CA instead of disabling TLS verification. The proxy
# intercepts and re-signs all outbound HTTPS, so apt / pip / spaCy / DeepPavlov / HuggingFace
# verify against this CA. Single source of truth: certs/proxy-ca.crt (the same file the
# litellm image bakes in). NOTE: the build context is the repo root — see docker-compose.yml.
# The whole certs/ directory is copied (it always exists — see certs/README.md), so the build
# also works with NO proxy CA supplied (open-internet builds): the install step is skipped.
COPY certs/ /tmp/proxy-certs/
RUN if ls /tmp/proxy-certs/*.crt >/dev/null 2>&1; then \
        cp /tmp/proxy-certs/*.crt /usr/local/share/ca-certificates/ && \
        update-ca-certificates; \
    fi && rm -rf /tmp/proxy-certs

# Point every client at the system trust store (which now includes the proxy CA, if supplied):
#  - SSL_CERT_FILE / REQUESTS_CA_BUNDLE: stdlib ssl (urllib) and requests/urllib3
#  - CURL_CA_BUNDLE: curl
#  - PIP_CERT: pip, which otherwise uses its own vendored certifi bundle
ENV SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt \
    REQUESTS_CA_BUNDLE=/etc/ssl/certs/ca-certificates.crt \
    CURL_CA_BUNDLE=/etc/ssl/certs/ca-certificates.crt \
    PIP_CERT=/etc/ssl/certs/ca-certificates.crt

# Corporate proxies commonly block plain-HTTP egress and only allow HTTPS via CONNECT, so pin
# apt to https. HTTP_PROXY/HTTPS_PROXY/NO_PROXY are Docker predefined build args (forwarded
# from docker-compose build.args) — BuildKit injects them into RUN automatically and scrubs
# them from the final image, so apt reaches the Debian mirrors through the proxy at build time.
# With no proxy args set, apt goes direct.
RUN sed -i 's|http://|https://|g' /etc/apt/sources.list.d/debian.sources 2>/dev/null || true && \
    sed -i 's|http://|https://|g' /etc/apt/sources.list 2>/dev/null || true && \
    apt-get update && apt-get install -y --no-install-recommends curl gcc g++ && \
    rm -rf /var/lib/apt/lists/*

# Internal PyPI mirror (supplied via docker-compose build args). The corporate proxy blocks
# CONNECT tunnels to public pypi.org, so pip must use the mirror instead. When PIP_INDEX_URL
# is empty, pip falls back to public PyPI. Set PIP_TRUSTED_HOST if the mirror serves plain HTTP
# or a cert outside the proxy CA. PIP_PROXY lets pip use a dedicated proxy node — it is written
# into pip.conf so it overrides the generic *_PROXY env for pip only.
ARG PIP_INDEX_URL=""
ARG PIP_TRUSTED_HOST=""
ARG PIP_PROXY=""
RUN printf '[global]\n' > /etc/pip.conf && \
    if [ -n "$PIP_INDEX_URL" ]; then printf 'index-url = %s\n' "$PIP_INDEX_URL" >> /etc/pip.conf; fi && \
    if [ -n "$PIP_TRUSTED_HOST" ]; then printf 'trusted-host = %s\n' "$PIP_TRUSTED_HOST" >> /etc/pip.conf; fi && \
    if [ -n "$PIP_PROXY" ]; then printf 'proxy = %s\n' "$PIP_PROXY" >> /etc/pip.conf; fi
ENV PIP_CONFIG_FILE=/etc/pip.conf

RUN pip install --no-cache-dir --upgrade pip setuptools wheel

# === Analyzer dependencies (no NER model) — shared by the test and runtime targets ===
FROM base AS analyzer-runtime

COPY presidio/requirements-analyzer.txt presidio/requirements-torchcrf.txt ./
RUN pip install --no-cache-dir -r requirements-analyzer.txt

# huggingface_hub v1.x (and any other httpx-based client) verifies against certifi's bundle,
# not SSL_CERT_FILE, so append the proxy CA to certifi too — this is what lets the runtime
# HuggingFace download of the rubert weights verify against the proxy CA. Skipped when no CA
# was supplied (open-internet builds keep stock certifi).
RUN if ls /usr/local/share/ca-certificates/*.crt >/dev/null 2>&1; then \
        cat /usr/local/share/ca-certificates/*.crt \
            >> "$(python -c 'import certifi; print(certifi.where())')"; \
    fi

# Install DeepPavlov without deps. Its transitive pins include older packages
# that are not suitable for this Python 3.11 image.
RUN pip install --no-cache-dir --no-deps 'deeppavlov>=1.0,<2.0'

# pytorch-crf must be installed AFTER torch.
RUN pip install --no-cache-dir -r requirements-torchcrf.txt

# Remove the hf_xet Rust downloader and force huggingface_hub onto the plain HTTPS path, which
# honors certifi / SSL_CERT_FILE and thus the proxy CA. Belt-and-suspenders with
# HF_HUB_DISABLE_XET, which has version bugs.
RUN pip uninstall -y hf_xet 2>/dev/null || true

# Download spaCy Russian model
RUN python -m spacy download ru_core_news_sm

# Lightweight test target: Analyzer/FastAPI dependencies + Russian NLP engine without the
# large pre-downloaded DeepPavlov model archive. Used by the presidio-analyzer-tests service.
FROM analyzer-runtime AS analyzer-test

# === Presidio Analyzer (runtime) ===
FROM analyzer-runtime AS analyzer

# Pre-download DeepPavlov ner_rus_bert model (~1.4 GB)
# Only download files, don't build model (avoids transformers compat issues at build time)
ARG DEEPPAVLOV_NER_MODEL_URL="http://files.deeppavlov.ai/v1/ner/ner_rus_bert_torch_new.tar.gz"
ARG DEEPPAVLOV_NER_MODEL_SHA256=""
ARG DEEPPAVLOV_NER_DOWNLOAD_TIMEOUT_SECONDS="120"
ENV DEEPPAVLOV_NER_MODEL_URL=${DEEPPAVLOV_NER_MODEL_URL}
ENV DEEPPAVLOV_NER_MODEL_SHA256=${DEEPPAVLOV_NER_MODEL_SHA256}
ENV DEEPPAVLOV_NER_DOWNLOAD_TIMEOUT_SECONDS=${DEEPPAVLOV_NER_DOWNLOAD_TIMEOUT_SECONDS}
COPY presidio/download_model.py .
RUN python download_model.py

WORKDIR /app
COPY presidio/analyzer_server.py .
COPY presidio/capacity.py .
COPY presidio/recognizers/ ./recognizers/
COPY presidio/ner/ ./ner/

EXPOSE 5001
ENV PRESIDIO_ANALYZER_WORKERS=1
CMD ["sh", "-c", "exec uvicorn analyzer_server:app --host 0.0.0.0 --port 5001 --workers \"$PRESIDIO_ANALYZER_WORKERS\""]
