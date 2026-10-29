# Python 3.11 remains the supported Analyzer runtime.
FROM python:3.11-slim AS python-base

# The repository root is the build context in this branch so both Analyzer and
# LiteLLM can use the same optional corporate CA bundle.
COPY certs/ /tmp/proxy-certs/
RUN set -eu; \
    if find /tmp/proxy-certs -maxdepth 1 -type f -name '*.crt' | grep -q .; then \
        python -c "from pathlib import Path; files = sorted(Path('/tmp/proxy-certs').glob('*.crt')); Path('/tmp/proxy-ca.pem').write_bytes(b''.join(path.read_bytes().rstrip(b'\\r\\n') + b'\\n' for path in files))"; \
        python -c "import ssl; ssl.create_default_context(cafile='/tmp/proxy-ca.pem')"; \
        cat /tmp/proxy-ca.pem >> /etc/ssl/certs/ca-certificates.crt; \
        python -c "import ssl; ssl.create_default_context(cafile='/etc/ssl/certs/ca-certificates.crt')"; \
    fi; \
    rm -rf /tmp/proxy-certs /tmp/proxy-ca.pem

ENV SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt \
    REQUESTS_CA_BUNDLE=/etc/ssl/certs/ca-certificates.crt \
    CURL_CA_BUNDLE=/etc/ssl/certs/ca-certificates.crt \
    PIP_CERT=/etc/ssl/certs/ca-certificates.crt

ARG PIP_INDEX_URL=""
ARG PIP_TRUSTED_HOST=""
ARG PIP_PROXY=""
ARG PYTORCH_INDEX_URL="https://download.pytorch.org/whl/cpu"

# Model acquisition remains independent from Python dependencies. A dedicated
# proxy can override the generic HTTP(S)_PROXY only for this verified download.
FROM python-base AS model-download
ARG NER_MODEL_PROXY=""
WORKDIR /model-build
COPY presidio/model_artifact.py presidio/download_hf_model.py presidio/model_manifest.json ./
RUN set -eu; \
    if [ -n "$NER_MODEL_PROXY" ]; then \
        export HTTP_PROXY="$NER_MODEL_PROXY" HTTPS_PROXY="$NER_MODEL_PROXY"; \
        export http_proxy="$NER_MODEL_PROXY" https_proxy="$NER_MODEL_PROXY"; \
    fi; \
    python download_hf_model.py

# Supported architectures install all runtime packages from wheels.
FROM python-base AS analyzer-common-build

RUN --mount=type=cache,target=/root/.cache/pip \
    set -eu; \
    if [ -z "$PIP_INDEX_URL" ]; then unset PIP_INDEX_URL; fi; \
    if [ -z "$PIP_TRUSTED_HOST" ]; then unset PIP_TRUSTED_HOST; fi; \
    if [ -z "$PIP_PROXY" ]; then unset PIP_PROXY; fi; \
    python -m pip install --upgrade pip setuptools wheel

COPY presidio/requirements-analyzer.txt ./
RUN --mount=type=cache,target=/root/.cache/pip \
    set -eu; \
    if [ -z "$PIP_INDEX_URL" ]; then unset PIP_INDEX_URL; fi; \
    if [ -z "$PIP_TRUSTED_HOST" ]; then unset PIP_TRUSTED_HOST; fi; \
    if [ -z "$PIP_PROXY" ]; then unset PIP_PROXY; fi; \
    python -m pip install -r requirements-analyzer.txt

RUN --mount=type=cache,target=/root/.cache/pip \
    set -eu; \
    if [ -z "$PIP_INDEX_URL" ]; then unset PIP_INDEX_URL; fi; \
    if [ -z "$PIP_TRUSTED_HOST" ]; then unset PIP_TRUSTED_HOST; fi; \
    if [ -z "$PIP_PROXY" ]; then unset PIP_PROXY; fi; \
    python -m spacy download ru_core_news_sm

# Heavy libraries are confined to the source-model export and parity checks.
FROM analyzer-common-build AS triton-export-build
COPY presidio/requirements-export-cpu.txt ./
RUN --mount=type=cache,target=/root/.cache/pip \
    set -eu; \
    test -n "$PYTORCH_INDEX_URL"; \
    if [ -z "$PIP_TRUSTED_HOST" ]; then unset PIP_TRUSTED_HOST; fi; \
    if [ -z "$PIP_PROXY" ]; then unset PIP_PROXY; fi; \
    PIP_INDEX_URL="$PYTORCH_INDEX_URL" \
        python -m pip install -r requirements-export-cpu.txt

# Export must precede the runtime which copies its trusted metadata.
FROM triton-export-build AS triton-model-export
ENV HF_HUB_OFFLINE=1 TOKENIZERS_PARALLELISM=false
COPY presidio/requirements-onnx-export.txt ./
RUN --mount=type=cache,target=/root/.cache/pip \
    python -m pip install -r requirements-onnx-export.txt
COPY --from=model-download /opt/models /opt/models
COPY presidio/model_artifact.py presidio/model_profiles.py presidio/result_merging.py presidio/entity_types.py ./
COPY presidio/model_contract.py presidio/onnx_artifact.py presidio/string_spans.py ./
COPY presidio/ner/ ./ner/
COPY presidio/export_triton_model.py ./
COPY presidio/verify_triton_export.py ./
RUN python export_triton_model.py --out /export/models

# Lightweight test target without the model weights.
FROM analyzer-common-build AS analyzer-test
COPY presidio/requirements-analyzer-test.txt ./
RUN --mount=type=cache,target=/root/.cache/pip \
    set -eu; \
    if [ -z "$PIP_INDEX_URL" ]; then unset PIP_INDEX_URL; fi; \
    if [ -z "$PIP_TRUSTED_HOST" ]; then unset PIP_TRUSTED_HOST; fi; \
    if [ -z "$PIP_PROXY" ]; then unset PIP_PROXY; fi; \
    python -m pip install -r requirements-analyzer-test.txt

# Publish only the verified tokenizer/config, never the source-model weights.
FROM model-download AS runtime-artifact
RUN python -c "from pathlib import Path; import shutil; source = Path('/opt/models/rubert-tiny2-secret-detection'); target = Path('/runtime-models') / source.name; target.mkdir(parents=True); [shutil.copy2(p, target / p.name) for p in source.iterdir() if p.name != 'model.safetensors']"

# Production runtimes contain only shared libraries and installed Python files.
FROM python-base AS analyzer-runtime

RUN apt-get update && apt-get install -y --no-install-recommends libgomp1 && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app
ENV HF_HUB_OFFLINE=1 \
    HF_HUB_DISABLE_TELEMETRY=1 \
    TOKENIZERS_PARALLELISM=false \
    OMP_NUM_THREADS=2 \
    MKL_NUM_THREADS=2 \
    OPENBLAS_NUM_THREADS=2 \
    PYTHONDONTWRITEBYTECODE=1

COPY --from=runtime-artifact /runtime-models /opt/models
COPY presidio/analyzer_server.py .
COPY presidio/capacity.py .
COPY presidio/entity_types.py .
COPY presidio/model_artifact.py .
COPY presidio/model_manifest.json .
COPY presidio/model_contract.py presidio/onnx_artifact.py ./
COPY presidio/model_profiles.py .
COPY presidio/result_merging.py .
COPY presidio/string_spans.py .
COPY presidio/text_chunking.py .
COPY presidio/real_model_smoke.py .
COPY presidio/verify_cpu_runtime.py .
COPY presidio/recognizers/ ./recognizers/
COPY presidio/ner/ ./ner/

EXPOSE 5001
ENV PRESIDIO_ANALYZER_WORKERS=1
CMD ["sh", "-c", "exec uvicorn analyzer_server:app --host 0.0.0.0 --port 5001 --workers \"$PRESIDIO_ANALYZER_WORKERS\""]

FROM analyzer-runtime AS analyzer
COPY --from=analyzer-common-build /usr/local /usr/local
COPY --from=triton-model-export /export/models/tiny2/export-manifest.json /opt/ner-export/export-manifest.json
ENV PRESIDIO_ANALYZER_NER_BATCH_SIZE=4
RUN python verify_cpu_runtime.py

FROM nvcr.io/nvidia/tritonserver:26.08-py3@sha256:9185ba5b33d2c85c9204b208957b4bbfc6b2b89066efc9192e0f60c2ab58cec0 AS ner-triton
COPY --from=triton-model-export /export/models /models
COPY presidio/triton_entrypoint.py /opt/ru-ner/triton_entrypoint.py
COPY presidio/model_artifact.py presidio/model_contract.py presidio/onnx_artifact.py /opt/ru-ner/
COPY presidio/model_manifest.json /opt/ru-ner/
COPY presidio/verify_triton_service.py /opt/ru-ner/
ENV TRITON_MODEL_INSTANCES=2
EXPOSE 8000 8002
ENTRYPOINT ["python3", "/opt/ru-ner/triton_entrypoint.py"]
