# Документация ru-llm-proxy

Этот раздел устроен как карта документации, а не как один длинный справочник. Основной язык документации — русский. Английские термины остаются там, где это официальное имя продукта, протокола, API, команды или параметра LiteLLM.

## Как читать

| Если нужно | Начните здесь |
| --- | --- |
| Быстро понять проект и запустить локально | [README.md](../README.md) |
| Понять поток запроса, защитные слои LiteLLM и границы данных | [architecture.md](architecture.md) |
| Настроить `.env`, Docker Compose и значения по умолчанию для запуска | [configuration.md](configuration.md) |
| Выполнить API-запросы и проверить режимы | [examples.md](examples.md) |
| Выдать пользовательские ключи и ограничить доступ | [admin-access.md](admin-access.md) |
| Подключить конкретный клиент | [clients/](clients/) |
| Настроить мониторинг, метрики и алерты | [monitoring.md](monitoring.md) |
| Подготовить подтверждения для проверки требований | [compliance.md](compliance.md) |
| Ограничить исходящие сетевые соединения в промышленной среде | [egress-controls.md](egress-controls.md) |
| Разобраться с закреплением маршрутов | [routing.md](routing.md) |
| Посмотреть историческое исследование выбора стека | [research.md](research.md) |

## Учебный путь

1. Прочитайте [главный README](../README.md), чтобы понять назначение прокси и текущие ограничения.
2. Выполните быстрый запуск из README.
3. Проверьте базовый запрос и запрос с персональными данными по [examples.md](examples.md).
4. Создайте пользовательский ключ LiteLLM по [admin-access.md](admin-access.md).
5. Подключите нужный клиент из [clients/](clients/).
6. Перед передачей в эксплуатацию пройдите [monitoring.md](monitoring.md), [compliance.md](compliance.md) и [egress-controls.md](egress-controls.md).

## Концепции

| Тема | Документ |
| --- | --- |
| Компоненты, поток запроса и обработчики до/после вызова модели | [architecture.md](architecture.md) |
| Маскирование персональных данных, режим блокировки, fail-open/fail-closed | [architecture.md](architecture.md), [configuration.md](configuration.md) |
| spaCy, DeepPavlov и русские распознаватели | [architecture.md](architecture.md) |
| Маршрутизация провайдеров и закрепление развёртывания за ключом | [routing.md](routing.md) |
| Границы исходящих соединений и сетевые ограничения | [egress-controls.md](egress-controls.md) |

## Практические инструкции

| Действие | Документ |
| --- | --- |
| Заполнить обязательные переменные окружения | [configuration.md](configuration.md) |
| Добавить дополнительного провайдера OpenAI/Anthropic | [examples.md](examples.md), [../examples/litellm-config.optional-providers.yaml](../examples/litellm-config.optional-providers.yaml) |
| Создать пользовательский ключ LiteLLM | [admin-access.md](admin-access.md), [examples.md](examples.md) |
| Проверить защитные слои через API | [examples.md](examples.md) |
| Настроить алерты | [monitoring.md](monitoring.md) |
| Обновить образ LiteLLM | [monitoring.md](monitoring.md) |

## Справочники

| Справочник | Что содержит |
| --- | --- |
| [configuration.md](configuration.md) | Все переменные окружения, значения по умолчанию, допустимые значения и влияние на запуск. |
| [examples.md](examples.md) | Curl-примеры для Chat Completions, Responses API, Anthropic Messages, Analyzer, защитных слоёв, метрик и маршрутизации. |
| [monitoring.md](monitoring.md) | Метрики, проверки состояния, журналы аудита, рекомендуемые алерты и эксплуатационный регламент. |
| [admin-access.md](admin-access.md) | Границы секретов, административный интерфейс, `LITELLM_MASTER_KEY`, пользовательские ключи, ротация и аварийный доступ. |

## Клиенты

| Клиент | Документ |
| --- | --- |
| ZCode | [clients/zcode.md](clients/zcode.md) |
| Codex CLI / Codex App | [clients/codex.md](clients/codex.md) |
| Claude Code / клиенты, совместимые с Anthropic Messages API | [clients/claude-code.md](clients/claude-code.md) |
| Kilo Code | [clients/kilo-code.md](clients/kilo-code.md) |
| OpenCode | [clients/opencode.md](clients/opencode.md) |
| JWT / OIDC-аутентификация на входе в прокси | [clients/jwt.md](clients/jwt.md) |

## Термины

| Термин | Как используем |
| --- | --- |
| Guardrail | Защитный обработчик LiteLLM, который проверяет и изменяет запросы/ответы. Термин оставляем английским, потому что это имя механизма LiteLLM. |
| Provider | Провайдер модели. В настройках LiteLLM может встречаться как часть имени параметра. В обычном тексте пишем «провайдер». |
| Deployment | Конкретное развёртывание модели внутри LiteLLM. В обычном тексте используем “развёртывание”; английское слово оставляем только при ссылке на имя поля или метрики LiteLLM. |
| Virtual key | Пользовательский ключ LiteLLM. Не равен `LITELLM_MASTER_KEY`. В обычном тексте пишем “пользовательский ключ” или “клиентский ключ”. |
| Быстрая проверка (`smoke`) | Проверка работоспособности. В командах Makefile сохраняем `smoke`, потому что это часть имени цели. |
| PII | Персональные и чувствительные данные, которые проект должен обнаружить, замаскировать или заблокировать. |

## Что намеренно вынесено в отдельные задачи

| Задача | Почему не здесь |
| --- | --- |
| Промышленное развёртывание в Kubernetes | Проектируется в #59, чтобы не смешивать документационный рефакторинг и инфраструктурный дизайн. |
| Маршрутизация чувствительных запросов во внутреннюю модель | Продуктовая функциональность #54. |
| Замер производительности и стратегия разбиения больших контекстов | Производительное исследование #55. |
| Оптимизация образа Analyzer для CPU/GPU | Исследование #62. |

## Правила поддержки документации

- README должен оставаться короткой входной страницей.
- Подробные настройки живут в [configuration.md](configuration.md), а не в README.
- Примеры API живут в [examples.md](examples.md).
- Операционные инструкции, метрики и алерты живут в [monitoring.md](monitoring.md).
- Исторические исследования не должны выглядеть как актуальные инструкции.
- Любая новая переменная окружения должна быть описана в [configuration.md](configuration.md) и покрыта статическими тестами.
