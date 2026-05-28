# Python 3.11 base (3.12 incompatible with deeppavlov's numpy dependency)
FROM python:3.11-slim AS base

RUN apt-get update && apt-get install -y --no-install-recommends curl gcc g++ && \
    rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir --upgrade pip setuptools wheel

# === Presidio Analyzer ===
FROM base AS analyzer

# Pre-install binary wheels for numpy, scikit-learn, scipy
# (deeppavlov pins old versions that fail to build from source)
RUN pip install --no-cache-dir \
    'numpy>=1.26,<2.2' \
    'scikit-learn>=1.3' \
    'scipy>=1.10'

# Install deeppavlov without deps, then add its missing runtime deps manually
RUN pip install --no-cache-dir --no-deps deeppavlov>=1.0

# Deeppavlov runtime dependencies (compatible versions)
RUN pip install --no-cache-dir \
    nltk>=3.2.4 \
    pandas>=1.0.0 \
    prometheus-client>=0.13.0 \
    pybind11==2.10.3 \
    tqdm>=4.42.0 \
    torchcrf>=0.1.0 \
    requests>=2.19.0 \
    filelock>=3.0.0 \
    pydantic>=1.0

# Install remaining service dependencies
RUN pip install --no-cache-dir \
    presidio-analyzer>=2.2.362 \
    presidio-anonymizer>=2.2.362 \
    spacy>=3.8 \
    fastapi>=0.115 \
    uvicorn>=0.34 \
    torch>=2.0 \
    transformers>=4.30

# torchcrf must be installed AFTER torch
RUN pip install --no-cache-dir torchcrf>=0.1.0

# Download spaCy Russian model
RUN python -m spacy download ru_core_news_sm

# Pre-download DeepPavlov ner_rus_bert model (~1.4 GB)
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
