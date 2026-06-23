# Python 3.11 base (3.12 incompatible with deeppavlov's numpy dependency)
FROM python:3.11-slim AS base

RUN apt-get update && apt-get install -y --no-install-recommends curl gcc g++ && \
    rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir --upgrade pip setuptools wheel

# === Presidio Analyzer ===
FROM base AS analyzer

COPY requirements-analyzer.txt requirements-torchcrf.txt ./
RUN pip install --no-cache-dir -r requirements-analyzer.txt

# Install DeepPavlov without deps. Its transitive pins include older packages
# that are not suitable for this Python 3.11 image.
RUN pip install --no-cache-dir --no-deps 'deeppavlov>=1.0,<2.0'

# pytorch-crf must be installed AFTER torch.
RUN pip install --no-cache-dir -r requirements-torchcrf.txt

# Download spaCy Russian model
RUN python -m spacy download ru_core_news_sm

# Pre-download DeepPavlov ner_rus_bert model (~1.4 GB)
# Only download files, don't build model (avoids transformers compat issues at build time)
ARG DEEPPAVLOV_NER_MODEL_URL="http://files.deeppavlov.ai/v1/ner/ner_rus_bert_torch_new.tar.gz"
ARG DEEPPAVLOV_NER_MODEL_SHA256=""
ARG DEEPPAVLOV_NER_DOWNLOAD_TIMEOUT_SECONDS="120"
ENV DEEPPAVLOV_NER_MODEL_URL=${DEEPPAVLOV_NER_MODEL_URL}
ENV DEEPPAVLOV_NER_MODEL_SHA256=${DEEPPAVLOV_NER_MODEL_SHA256}
ENV DEEPPAVLOV_NER_DOWNLOAD_TIMEOUT_SECONDS=${DEEPPAVLOV_NER_DOWNLOAD_TIMEOUT_SECONDS}
COPY download_model.py .
RUN python download_model.py

WORKDIR /app
COPY analyzer_server.py .
COPY capacity.py .
COPY recognizers/ ./recognizers/
COPY ner/ ./ner/

EXPOSE 5001
ENV PRESIDIO_ANALYZER_WORKERS=1
CMD ["sh", "-c", "exec uvicorn analyzer_server:app --host 0.0.0.0 --port 5001 --workers \"$PRESIDIO_ANALYZER_WORKERS\""]
