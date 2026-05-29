.PHONY: setup build up down restart logs test test-unit test-e2e health clean help

SED_INPLACE = sed -i.bak -e

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
	@echo "  make test     — запустить тесты recognizers"
	@echo "  make test-unit — запустить все unit-тесты"
	@echo "  make test-e2e — end-to-end тест (нужны запущенные сервисы)"
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
test:
	@echo "🧪 Запуск тестов recognizers..."
	docker compose run --rm --no-deps presidio-analyzer \
		python -m pytest /app/tests/ -v 2>/dev/null || \
		(echo "⚠️  Тесты через Docker недоступны. Запускаю локально..." && \
		 pip install -q pytest presidio-analyzer presidio-anonymizer spacy && \
		 python -m spacy download ru_core_web_sm -q && \
		 cd presidio && python -m pytest tests/ -v)

# === Health check ===
health:
	@echo "=== Статус сервисов ==="
	@echo ""
	@echo -n "LiteLLM Proxy:    "; curl -sf http://localhost:4000/health > /dev/null 2>&1 && echo "✅ OK" || echo "❌ DOWN"
	@echo -n "Presidio Analyzer: "; curl -sf http://localhost:5001/api/v1/health > /dev/null 2>&1 && echo "✅ OK" || echo "❌ DOWN"
	@echo -n "Presidio Anonymizer: "; curl -sf http://localhost:5002/api/v1/health > /dev/null 2>&1 && echo "✅ OK" || echo "❌ DOWN"
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
	@echo ""
	@echo "📋 Recognizer tests:"
	docker compose run --rm --no-deps presidio-analyzer \
		python -m pytest /app/tests/ -v 2>/dev/null || \
		(echo "⚠️  Тесты через Docker недоступны. Запускаю локально..." && \
		 pip install -q pytest presidio-analyzer presidio-anonymizer spacy && \
		 python -m spacy download ru_core_web_sm -q && \
		 cd presidio && python -m pytest tests/ -v)

# === End-to-end tests (requires running services) ===
test-e2e:
	@echo "🧪 End-to-end тесты (требуются запущенные сервисы)"
	@if [ ! -f .env ]; then echo "❌ .env not found"; exit 1; fi
	@eval "$$(grep LITELLM_MASTER_KEY .env | sed 's/^/export /')" && \
		bash tests/e2e/test_e2e.sh
