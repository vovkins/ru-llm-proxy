# AI Proxy: Санитайзер PII + Агрегатор LLM

**Тип:** Исследование / Идея продукта
**Дата:** 2026-05-28
**Статус:** Исследование рынка завершено

---

## Суть идеи

Прокси-сервер между клиентами и LLM-провайдерами:
1. **Санитайзер PII** — убирает чувствительные данные (имена, телефоны, адреса, паспорта) из запросов перед отправкой к внешним моделям
2. **Агрегатор** — единая точка входа к нескольким LLM-провайдерам (OpenAI, Anthropic, Google, etc.)
3. **Критично:** Хорошая работа с русским языком

---

## Существующие решения на рынке

### 1. LiteLLM Proxy (open-source, MIT)
- **Что:** Самый популярный open-source LLM-шлюз (100k+ ⭐)
- **Агрегация:** OpenAI, Anthropic, Google, Azure, Bedrock, 100+ провайдеров через единый OpenAI-совместимый API
- **PII:** Интеграция с Microsoft Presidio для маскирования PII/PHI. Также поддерживает Pillar Security, Lasso Security, PromptGuard (self-hosted)
- **Плюсы:** Зрелый, активное развитие, self-hosted, кастомные guardrails, балансировка нагрузки, кэширование, трекинг расходов
- **Минусы:** PII — не из коробки, нужна интеграция с Presidio. Русский язык — зависит от качества NER-модели
- **Вердикт:** Лучшая основа для агрегатора, PII нужно докручивать

### 2. Portkey AI Gateway (open-source, Apache 2.0)
- **Что:** AI-шлюз с фокусом на production-безопасность
- **Агрегация:** 1600+ моделей через единый API
- **PII:** 50+ встроенных guardrails, PII redaction (regex-based), jailbreak detection, audit trails
- **Плюсы:** Open-source ядро, мощный UI, observability, guardrails из коробки
- **Минусы:** PII redaction — regex-based (ограничен для русского), полноценные guardrails — на managed-платформе ($49/мес)
- **Вердикт:** Сильный конкурент LiteLLM, но PII для русского тоже нужно дорабатывать

### 3. Microsoft Presidio (open-source, MIT)
- **Что:** Фреймворк для обнаружения и анонимизации PII
- **Поддержка языков:** Многоязычный через spaCy/Stanza модели. Есть `ru_core_web_sm/lg` для русского, но качество NER для русского значительно хуже английского
- **Плюсы:** Open-source, кастомизируемый, интегрируется с LiteLLM, готовые Docker-образы
- **Минусы:** Русский NER — слабое место (spaCy ru_core_web плохо отличает PER/ORG/LOC). Контекстные слова нужно писать вручную для русского
- **Вердикт:** Лучший open-source фундамент для PII-детекции, но для русского нужна доработка

### 4. Protecto.ai (коммерческий)
- **Что:** AI-native privacy platform для LLM
- **PII:** DeepSight — transformer-based PII detection, кросс-язычный, понимает сленг и опечатки
- **Плюсы:** Лучшая точность PII-детекции, токенизация с сохранением семантики (LLM понимает замаскированный текст), аудит-логи, RBAC
- **Минусы:** Коммерческий (нет self-hosted open-source), ценообразование enterprise
- **Вердикт:** Самый мощный для PII, но платный

### 5. Kong AI Gateway (enterprise + open-source)
- **Что:** API-шлюз Kong с плагином AI PII Sanitizer
- **PII:** Плагин для автоматической маскировки в request/response
- **Плюсы:** Enterprise-grade, масштабируемость, интеграция с существующей Kong-инфраструктурой
- **Минусы:** Enterprise-фокус, сложно для малого/среднего использования, русский язык не гарантирован

### 6. AI DLP Proxy (open-source)
- **Что:** Лёгкий DLP-прокси для LLM-эндпоинтов
- **PII:** Real-time redaction для OpenAI/Anthropic
- **Плюсы:** Простой, self-hosted, минималистичный
- **Минусы:** Базовый функционал, неизвестно про русский язык

### 7. Gravitee (enterprise)
- **Что:** API-платформа с PII Filtering Policy
- **Плюсы:** Gateway-level контроль, GDPR/CCPA compliance
- **Минусы:** Enterprise, сложный, коммерческий

---

## Сравнительный анализ: Ferro Labs AI Gateway vs LiteLLM

*Добавлено: 2026-05-28*

### Общая сравнительная таблица

| Критерий | Ferro Labs AI Gateway | LiteLLM Proxy |
|----------|----------------------|--------------|
| **Язык** | Go | Python |
| **Лицензия** | Apache 2.0 | Apache 2.0 (ранее MIT) |
| **Провайдеры** | 30 (+2500 моделей) | 100+ |
| **Роутинг** | 8 стратегий (single, fallback, weighted, least-latency, cost-optimized, content-based, A/B test, conditional) | Fallback, load balancing, cost-based routing |
| **Архитектура** | Одиночный Go-бинарник (20 MB Docker, 32 MB RAM) | Python-сервис (pip, virtualenv, heavier runtime) |
| **Производительность** | 13,925 RPS при 1000 VU, p99 overhead <1ms | Ограничен Python GIL, ниже throughput |
| **Self-hosted** | ✅ Да | ✅ Да |
| **Managed cloud** | В разработке (waitlist) | ✅ Есть |
| **PII/Guardrails** | ⚠️ Enterprise-плагины (только Managed) | ✅ Presidio, Pillar, Lasso, кастомные — всё self-hosted |
| **MCP support** | ✅ Встроенный MCP tool loop | ❌ Нет |
| **Python-экосистема** | ❌ Нет | ✅ LangChain, LlamaIndex |
| **Кастомизация guardrails** | 6 OSS плагинов (word filter, rate limit, budget, cache, logger, max-token) | Гибкие кастомные guardrails через Python |
| **Зрелость** | Молодой (март 2026 v1.0) | Зрелый, большое community |
| **Русский язык** | Нет информации | Зависит от Presidio + кастомных recognizers |

### Детальный разбор

#### Ferro Labs AI Gateway — сильные стороны
1. **Производительность:** Go-бинарник без зависимостей, 13,925 RPS vs LiteLLM ограничен Python
2. **Простота деплоя:** `ferrogw init` → `ferrogw` — один бинарник, один конфиг-файл
3. **8 стратегий роутинга:** content-based routing и A/B testing из коробки
4. **Встроенный MCP loop:** прокси сам обрабатывает tool-calling без изменений на клиенте
5. **Минимальное потребление ресурсов:** 32-135 MB RAM в зависимости от нагрузки

#### Ferro Labs AI Gateway — слабые стороны (для нашей задачи)
1. **❌ PII redaction — Enterprise-плагин:** Доступен только в Ferro Labs Managed (SaaS, пока waitlist). В OSS-версении PII-функционала НЕТ
2. **Молодой проект:** Март 2026 (v1.0), меньше community, меньше battle-tested
3. **Go-экосистема:** Сложнее кастомизировать под русскую NER, чем Python
4. **30 провайдеров vs 100+:** Покрывает основных, но меньше экзотических
5. **Нет Python-интеграции:** Если стек Python — Ferro будет чужеродным

#### LiteLLM — сильные стороны
1. **✅ PII полностью self-hosted:** Presidio интеграция + кастомные guardrails через Python
2. **Зрелость:** Огромное community, активное развитие, battle-tested в production
3. **Python-экосистема:** Легко написать кастомные русские recognizers, интегрировать ruBERT/DeepPavlov
4. **100+ провайдеров:** Максимальный охват
5. **Гибкость guardrails:** Custom guardrail API позволяет писать любую логику на Python

#### LiteLLM — слабые стороны
1. **Производительность:** Python — ограничен throughput, выше overhead
2. **Сложнее деплой:** pip, virtualenv, зависимости, управление Python-процессом
3. **Меньше стратегий роутинга:** Нет A/B test, content-based из коробки
4. **Нет MCP:** Нет встроенной обработки tool-calling

### Вердикт для нашей задачи (PII-санитайзер + русский язык + агрегация)

**LiteLLM — однозначный победитель** по следующим причинам:

1. **PII — ключевая функция** — у Ferro она закрыта за enterprise-планом, у LiteLLM — полностью open-source через Presidio
2. **Русский язык** — Python-экосистема позволяет легко подключить ruBERT/DeepPavlov для NER; в Go это значительно сложнее
3. **Кастомизация** — кастомные guardrails на Python дают полную свободу для российских форматов данных
4. **Зрелость** — LiteLLM battle-tested, Ferro ещё молод

**Ferro Labs стоит рассмотреть**, если:
- Нужен чистый high-performance роутер без PII (например, как frontend перед LiteLLM+Presidio)
- Go-стек предпочтительнее Python
- В будущем Ferro откроет PII-плагины в OSS

---

## Анализ проблемы русского языка

**Главный вызов:** Качество NER для русского языка значительно уступает английскому:
- spaCy `ru_core_web` — плохо различает PER/ORG/LOC
- Для качественной детекции нужен подход на основе transformers (DeepPavlov RubERT, ruBERT) или LLM-based детекция

**Что работает для русского:**
- Regex-паттерны: телефоны (+7..., 8...), email, номера карт, ИНН, СНИЛС, паспорта (серии/номера)
- LLM-based подход: использовать небольшую модель (GPT-4o-mini, Claude Haiku) для детекции PII в промпте — дорого, но точно
- Transformer-based NER: ruBERT/DeepPavlov — лучше spaCy для русского

---

## Варианты реализации

### Вариант А: Собрать из open-source компонентов ⭐ Рекомендуемый
- **Основа:** LiteLLM Proxy (агрегация + роутинг)
- **PII-слой:** Microsoft Presidio + кастомные русские recognizers
- **Доработка:** Кастомные regex для российских форматов (телефоны, ИНН, СНИЛС, паспорта, адреса) + ruBERT/DeepPavlov NER для русского
- **Срок:** 2-4 недели MVP
- **Плюсы:** Полный контроль, self-hosted, бесплатно, кастомизация под русский

### Вариант Б: Portkey Gateway + кастомный guardrail
- **Основа:** Portkey (open-source gateway)
- **PII-слой:** Кастомный guardrail с русской NER-моделью
- **Срок:** 2-3 недели MVP
- **Плюсы:** Хороший UI/observability из коробки
- **Минусы:** Меньше гибкости в guardrails на self-hosted версии

### Вариант В: Коммерческое решение (Protecto.ai)
- **Готовый продукт** с лучшей PII-детекцией
- **Плюсы:** Быстрый старт, высокая точность
- **Минусы:** Зависимость от vendor, стоимость, неясно насколько хорошо работает русский

### Вариант Г: Сделать с нуля
- Написать свой прокси на Python/FastAPI или Node.js
- Интегрировать PII-детекцию и роутинг к провайдерам
- **Срок:** 4-8 недель MVP
- **Плюсы:** Полный контроль архитектуры
- **Минусы:** Дольше, нужно писать то, что уже есть в LiteLLM

---

## Требования к серверу (Вариант А: LiteLLM + Presidio + ruBERT NER)

**Важно:** Прокси не генерирует текст — только пропускает через себя запросы к внешним API. Ресурсы нужны в основном на NER-модель, а не на inference.

### Потребление ресурсов по компонентам

| Компонент | RAM | CPU | Диск |
|-----------|-----|-----|------|
| LiteLLM Proxy | 200-500 MB | минимально | ~500 MB |
| Presidio Analyzer + Anonymizer | 300-500 MB | минимально | ~300 MB |
| spaCy `ru_core_web_lg` | +500 MB (на загрузку модели) | средне | ~500 MB |
| ruBERT / DeepPavlov NER | +1-2 GB (на модель) | средне-высоко | 1-2 GB |

### Три уровня конфигурации

| Конфигурация | RAM | CPU | Диск | PII-качество для русского |
|---|---|---|---|---|
| **Минимальный** (regex-only recognizers) | 1 GB | 1 vCPU | 5 GB | ⚠️ Телефоны, email, ИНН, СНИЛС — ок. Имена/адреса — пропускает |
| **Средний** (regex + spaCy `ru_core_web_lg`) | 2 GB | 2 vCPU | 10 GB | ⚠️ Имена/адреса — средне, много false positives/negatives |
| **Полный** (regex + ruBERT/DeepPavlov NER) | 4 GB | 2-4 vCPU | 15-20 GB | ✅ Нормальное качество для имён/адресов/организаций |

### Рекомендация

**2 vCPU, 4 GB RAM, 20 GB SSD** — комфортно для полного варианта с запасом.

**Стоимость облака:** $5-10/мес (Hetzner, DigitalOcean, Vultr).

**Существующие серверы:** VPS (45.12.255.117, 8 GB RAM) или домашний сервер (8 GB) — оба потянут полный вариант.

---

## Варианты развёртывания (деплой Варианта А)

### Компоненты для деплоя

1. **LiteLLM Proxy** — Python-сервис, единый API-эндпоинт
2. **Presidio Analyzer** — REST-сервис (или in-process Python) для детекции PII
3. **Presidio Anonymizer** — REST-сервис (или in-process) для маскирования
4. **NER-модель** — ruBERT/DeepPavlov (файлы ~1-2 GB)
5. **PostgreSQL** — база данных для LiteLLM (ключи, логи, расходы) — опционально, можно SQLite для MVP
6. **Redis** — для кэширования и сессий — опционально для MVP

### Вариант 1: Docker Compose ⭐ Рекомендуемый

**Архитектура:** Каждый компонент в своём контейнере, оркестрация через `docker-compose.yml`.

```yaml
# docker-compose.yml (упрощённый MVP)
services:
  litellm:
    image: ghcr.io/berriai/litellm:main-latest
    ports:
      - "4000:4000"
    volumes:
      - ./litellm-config.yaml:/app/config.yaml
    environment:
      - OPENAI_API_KEY=${OPENAI_API_KEY}
      - ANTHROPIC_API_KEY=${ANTHROPIC_API_KEY}
      - MASTER_KEY=${MASTER_KEY}
    depends_on:
      - presidio-analyzer

  presidio-analyzer:
    build: ./presidio-custom  # свой образ с русскими recognizers + ruBERT
    ports:
      - "5001:5001"
    volumes:
      - ./models:/models  # NER-модели

  presidio-anonymizer:
    image: mcr.microsoft.com/presidio-anonymizer:latest
    ports:
      - "5002:5002"

  db:
    image: postgres:16-alpine
    environment:
      POSTGRES_DB: litellm
      POSTGRES_PASSWORD: ${DB_PASSWORD}
    volumes:
      - pgdata:/var/lib/postgresql/data

volumes:
  pgdata:
```

**Плюсы:**
- Изоляция компонентов — каждый в своём контейнере
- Воспроизводимость — один `docker-compose up` поднимает всё
- Легко масштабировать — увеличить replicas для LiteLLM
- Легко обновлять — `docker pull` + restart
- Стандартный подход для production
- Проксирование через Nginx/Caddy для TLS

**Минусы:**
- Нужен Docker на сервере
- Чуть больше потребление памяти (overhead контейнеров ~50-100 MB каждый)
- Управление volumes для персистентности

**Настройка:**
1. `.env` файл с API-ключами провайдеров и MASTER_KEY
2. `litellm-config.yaml` — список провайдеров, guardrails, роутинг
3. `presidio-custom/Dockerfile` — кастомный образ с русскими recognizers и NER-моделью
4. Nginx/Caddy как reverse proxy для TLS и rate limiting

### Вариант 2: Всё на хосте (bare metal)

**Архитектура:** Все компоненты запускаются как systemd-сервисы или через `supervisord`.

```
/systemd/
  litellm.service       → litellm --config config.yaml --port 4000
  presidio-analyzer.service → python analyzer_server.py --port 5001
  presidio-anonymizer.service → python anonymizer_server.py --port 5002
```

**Плюсы:**
- Минимальный overhead — нет Docker-слоя
- Меньше потребление памяти (~200-300 MB экономии)
- Прямой доступ к GPU (если появится)
- Проще отладка — логи прямо в journalctl

**Минусы:**
- Нужно управлять зависимостями Python (virtualenv, версии)
- Конфликты версий между LiteLLM и Presidio
- Сложнее обновлять — ручное управление пакетами
- Сложнее мигрировать на другой сервер
- Нет изоляции — ошибка в одном компоненте может повлиять на другие

**Настройка:**
1. Python 3.11+, venv для каждого компонента
2. systemd unit-файлы для автозапуска
3. Nginx как reverse proxy
4. Общая группа пользователей и права доступа

### Вариант 3: Гибрид (Docker для LiteLLM, Python для Presidio)

**Архитектура:** LiteLLM в Docker (официальный образ), Presidio in-process как Python-библиотека внутри LiteLLM через custom guardrail.

**Плюсы:**
- Меньше контейнеров (2 вместо 4+)
- Presidio работает in-process — нет сетевого overhead
- LiteLLM обновляется через Docker

**Минусы:**
- Нужно писать кастомный Dockerfile для LiteLLM с Presidio внутри
- NER-модель загружается в память LiteLLM-процесса (+1-2 GB)
- Сложнее масштабировать независимо

### Сравнение вариантов деплоя

| Критерий | Docker Compose | Bare metal | Гибрид |
|----------|---------------|------------|--------|
| Сложность настройки | средняя | высокая | средняя |
| Изоляция компонентов | ✅ полная | ❌ нет | ⚠️ частичная |
| Потребление RAM | +200-400 MB overhead | минимальное | среднее |
| Обновление | просто (docker pull) | сложно (pip) | средне |
| Воспроизводимость | ✅ высокая | ❌ низкая | ⚠️ средняя |
| Масштабирование | ✅ replicas | ручное | ограничено |
| Мониторинг | Docker logs, stats | journalctl | смешанный |
| Резервное копирование | volumes | файлы на хосте | volumes + файлы |

### Рекомендация по деплою

**Docker Compose** — лучший вариант для MVP и production:
1. Один `docker-compose.yml` описывает всю инфраструктуру
2. `.env` для секретов — не коммитится в git
3. Кастомный Dockerfile для Presidio с русскими recognizers
4. Nginx/Caddy как reverse proxy с TLS (Let's Encrypt)
5. GitHub Actions для CI/CD — при обновлении recognizers автоматический rebuild

**Процесс настройки:**
1. Клонировать репозиторий проекта с `docker-compose.yml` и конфигами
2. Создать `.env` с API-ключами провайдеров
3. `docker-compose up -d` — запустить все сервисы
4. Настроить Nginx с TLS как reverse proxy
5. Проверить через `curl` что API отвечает
6. Настроить мониторинг (Prometheus + Grafana или простой healthcheck)

---

## Рекомендация

**Вариант А (LiteLLM + Presidio + русские recognizers)** — оптимальный баланс:
1. LiteLLM даёт готовую агрегацию 100+ провайдеров из коробки
2. Presidio — проверенный PII-фреймворк, интеграция с LiteLLM уже есть
3. Основная работа — написать качественные русские recognizers (regex для российских форматов + ruBERT NER)
4. Self-hosted, бесплатно, полный контроль

**Следующие шаги:**
1. Протестировать Presidio с `ru_core_web` на русских текстах — оценить baseline
2. Протестировать ruBERT/DeepPavlov NER на тех же текстах
3. Написать кастомные regex-recognizers для российских форматов данных
4. Собрать MVP: LiteLLM + Presidio с русской конфигурацией
