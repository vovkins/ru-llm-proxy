# Python 3.11 remains the supported Analyzer runtime for this migration.

# Model acquisition is independent from Python runtime dependencies. Dependency
# changes therefore do not invalidate the pinned 709 MB model layer.
FROM python:3.11-slim AS model-download
WORKDIR /model-build
COPY model_artifact.py download_hf_model.py model_manifest.json ./
RUN python download_hf_model.py

# Dependency stage. Supported architectures install all packages from wheels,
# so compilers are neither downloaded nor copied into the production image.
FROM python:3.11-slim AS analyzer-build

RUN python -m pip install --upgrade pip setuptools wheel

# Install the large CPU wheel separately so changes to common dependencies can
# reuse its layer and BuildKit package cache.
COPY requirements-analyzer-cpu.txt ./
RUN --mount=type=cache,target=/root/.cache/pip \
    python -m pip install -r requirements-analyzer-cpu.txt

COPY requirements-analyzer.txt ./
RUN --mount=type=cache,target=/root/.cache/pip \
    python -m pip install -r requirements-analyzer.txt

RUN --mount=type=cache,target=/root/.cache/pip \
    python -m spacy download ru_core_news_sm

# Lightweight test target without the large model weights.
FROM analyzer-build AS analyzer-test
COPY requirements-analyzer-test.txt ./
RUN --mount=type=cache,target=/root/.cache/pip \
    python -m pip install -r requirements-analyzer-test.txt

# Production runtime contains only shared libraries and installed Python files.
FROM python:3.11-slim AS analyzer

RUN apt-get update && apt-get install -y --no-install-recommends libgomp1 && \
    rm -rf /var/lib/apt/lists/*

COPY --from=analyzer-build /usr/local /usr/local

WORKDIR /app
ENV HF_HUB_OFFLINE=1 \
    TRANSFORMERS_OFFLINE=1 \
    HF_HUB_DISABLE_TELEMETRY=1 \
    TOKENIZERS_PARALLELISM=false \
    PYTHONDONTWRITEBYTECODE=1

COPY verify_cpu_runtime.py .
RUN python verify_cpu_runtime.py

COPY --from=model-download /opt/models/ner_rus_bert-secret_detection /opt/models/ner_rus_bert-secret_detection
COPY analyzer_server.py .
COPY capacity.py .
COPY entity_types.py .
COPY model_artifact.py .
COPY result_merging.py .
COPY real_model_smoke.py .
COPY recognizers/ ./recognizers/
COPY ner/ ./ner/

EXPOSE 5001
ENV PRESIDIO_ANALYZER_WORKERS=1
CMD ["sh", "-c", "exec uvicorn analyzer_server:app --host 0.0.0.0 --port 5001 --workers \"$PRESIDIO_ANALYZER_WORKERS\""]
