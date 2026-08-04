# Python 3.11 remains the supported Analyzer runtime for this migration.
FROM python:3.11-slim AS base

RUN apt-get update && apt-get install -y --no-install-recommends curl gcc g++ && \
    rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir --upgrade pip setuptools wheel

# === Presidio Analyzer dependencies ===
FROM base AS analyzer-runtime

COPY requirements-analyzer.txt ./
RUN pip install --no-cache-dir -r requirements-analyzer.txt

# Download spaCy Russian model
RUN python -m spacy download ru_core_news_sm

# Lightweight test target without the large model weights.
FROM analyzer-runtime AS analyzer-test
COPY requirements-analyzer-test.txt ./
RUN pip install --no-cache-dir -r requirements-analyzer-test.txt

# Download only the reviewed files from the immutable model revision.
FROM analyzer-runtime AS model-download
WORKDIR /model-build
COPY model_artifact.py download_hf_model.py model_manifest.json ./
RUN python download_hf_model.py

# === Presidio Analyzer ===
FROM analyzer-runtime AS analyzer
WORKDIR /app
ENV HF_HUB_OFFLINE=1 \
    TRANSFORMERS_OFFLINE=1 \
    HF_HUB_DISABLE_TELEMETRY=1 \
    TOKENIZERS_PARALLELISM=false
COPY --from=model-download /opt/models/ner_rus_bert-secret_detection /opt/models/ner_rus_bert-secret_detection
COPY analyzer_server.py .
COPY capacity.py .
COPY model_artifact.py .
COPY result_merging.py .
COPY real_model_smoke.py .
COPY recognizers/ ./recognizers/
COPY ner/ ./ner/

EXPOSE 5001
ENV PRESIDIO_ANALYZER_WORKERS=1
CMD ["sh", "-c", "exec uvicorn analyzer_server:app --host 0.0.0.0 --port 5001 --workers \"$PRESIDIO_ANALYZER_WORKERS\""]
