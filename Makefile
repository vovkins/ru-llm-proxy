.PHONY: setup build up down restart logs test test-unit test-recognizers test-guardrail test-flow test-e2e health clean help

SED_INPLACE = sed -i.bak -e
PYTEST = python -m pytest -p no:cacheprovider -v
PYTEST_DOCKER_FLAGS = --rm --no-deps --build \
	-e PYTHONPATH=/workspace:/workspace/presidio \
	-e PYTHONDONTWRITEBYTECODE=1 \
	-v .:/workspace:ro \
	-w /workspace

# Default target
help:
	@echo "ru-llm-proxy — команды:"
	@echo ""
	@echo "  make setup    — первичная настройка (.env, генерация ключей)"
	@echo "  make build    — собрать Docker-образы"
	@echo "  make up       — запустить все сервисы"
	@echo "  make down     — остановить все сервисы"
	@echo "  make restart  — рестарт LiteLLM (применить новый конфиг)"
	@echo "  make logs     — логи всех сервисов"
	@echo "  make test     — запустить весь локальный test suite"
	@echo "  make test-unit — unit-тесты recognizers/NER, guardrail и flow"
	@echo "  make test-recognizers — unit-тесты recognizers и NER helpers"
	@echo "  make test-guardrail — unit-тесты LiteLLM guardrail"
	@echo "  make test-flow — deterministic guardrail-flow без внешнего LLM"
	@echo "  make test-e2e — live smoke test (нужны сервисы и LLM provider key)"
	@echo "  make health   — проверить статус всех сервисов"
	@echo "  make clean    — удалить volumes и образы"

# === Setup ===
setup:
	@if [ ! -f .env ]; then \
		echo "Создание .env из шаблона..."; \
		cp .env.example .env; \
		echo ""; \
		echo "Генерация ключей..."; \
		MASTER_KEY=$$(openssl rand -hex 32); \
		SALT_KEY=$$(openssl rand -hex 32); \
		DB_PASSWORD=$$(openssl rand -hex 16); \
		$(SED_INPLACE) "s/^LITELLM_MASTER_KEY=.*/LITELLM_MASTER_KEY=sk-$${MASTER_KEY}/" .env; \
		$(SED_INPLACE) "s/^LITELLM_SALT_KEY=.*/LITELLM_SALT_KEY=$${SALT_KEY}/" .env; \
		$(SED_INPLACE) "s/^POSTGRES_PASSWORD=.*/POSTGRES_PASSWORD=$${DB_PASSWORD}/" .env; \
		$(SED_INPLACE) "s|postgresql://litellm:.*@db|postgresql://litellm:$${DB_PASSWORD}@db|" .env; \
		rm -f .env.bak; \
		echo ""; \
		echo "✅ .env создан с автосгенерированными ключами"; \
		echo "⚠️  Заполните API-ключ основного провайдера в .env:"; \
		echo "   ZAI_API_KEY=***"; \
		echo ""; \
		echo "Опционально можно заполнить ключи других провайдеров:"; \
		echo "   OPENAI_API_KEY=***"; \
		echo "   ANTHROPIC_API_KEY=***"; \
		echo "   GOOGLE_API_KEY=***"; \
		echo ""; \
	else \
		echo "✅ .env уже существует, пропускаем"; \
	fi

# === Build ===
build:
	docker compose build --no-cache

# === Up ===
up:
	docker compose up -d
	@echo ""
	@echo "⏳ Ожидание запуска сервисов..."
	@sleep 5
	@$(MAKE) health

# === Down ===
down:
	docker compose down

# === Restart (apply new config without rebuild) ===
restart:
	docker compose restart litellm
	@echo "✅ LiteLLM перезапущен с новым конфигом"

# === Logs ===
logs:
	docker compose logs -f --tail=50

# === Test ===
test: test-unit

test-recognizers:
	@echo "🧪 Recognizer + NER unit tests"
	docker compose run $(PYTEST_DOCKER_FLAGS) presidio-analyzer \
		$(PYTEST) presidio/tests

test-guardrail:
	@echo "🧪 LiteLLM guardrail unit tests"
	docker compose run $(PYTEST_DOCKER_FLAGS) guardrail-tests \
		$(PYTEST) litellm_guardrails/tests

test-flow:
	@echo "🧪 Deterministic guardrail-flow test"
	docker compose run $(PYTEST_DOCKER_FLAGS) guardrail-tests \
		$(PYTEST) tests/e2e/test_guardrail_flow.py

# === Health check ===
health:
	@echo "=== Статус сервисов ==="
	@echo ""
	@echo -n "LiteLLM Proxy:    "; curl -sf http://localhost:4000/health/liveliness > /dev/null 2>&1 && echo "✅ OK" || echo "❌ DOWN"
	@echo -n "Presidio Analyzer: "; curl -sf http://localhost:5001/api/v1/health > /dev/null 2>&1 && echo "✅ OK" || echo "❌ DOWN"
	@echo -n "PostgreSQL:       "; docker compose exec -T db pg_isready -U litellm > /dev/null 2>&1 && echo "✅ OK" || echo "❌ DOWN"
	@echo -n "Redis:            "; docker compose exec -T redis redis-cli ping > /dev/null 2>&1 && echo "✅ OK" || echo "❌ DOWN"
	@echo ""

# === Clean ===
clean:
	@echo "⚠️  Это удалит все данные (БД, Redis, Docker-образы)"
	@read -p "Продолжить? [y/N] " confirm && [ "$$confirm" = "y" ] || exit 1
	docker compose down -v --rmi local
	@echo "✅ Очищено"

# === Unit tests (all) ===
test-unit:
	@echo "🧪 Запуск всех unit-тестов..."
	@$(MAKE) test-recognizers
	@$(MAKE) test-guardrail
	@$(MAKE) test-flow
	@echo "✅ Unit suite completed"

# === Live smoke test (requires running services) ===
test-e2e:
	@echo "🧪 Live smoke test (требуются запущенные сервисы и LLM provider key)"
	@if [ ! -f .env ]; then echo "❌ .env not found"; exit 1; fi
	@eval "$$(grep LITELLM_MASTER_KEY .env | sed 's/^/export /')" && \
		bash tests/e2e/test_e2e.sh
