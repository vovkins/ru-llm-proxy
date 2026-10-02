# Tiny2 через Triton и ONNX Runtime

Дополнительный CPU FP32 backend для закреплённых весов Tiny2. По умолчанию
Analyzer использует Transformers/PyTorch и профиль `bert`. Triton включается
явно. Правила Presidio, маскирование, Redis-кэш и восстановление сохраняются.

## Запуск

Шесть проверенных файлов Tiny2 положить в `presidio/model-artifacts/tiny2/`
по [инструкции модели](../presidio/model-artifacts/tiny2/README.md). Они игнорируются
Git. `.env` заполняется обычным способом; новых ключей LLM-провайдера не требуется.

```bash
docker compose -f docker-compose.yml -f docker-compose.triton.yml build ner-triton presidio-analyzer
docker compose -f docker-compose.yml -f docker-compose.triton.yml up -d ner-triton presidio-analyzer
```

Дополнительный файл использует отдельные теги образов, профиль `tiny2`, backend
`triton`, CPU FP32 и пакет8 окон. ONNX экспортируется во время сборки из
проверенных локальных весов. Интернет во время работы модели не требуется.
Порты Triton не публикуются на хост; Analyzer обращается по внутренней сети.
GPU не требуется. По умолчанию один инстанс Triton и один процесс Analyzer.

```bash
curl --fail http://localhost:5001/api/v1/health
curl --fail http://localhost:5001/api/v1/analyze \
  -H 'Content-Type: application/json' \
  -d '{"text":"password=pine-dawn727; postgresql://svc-a:1592@db","language":"ru","score_threshold":0.35}'
```

Текст в примере фиктивный. В runtime и стартовом журнале backend — `triton`.
Ограничения параллельности/очереди Analyzer остаются исходными. Настройки
нагрузочного стенда 128/600 и admission8/32 сюда не перенесены.

## Контракт

- Исходные веса/токенизатор проверяются манифестом Tiny2.
- Экспорт FP32/opset17 проверяется сравнением logits PyTorch/ONNX Runtime.
- Triton при старте проверяет SHA-256 ONNX. Analyzer сверяет source-model,
  tokenizer и label-contract SHA, precision, CPU instances, tensor shapes/types.
- Версия модели указана явно. Изменившаяся идентичность графа отклоняется.
- Существуют те же окна384/64, BIO-декодер, confidence, O bias и постобработка.
  Padding до386 позиций обеспечивает совместимый батчинг. Bias не меняет score.
- Dynamic batching: максимум32, задержка до1мс, один intra/inter-op поток.
- При недоступности либо неправильном ответе Triton — безопасный отказ.
  Автоматического перехода на PyTorch нет. После service-wide ошибки нужен
  перезапуск Analyzer, как при отказе текущего обязательного NER backend.
- Backend включён в analysis signature; старый Redis-кэш не смешивается с новым.

Binary HTTP описан в [документации Triton](https://docs.nvidia.com/deeplearning/triton-inference-server/user-guide/docs/protocol/extension_binary_data.html).

## Настройки

| Переменная | Default | Назначение |
|---|---|---|
| `PRESIDIO_ANALYZER_NER_BACKEND` | `transformers` | `transformers` / `triton` |
| `PRESIDIO_ANALYZER_TRITON_URL` | `http://ner-triton:8000` | HTTP(S) URL без credentials/query |
| `PRESIDIO_ANALYZER_TRITON_MODEL_NAME` | `tiny2` | Имя модели |
| `PRESIDIO_ANALYZER_TRITON_MODEL_VERSION` | `1` | Явная положительная версия |
| `PRESIDIO_ANALYZER_TRITON_TIMEOUT_SECONDS` | `60` | Таймаут вызова, максимум600с |
| `TRITON_MODEL_INSTANCES` | `1` | CPU-инстансы Triton, 1–32 |

Количество процессов Analyzer задаёт существующая `PRESIDIO_ANALYZER_WORKERS`.
Это отдельный параметр. Внешний Triton можно подключить через обычный compose
с `NER_MODEL_PROFILE=tiny2`, backend `triton` и другим URL. Ему нужен тот же
проверенный identity/tensor контракт; 17 выходов сами по себе недостаточны.

## Откат

```bash
docker compose -f docker-compose.yml -f docker-compose.triton.yml stop presidio-analyzer ner-triton
docker compose -f docker-compose.yml up -d --no-deps presidio-analyzer
```

Если `.env` менялся для внешнего Triton, вернуть backend `transformers`.
Чтобы сохранить Tiny2 при откате на PyTorch, установить `NER_MODEL_PROFILE=tiny2`
и собрать обычный Analyzer с этим профилем. Исходная ветка
`feature/ner-tiny2-profile` также сохранена.

## Границы проверки

Подключение backend не означает достижения задержки около1с. Предыдущие замеры
относятся к Linux ARM64 VM; на целевом сервере нужны собственные измерения.
Очередь Analyzer и стоимость spaCy сохраняются. Параллельность8/32 вызывала OOM
в VM12GiB и не включена как default. Рабочий корпоративный сервер не менялся.
