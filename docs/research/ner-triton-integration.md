# Проверка production-подключения Tiny2 к Triton

2 октября 2026. Ветка `codex/ner-tiny2-triton-onnx`, основана на
`feature/ner-tiny2-profile`. Исходный профиль Transformers/PyTorch сохранён.
Запуск и откат: [ner-triton.md](../ner-triton.md).

## Проверено

| Проверка | Результат |
|---|---|
| Analyzer/NER, правила, guardrail и статические регрессии | 759 passed |
| Новый экспорт проверенных локальных весов | ONNX FP32 logits совпали с PyTorch, atol/rtol 1e-4 |
| Полный Analyzer, новый код + реальный Triton, 6312 golden-строк | 0 изменённых наборов интервалов относительно закреплённого Tiny2/Transformers baseline |
| Слияние базового и нового Compose | tiny2/triton/CPU, правильная зависимость от healthy Triton; внешних port bindings Triton нет |
| Docker target `ner-triton` | Собран и запущен; ONNX проверяется SHA-256 при старте |
| Docker target `analyzer` с Tiny2 | Собран и запущен с реальным Triton |
| `password=pine-dawn727` через API | Значение закрыто целиком: PASSWORD, 9–21 |
| `postgresql://svc-a:1592@db` через API | URI закрыт целиком: DB_URL, 0–26, включая логин и пароль |
| Остановка только тестового Triton | Первый health503; analyze503; перехода на локальную модель нет |

Последняя проверка выявила и исправила устаревший первый health-ответ:
в режиме Triton health теперь проверяет удалённую готовность до выдачи
здоровой analysis signature, а блокирующий запрос вынесен из event loop.
Для этого добавлен регрессионный тест. PyTorch-health сохраняет прежнюю логику.

## Условия

Локальная Linux ARM64 Ubuntu VM на Mac; контейнеры — Python3.11 CPU Analyzer
и Triton26.08/ONNX Runtime. Веса Tiny2 SHA-256:
`53cde9f0e47f95478411457e1d74fdc16070517d54a2b45a27d74e49ea75cb67`.
Golden использован как проверка регрессии, не как новый закрытый holdout.
Он частично размечен по классам; сравнивались полные выходы Analyzer.

В runtime остаются Transformers для локального токенизатора и PyTorch для
существующего декодера вероятностей. Forward модели выполняется удалённо
в ONNX Runtime/Triton; веса модели в процессе Analyzer не загружаются.
Модель/tokenizer identities проверяются по манифесту и конфигурации сервера;
это контракт доверенного внутреннего deployment, не удалённая аттестация сервера.

Изменение не заявляет production-SLA около1с и не развёртывалось на рабочем
сервере. Здесь проверено подключение, сохранение результатов и отказ при
недоступной модели. Очередь Analyzer, spaCy и остальные policy hooks сохранены.
Веса и ONNX не добавляются в Git. Исходные журналы проверок остаются локальными.
