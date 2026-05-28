# Python base
FROM python:3.12-slim AS base

RUN apt-get update && apt-get install -y --no-install-recommends curl && \
    rm -rf /var/lib/apt/lists/*

# === Presidio Analyzer ===
FROM base AS analyzer

RUN pip install --no-cache-dir \
    presidio-analyzer>=2.2.362 \
    presidio-anonymizer>=2.2.362 \
    spacy>=3.8 \
    fastapi>=0.115 \
    uvicorn>=0.34

RUN python -m spacy download ru_core_web_sm

WORKDIR /app
COPY analyzer_server.py .
COPY recognizers/ ./recognizers/

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
