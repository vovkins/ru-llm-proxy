# Файлы модели для профиля `tiny2`

Модель опубликована на Hugging Face:
[Katerina-Bir/rubert-tiny2-secret-detection](https://huggingface.co/Katerina-Bir/rubert-tiny2-secret-detection).

Закреплённая ревизия Hugging Face:
`4137142e79d52b0b69370e0e3dceb2d6ad82591d`.

Чтобы собрать образ Analyzer, положите сюда шесть файлов из манифеста
`presidio/model_manifest.tiny2.json`:

```
config.json
model.safetensors
special_tokens_map.json
tokenizer.json
tokenizer_config.json
vocab.txt
```

Скачайте перечисленные файлы из указанной ревизии Hugging Face и положите
их в `presidio/model-artifacts/tiny2/`. Они соответствуют артефакту
`tiny2-mix-a-s1-ft-spacy-free-20261013-a-e2-b2`; идентичность проверяется манифестом.

Затем соберите и запустите стек с профилем
([развёртывание](../../../docs/deployment.md#tiny2)):

```bash
make build ANALYZER_PROFILE=tiny2
make up STACK=litellm-presidio ANALYZER_PROFILE=tiny2
```

При сборке каждый файл сверяется с размером и SHA-256 из манифеста; при
несовпадении сборка останавливается. При запуске сервис ещё раз проверяет файлы
и сверяет модель с профилем. Веса в git не добавляются (см. `.gitignore`).
