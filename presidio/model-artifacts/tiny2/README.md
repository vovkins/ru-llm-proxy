# Файлы модели для профиля `tiny2`

Модель rubert-tiny2 для маскировки не опубликована на Hugging Face. Чтобы собрать
образ Analyzer с ней, положите сюда шесть файлов из манифеста
`presidio/model_manifest.tiny2.json`:

```
config.json
model.safetensors
special_tokens_map.json
tokenizer.json
tokenizer_config.json
vocab.txt
```

Получите файлы финального checkpoint `mix-a-s1` у владельца артефакта и
положите их в эту папку. Расположение исходного архива и постоянное хранилище
весов должны быть согласованы до промышленной сборки.

Затем соберите образ с профилем:

```bash
NER_MODEL_PROFILE=tiny2 docker compose build presidio-analyzer
NER_MODEL_PROFILE=tiny2 docker compose up -d presidio-analyzer
```

При сборке каждый файл сверяется с размером и SHA-256 из манифеста; при
несовпадении сборка останавливается. При запуске сервис ещё раз проверяет файлы
и сверяет модель с профилем. Веса в git не добавляются (см. `.gitignore`).
