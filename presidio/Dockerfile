# Python 3.11 base (3.12 incompatible with deeppavlov's numpy dependency)
FROM python:3.11-slim AS base

RUN apt-get update && apt-get install -y --no-install-recommends curl && \
    rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir --upgrade pip setuptools wheel

# === Presidio Analyzer ===
FROM base AS analyzer

# Install dependencies
RUN pip install --no-cache-dir \
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
