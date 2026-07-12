# Документация ru-llm-proxy

Этот раздел устроен как карта документации, а не как один длинный справочник. Основной язык документации — русский. Английские термины остаются там, где это официальное имя продукта, протокола, API, команды или параметра LiteLLM.

## Как читать

| Если нужно | Начните здесь |
| --- | --- |
| Быстро понять проект и запустить локально | [README.md](../README.md) |
| Понять поток запроса, guardrails и границы данных | [architecture.md](architecture.md) |
| Настроить `.env`, Docker Compose и runtime defaults | [configuration.md](configuration.md) |
| Выполнить API-запросы и проверить режимы | [examples.md](examples.md) |
| Выдать пользовательские ключи и ограничить доступ | [admin-access.md](admin-access.md) |
| Подключить конкретный клиент | [clients/](clients/) |
| Настроить мониторинг, метрики и алерты | [monitoring.md](monitoring.md) |
| Подготовить evidence для проверки требований | [compliance.md](compliance.md) |
| Ограничить сетевые выходы в production | [egress-controls.md](egress-controls.md) |
| Разобраться со sticky routing | [routing.md](routing.md) |
| Посмотреть историческое исследование выбора стека | [research.md](research.md) |

## Учебный путь

1. Прочитайте [главный README](../README.md), чтобы понять назначение прокси и текущие ограничения.
2. Выполните быстрый запуск из README.
3. Проверьте базовый запрос и запрос с PII по [examples.md](examples.md).
4. Создайте пользовательский LiteLLM virtual key по [admin-access.md](admin-access.md).
5. Подключите нужный клиент из [clients/](clients/).
6. Перед передачей в эксплуатацию пройдите [monitoring.md](monitoring.md), [compliance.md](compliance.md) и [egress-controls.md](egress-controls.md).

## Концепции

| Тема | Документ |
| --- | --- |
| Компоненты, request flow, pre-call/post-call hooks | [architecture.md](architecture.md) |
| PII masking, block mode, fail-open/fail-closed | [architecture.md](architecture.md), [configuration.md](configuration.md) |
| spaCy, DeepPavlov и русские recognizers | [architecture.md](architecture.md) |
| Provider routing и sticky affinity | [routing.md](routing.md) |
| Egress boundaries и сетевые ограничения | [egress-controls.md](egress-controls.md) |

## Практические инструкции

| Действие | Документ |
| --- | --- |
| Заполнить обязательные переменные окружения | [configuration.md](configuration.md) |
| Добавить optional OpenAI/Anthropic provider | [examples.md](examples.md), [../examples/litellm-config.optional-providers.yaml](../examples/litellm-config.optional-providers.yaml) |
| Создать virtual key | [admin-access.md](admin-access.md), [examples.md](examples.md) |
| Проверить guardrails через API | [examples.md](examples.md) |
| Настроить алерты | [monitoring.md](monitoring.md) |
| Обновить LiteLLM image | [monitoring.md](monitoring.md) |

## Справочники

| Справочник | Что содержит |
| --- | --- |
| [configuration.md](configuration.md) | Все переменные окружения, значения по умолчанию, допустимые значения и влияние на runtime. |
| [examples.md](examples.md) | Curl-примеры для Chat Completions, Responses API, Anthropic Messages, Analyzer, guardrails, metrics и routing. |
| [monitoring.md](monitoring.md) | Метрики, health checks, audit logs, recommended alerts и runbook. |
| [admin-access.md](admin-access.md) | Границы секретов, Admin UI, `LITELLM_MASTER_KEY`, virtual keys, ротация и аварийный доступ. |

## Клиенты

| Клиент | Документ |
| --- | --- |
| ZCode | [clients/zcode.md](clients/zcode.md) |
| Codex CLI / Codex App | [clients/codex.md](clients/codex.md) |
| Claude Code / Anthropic-compatible clients | [clients/claude-code.md](clients/claude-code.md) |
| Kilo Code | [clients/kilo-code.md](clients/kilo-code.md) |
| OpenCode | [clients/opencode.md](clients/opencode.md) |
| JWT / OIDC proxy auth | [clients/jwt.md](clients/jwt.md) |

## Термины

| Термин | Как используем |
| --- | --- |
| Guardrail | Защитный обработчик LiteLLM, который проверяет и изменяет запросы/ответы. Термин оставляем английским, потому что это имя механизма LiteLLM. |
| Provider | Провайдер модели или LLM-провайдер. В настройках LiteLLM может встречаться как часть official parameter name. |
| Deployment | Конкретная upstream-настройка модели внутри LiteLLM. В обычном тексте используем “развертывание”, но для LiteLLM routing оставляем “deployment”. |
| Virtual key | Пользовательский ключ LiteLLM. Не равен `LITELLM_MASTER_KEY`. |
| Smoke-проверка | Быстрая проверка работоспособности. В командах Makefile сохраняем `smoke`, потому что это часть имени target. |
| PII | Персональные и чувствительные данные, которые проект должен обнаружить, замаскировать или заблокировать. |

## Что намеренно вынесено в отдельные задачи

| Задача | Почему не здесь |
| --- | --- |
| Kubernetes production deployment | Проектируется в #59, чтобы не смешивать документационный рефакторинг и инфраструктурный дизайн. |
| Sensitivity-based routing во внутреннюю LLM | Продуктовая функциональность #54. |
| Large-context benchmark и chunking | Производительное исследование #55. |
| CPU/GPU analyzer image optimization | Исследование #62. |

## Правила поддержки документации

- README должен оставаться короткой входной страницей.
- Подробные настройки живут в [configuration.md](configuration.md), а не в README.
- Примеры API живут в [examples.md](examples.md).
- Операционные инструкции, метрики и алерты живут в [monitoring.md](monitoring.md).
- Исторические исследования не должны выглядеть как актуальные инструкции.
- Любая новая переменная окружения должна быть описана в [configuration.md](configuration.md) и покрыта static tests.
