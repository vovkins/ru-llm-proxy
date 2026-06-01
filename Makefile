.PHONY: setup build up down restart logs test test-unit test-recognizers test-guardrail test-flow test-e2e guardrails-list guardrails-smoke metrics monitor-smoke update-litellm health clean help

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
	@echo "  make guardrails-list — список guardrails, зарегистрированных в LiteLLM"
	@echo "  make guardrails-smoke — live smoke с явным guardrails parameter"
	@echo "  make metrics  — показать начало LiteLLM /metrics"
	@echo "  make monitor-smoke — проверить health, guardrails list и /metrics"
	@echo "  make update-litellm — подтянуть новый LiteLLM image и пересоздать proxy"
	@echo "  make health   — проверить статус всех сервисов"
	@echo "  make clean    — удалить volumes и образы"

# === Setup ===
setup:
	bash scripts/setup_env.sh

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

# === Guardrails diagnostics ===
guardrails-list:
	@echo "🛡️  LiteLLM registered guardrails"
	@if [ ! -f .env ]; then echo "❌ .env not found"; exit 1; fi
	@eval "$$(grep LITELLM_MASTER_KEY .env | sed 's/^/export /')" && \
		response=$$(curl -sS -H "Authorization: Bearer $$LITELLM_MASTER_KEY" http://localhost:4000/guardrails/list); \
		if command -v jq >/dev/null 2>&1; then printf "%s\n" "$$response" | jq .; else printf "%s\n" "$$response"; fi

guardrails-smoke:
	@echo "🛡️  LiteLLM guardrails live smoke"
	@if [ ! -f .env ]; then echo "❌ .env not found"; exit 1; fi
	@eval "$$(grep LITELLM_MASTER_KEY .env | sed 's/^/export /')" && \
		headers=$$(mktemp) && body=$$(mktemp) && \
		curl -sS -D "$$headers" -o "$$body" http://localhost:4000/chat/completions \
			-H "Authorization: Bearer $$LITELLM_MASTER_KEY" \
			-H "Content-Type: application/json" \
			-d '{"model":"glm-5.1","guardrails":["ru-pii-mask-pre","ru-pii-mask-post"],"messages":[{"role":"user","content":"Проверь текст: Иван Иванов, телефон +79031234567"}],"max_tokens":40}' >/dev/null && \
		{ \
			echo "Applied guardrails header:"; \
			if ! grep -i "^x-litellm-applied-guardrails:" "$$headers"; then echo "Header not found"; fi; \
			echo ""; \
			if command -v jq >/dev/null 2>&1; then jq . "$$body"; else cat "$$body"; fi; \
			rm -f "$$headers" "$$body"; \
		}

# === Monitoring diagnostics ===
metrics:
	@echo "📈 LiteLLM /metrics"
	@tmp=$$(mktemp) && \
		curl -sf http://localhost:4000/metrics > "$$tmp" && \
		sed -n '1,120p' "$$tmp"; \
		status=$$?; rm -f "$$tmp"; exit $$status

monitor-smoke:
	@echo "📈 Monitoring smoke check"
	@$(MAKE) health
	@$(MAKE) guardrails-list
	@tmp=$$(mktemp) && \
		curl -sf http://localhost:4000/metrics > "$$tmp" && \
		if grep -q "litellm_" "$$tmp"; then echo "✅ LiteLLM metrics exposed"; else echo "❌ LiteLLM metrics not found"; rm -f "$$tmp"; exit 1; fi; \
		if grep -q "ru_pii_guardrail_" "$$tmp"; then echo "✅ PII guardrail metrics exposed"; else echo "⚠️  PII guardrail metrics not emitted yet; run a PII request and retry"; fi; \
		rm -f "$$tmp"

# === LiteLLM update ===
update-litellm:
	@echo "⬇️  Pulling latest LiteLLM image configured in docker-compose.yml"
	docker compose pull litellm
	docker compose up -d --force-recreate --no-deps litellm
	@echo "✅ LiteLLM image updated and proxy container recreated"
