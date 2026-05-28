# Python base (full image for build tools)
FROM python:3.12 AS base

RUN apt-get update && apt-get install -y --no-install-recommends curl && \
    rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir --upgrade pip setuptools wheel

# === Presidio Analyzer ===
FROM base AS analyzer

# Install numpy first (pre-built wheel to avoid build isolation issues)
RUN pip install --no-cache-dir 'numpy>=1.26,<2.2'

# Install dependencies with no build isolation (setuptools already installed)
RUN pip install --no-cache-dir --no-build-isolation \
    presidio-analyzer>=2.2.362 \
    presidio-anonymizer>=2.2.362 \
    spacy>=3.8 \
    fastapi>=0.115 \
    uvicorn>=0.34 \
    deeppavlov>=1.0

# Download spaCy Russian model
RUN python -m spacy download ru_core_web_sm

# Pre-download DeepPavlov ner_rus_bert model (~700 MB)
RUN python -c "from deeppavlov import build_model, configs; build_model(configs.ner.ner_rus_bert, download=True); print('DeepPavlov model downloaded')"

WORKDIR /app
COPY analyzer_server.py .
COPY recognizers/ ./recognizers/
COPY ner/ ./ner/

EXPOSE 5001
CMD ["python", "analyzer_server.py"]

# === Presidio Anonymizer ===
FROM base AS anonymizer

RUN pip install --no-cache-dir \
    presidio-anonymizer>=2.2.362 \
    fastapi>=0.115 \
    uvicorn>=0.34

WORKDIR /app
COPY anonymizer_server.py .

EXPOSE 5002
CMD ["python", "anonymizer_server.py"]
