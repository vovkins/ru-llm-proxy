#!/usr/bin/env python3
"""Download DeepPavlov ner_rus_bert model files without building the model."""
import os
import tarfile
import urllib.request

url = "http://files.deeppavlov.ai/v1/ner/ner_rus_bert_torch_new.tar.gz"
dest = "/root/.deeppavlov/models/ner_rus_bert_torch_new.tar.gz"
out_dir = "/root/.deeppavlov/models/ner_rus_bert_torch"

os.makedirs(os.path.dirname(dest), exist_ok=True)

if not os.path.exists(out_dir):
    print("Downloading model...")
    urllib.request.urlretrieve(url, dest)
    print("Extracting model...")
    with tarfile.open(dest, "r:gz") as tar:
        tar.extractall(os.path.dirname(dest))
    os.remove(dest)
    print("Model downloaded and extracted")
else:
    print("Model already exists")
