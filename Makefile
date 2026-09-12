.PHONY: setup build up down restart logs test test-unit test-static test-recognizers test-recognizer-api test-ner-evaluation test-hf-model test-hf-model-run test-ner-proxy test-ner-integration ner-evaluate test-guardrail test-flow test-routing-diagnostics test-e2e test-pre-egress-proxy test-final-leak-proxy test-egress-security test-observability-gates virtual-key-create client-auth-smoke guardrails-list guardrails-smoke routing-smoke metrics monitor-smoke update-litellm health clean help require-stack

# Docker Desktop stores its credential helper outside the default non-interactive
# PATH on macOS. Export it once for every recipe and recursive make invocation.
DOCKER_DESKTOP_BIN := /Applications/Docker.app/Contents/Resources/bin
ifneq ($(wildcard $(DOCKER_DESKTOP_BIN)/docker-credential-desktop),)
export PATH := $(DOCKER_DESKTOP_BIN):$(PATH)
endif

PYTEST = python -m pytest -p no:cacheprovider -v
PYTHON_LOCAL ?= $(shell if [ -x .venv/bin/python ]; then printf ".venv/bin/python"; else printf "python3"; fi)
ANALYZER_URL ?= http://localhost:5001
NER_EVALUATION_JSON ?= presidio/evaluation/reports/huggingface-candidate.json
NER_EVALUATION_MD ?= docs/research/ner-migration-candidate.md
NER_EVALUATION_RUNTIME_CONTAINER ?= presidio-analyzer
NER_EVALUATION_MODEL_SHA256 ?= 6a2c875d02398554ec69384f489a0bf4fe3505fc347c6cdd3385d4fd31ef21a4
NER_EVALUATION_SYSTEM ?= fef2 secret-detection BERT + current recognizers
NER_EVALUATION_FLAGS ?=
ANALYZER_IMAGE ?= ru-llm-proxy-presidio-analyzer:latest
PYTEST_DOCKER_FLAGS = --rm --no-deps --build \
	-e PYTHONPATH=/workspace:/workspace/presidio \
	-e PYTHONDONTWRITEBYTECODE=1 \
	-v .:/workspace:ro \
	-w /workspace

STACK_LITELLM_PRESIDIO := litellm-presidio
STACK_CODEX_LB := litellm-presidio-codex-lb
STACK_START_TIMEOUT ?= 180
ENV_FILE ?= .env
BUILD_ENV_FILE := $(shell if [ -f "$(ENV_FILE)" ]; then printf "%s" "$(ENV_FILE)"; else printf "%s" ".env.example"; fi)
BASE_COMPOSE = docker compose --env-file $(ENV_FILE) -f docker-compose.yml
CODEX_LB_COMPOSE = $(BASE_COMPOSE) -f docker-compose.codex-lb.yml
COMPOSE = $(if $(filter $(STACK_CODEX_LB),$(STACK)),$(CODEX_LB_COMPOSE),$(BASE_COMPOSE))
BUILD_COMPOSE = CODEX_LB_POSTGRES_PASSWORD=build-only CODEX_LB_API_KEY=build-only docker compose --env-file $(BUILD_ENV_FILE) -f docker-compose.yml -f docker-compose.codex-lb.yml --profile test
CLEAN_CODEX_LB_COMPOSE = docker compose --env-file $(BUILD_ENV_FILE) -f docker-compose.yml -f docker-compose.codex-lb.yml

# Default target
help:
	@echo "ru-llm-proxy — команды:"
	@echo ""
	@echo "Составы для setup/up/down/restart/logs/health/clean и проверок стенда:"
	@echo "  STACK=$(STACK_LITELLM_PRESIDIO)"
	@echo "  STACK=$(STACK_CODEX_LB)"
	@echo ""
	@echo "Жизненный цикл:"
	@echo "  make build                         — собрать и загрузить образы обоих составов"
	@echo "  make setup STACK=<состав>          — выполнить первичную настройку выбранного состава"
	@echo "  make up STACK=<состав>             — запустить уже собранный и настроенный состав"
	@echo "  make down STACK=<состав>           — остановить выбранный состав"
	@echo "  make restart STACK=<состав>        — пересоздать контейнеры и перечитать .env"
	@echo "  make logs STACK=<состав>           — показать журналы выбранного состава"
	@echo "  make health STACK=<состав>         — проверить выбранный состав"
	@echo "  make clean STACK=<состав|all>      — удалить данные и локальные образы проекта"
	@echo ""
	@echo "Проверки:"
	@echo "  make test     — быстрый локальный suite: test-unit + test-static"
	@echo "  make test-unit — unit-тесты recognizers/NER, guardrail и flow"
	@echo "  make test-static — lightweight static/asyncio regression tests без Docker"
	@echo "  make test-recognizers — unit-тесты recognizers и NER helpers"
	@echo "  make test-recognizer-api — API-level Analyzer recognizer regression tests"
	@echo "  make test-ner-evaluation — быстрые тесты корпуса и метрик NER"
	@echo "  make test-hf-model — собрать Analyzer и проверить модель без сети"
	@echo "  make test-ner-proxy — проверить mask/block через реальный Analyzer и mock-провайдер"
	@echo "  make test-ner-integration — собрать модель и выполнить полный NER integration gate"
	@echo "  make ner-evaluate STACK=<состав> — оценить запущенный Analyzer на обезличенном корпусе"
	@echo "  make test-guardrail — unit-тесты LiteLLM guardrail"
	@echo "  make test-flow — deterministic guardrail-flow без внешнего LLM"
	@echo "  make test-routing-diagnostics — static tests для routing-smoke и guardrails-smoke Makefile targets"
	@echo "  make test-pre-egress-proxy — Docker smoke: pre-egress no-egress для chat/responses/messages"
	@echo "  make test-final-leak-proxy — Docker smoke: final leak-check не доходит до mock provider"
	@echo "  make test-egress-security — Docker egress-security gate: mock provider capture/no-egress"
	@echo "  make test-observability-gates — lightweight observability/audit gate checks"
	@echo "  make test-e2e STACK=<состав> — live smoke test (нужны сервисы и ключ провайдера)"
	@echo "  make virtual-key-create STACK=<состав> — создать пользовательский ключ LiteLLM"
	@echo "  make client-auth-smoke STACK=<состав> — проверить авторизацию и /v1 протоколы"
	@echo "  make guardrails-list STACK=<состав> — список защитных слоёв LiteLLM"
	@echo "  make guardrails-smoke STACK=<состав> — проверить обычные и потоковые ответы"
	@echo "  make routing-smoke STACK=<состав> — проверить закрепление маршрута"
	@echo "  make metrics STACK=<состав> — показать начало LiteLLM /metrics"
	@echo "  make monitor-smoke STACK=<состав> — проверить health, защитные слои и метрики"
	@echo "  make update-litellm STACK=<состав> — обновить образ LiteLLM и пересоздать proxy"

require-stack:
	@case "$(STACK)" in \
		"$(STACK_LITELLM_PRESIDIO)"|"$(STACK_CODEX_LB)") ;; \
		*) echo "❌ Укажите STACK=$(STACK_LITELLM_PRESIDIO) или STACK=$(STACK_CODEX_LB)"; exit 2 ;; \
	esac

# === Setup ===
setup: require-stack
	bash scripts/setup_env.sh "$(ENV_FILE)" .env.example "$(STACK)"
	@if [ "$(STACK)" = "$(STACK_CODEX_LB)" ]; then \
		bash scripts/setup_codex_lb.sh "$(ENV_FILE)"; \
	else \
		echo "✅ Базовый состав настроен. Следующий шаг: make up STACK=$(STACK_LITELLM_PRESIDIO)"; \
	fi

# === Build ===
build:
	@echo "⬇️  Загрузка готовых образов обоих составов"
	$(BUILD_COMPOSE) pull nginx redis db codex-lb-db
	@echo "🔨 Сборка прикладных и тестовых образов обоих составов"
	$(BUILD_COMPOSE) build --no-cache litellm presidio-analyzer codex-lb guardrail-tests presidio-analyzer-tests

# === Up ===
up: require-stack
	bash scripts/stack_guard.sh mutation "$(STACK)" "$(ENV_FILE)"
	bash scripts/stack_guard.sh preflight "$(STACK)" "$(ENV_FILE)"
	$(COMPOSE) up -d --no-build
	@echo ""
	@echo "⏳ Ожидание запуска сервисов..."
	@bash scripts/stack_health.sh "$(STACK)" "$(ENV_FILE)" "$(STACK_START_TIMEOUT)"

# === Down ===
down: require-stack
	bash scripts/stack_guard.sh mutation "$(STACK)" "$(ENV_FILE)"
	$(COMPOSE) down

# === Restart (recreate containers and reread configuration) ===
restart: require-stack
	bash scripts/stack_guard.sh mutation "$(STACK)" "$(ENV_FILE)"
	bash scripts/stack_guard.sh preflight "$(STACK)" "$(ENV_FILE)"
	$(COMPOSE) up -d --no-build --force-recreate
	@bash scripts/stack_health.sh "$(STACK)" "$(ENV_FILE)" "$(STACK_START_TIMEOUT)"

# === Logs ===
logs: require-stack
	$(COMPOSE) logs -f --tail=50

# === Test ===
test: test-unit test-static

test-static: test-routing-diagnostics
	@echo "🧪 Static and lightweight regression tests"
	$(PYTHON_LOCAL) -m pytest -p no:cacheprovider -q \
		tests/test_analyzer_capacity_config.py \
		tests/test_air_gapped_config.py \
		tests/test_analyzer_image_contract.py \
		tests/test_ner_integration_config.py \
		tests/test_analyzer_telemetry_config.py \
		tests/test_long_context_benchmark.py \
		tests/test_model_profile_config.py \
		tests/test_codex_lb_compose_config.py \
		tests/test_codex_lb_litellm_config.py \
		tests/test_stack_lifecycle.py \
		tests/test_guardrail_entity_contract.py \
		tests/test_recognizer_calibration_config.py \
		tests/test_repository_status_docs.py \
		tests/test_guardrail_dependency_config.py \
		tests/test_pre_egress_policy_config.py \
		tests/test_final_payload_leak_check_config.py \
		tests/test_configuration_docs.py \
		tests/test_compliance_gate_config.py \
		tests/test_production_egress_controls_docs.py \
		tests/test_admin_auth_rbac_docs.py \
		tests/test_zcode_client_docs.py \
		tests/test_regulated_topic_policy_config.py \
		tests/test_synthetic_pii_allowlist_config.py \
		tests/test_dictionary_substitution_config.py \
		presidio/tests/test_capacity.py \
		presidio/tests/test_evaluation.py \
		presidio/tests/test_ner_text_processing.py \
		presidio/tests/test_text_chunking.py

test-recognizers:
	@echo "🧪 Recognizer + NER unit tests"
	docker compose run $(PYTEST_DOCKER_FLAGS) presidio-analyzer-tests \
		$(PYTEST) presidio/tests

test-recognizer-api:
	@echo "🧪 Analyzer credential and API recognizer tests"
	docker compose run $(PYTEST_DOCKER_FLAGS) presidio-analyzer-tests \
		$(PYTEST) \
		presidio/tests/test_result_merging.py \
		presidio/tests/test_credential_rules.py \
		presidio/tests/test_analyzer_api_thresholds.py

test-ner-evaluation:
	@echo "🧪 NER migration corpus and metrics tests"
	$(PYTHON_LOCAL) -m pytest -p no:cacheprovider -q presidio/tests/test_evaluation.py

test-hf-model:
	@echo "🧪 Pinned Hugging Face model smoke test without network"
	docker compose build presidio-analyzer
	@$(MAKE) test-hf-model-run

test-hf-model-run:
	@echo "🧪 Run pinned Hugging Face model smoke without network"
	docker run --rm --network none $(ANALYZER_IMAGE) python verify_cpu_runtime.py
	docker run --rm --network none $(ANALYZER_IMAGE) python real_model_smoke.py

test-ner-proxy:
	@echo "🧪 Real Analyzer proxy mask/block flow with mock provider"
	bash tests/e2e/test_ner_proxy_flow.sh

test-ner-integration:
	@echo "🧪 Full pinned NER integration gate"
	@$(MAKE) test-hf-model
	@$(MAKE) test-ner-proxy

ner-evaluate: require-stack
	@echo "📊 NER evaluation via $(ANALYZER_URL)"
	$(PYTHON_LOCAL) -m presidio.evaluation.run_baseline \
		--analyzer-url "$(ANALYZER_URL)" \
		--system "$(NER_EVALUATION_SYSTEM)" \
		--runtime-container "$(NER_EVALUATION_RUNTIME_CONTAINER)" \
		--model-artifact-sha256 "$(NER_EVALUATION_MODEL_SHA256)" \
		--model-checksum-enforced \
		--json-output "$(NER_EVALUATION_JSON)" \
		--markdown-output "$(NER_EVALUATION_MD)" $(NER_EVALUATION_FLAGS)

test-guardrail:
	@echo "🧪 LiteLLM guardrail unit tests"
	docker compose run $(PYTEST_DOCKER_FLAGS) guardrail-tests \
		$(PYTEST) litellm_guardrails/tests

test-flow:
	@echo "🧪 Deterministic guardrail-flow test"
	docker compose run $(PYTEST_DOCKER_FLAGS) guardrail-tests \
		$(PYTEST) tests/e2e/test_guardrail_flow.py

test-pre-egress-proxy:
	@echo "🧪 Pre-egress proxy non-egress smoke"
	bash tests/e2e/test_pre_egress_proxy_non_egress.sh

test-final-leak-proxy:
	@echo "🧪 Final payload leak-check proxy non-egress smoke"
	bash tests/e2e/test_final_leak_proxy_non_egress.sh

test-egress-security:
	@echo "🧪 Egress-security gate: mock provider capture/no-egress"
	@$(MAKE) test-pre-egress-proxy
	@$(MAKE) test-final-leak-proxy

test-observability-gates:
	@echo "🧪 Observability gate: audit/logging contract checks"
	$(PYTHON_LOCAL) -m pytest -p no:cacheprovider -q tests/test_compliance_gate_config.py

test-routing-diagnostics:
	@echo "🧪 Makefile diagnostics static tests"
	$(PYTHON_LOCAL) tests/test_makefile_routing_smoke.py
	$(PYTHON_LOCAL) tests/test_makefile_guardrails_smoke.py

# === Health check ===
health: require-stack
	bash scripts/stack_health.sh "$(STACK)" "$(ENV_FILE)"

# === Clean ===
clean:
	@case "$(STACK)" in \
		"$(STACK_LITELLM_PRESIDIO)"|"$(STACK_CODEX_LB)"|all) ;; \
		*) echo "❌ Укажите STACK=$(STACK_LITELLM_PRESIDIO), STACK=$(STACK_CODEX_LB) или STACK=all"; exit 2 ;; \
	esac
	@if [ "$(STACK)" != "all" ]; then bash scripts/stack_guard.sh mutation "$(STACK)" "$(ENV_FILE)"; fi
	@echo "⚠️  Это удалит все данные (БД, Redis, Docker-образы)"
	@read -p "Продолжить? [y/N] " confirm && [ "$$confirm" = "y" ] || exit 1
	@if [ "$(STACK)" = "all" ] || [ "$(STACK)" = "$(STACK_CODEX_LB)" ]; then \
		$(CLEAN_CODEX_LB_COMPOSE) down -v --rmi local --remove-orphans; \
	else \
		$(BASE_COMPOSE) down -v --rmi local --remove-orphans; \
	fi
	@echo "✅ Очищено"

# === Unit tests (all) ===
test-unit:
	@echo "🧪 Запуск всех unit-тестов..."
	@$(MAKE) test-recognizers
	@$(MAKE) test-guardrail
	@$(MAKE) test-flow
	@echo "✅ Unit suite completed"

# === Live smoke test (requires running services) ===
test-e2e: require-stack
	@echo "🧪 Live smoke test (требуются запущенные сервисы и LLM provider key)"
	@if [ ! -f .env ]; then echo "❌ .env not found"; exit 1; fi
	@RU_LLM_PROXY_TOKEN=$$(bash scripts/create_virtual_key.sh \
			--alias "e2e-$$(date +%Y%m%d%H%M%S)" \
			--models standard,zai \
			--duration 30m | awk -F= '$$1 == "RU_LLM_PROXY_TOKEN" {print $$2; exit}'); \
		if [ -z "$$RU_LLM_PROXY_TOKEN" ]; then echo "❌ failed to create e2e virtual key"; exit 1; fi; \
		export RU_LLM_PROXY_TOKEN; \
		bash tests/e2e/test_e2e.sh

# === Client access ===
virtual-key-create: require-stack
	@KEY_ALIAS="$(KEY_ALIAS)" \
		MODELS="$(MODELS)" \
		DURATION="$(DURATION)" \
		MAX_BUDGET="$(MAX_BUDGET)" \
		BUDGET_DURATION="$(BUDGET_DURATION)" \
		RPM_LIMIT="$(RPM_LIMIT)" \
		TPM_LIMIT="$(TPM_LIMIT)" \
		USER_ID="$(USER_ID)" \
		TEAM_ID="$(TEAM_ID)" \
		METADATA_JSON='$(METADATA_JSON)' \
		bash scripts/create_virtual_key.sh

client-auth-smoke: require-stack
	@bash tests/e2e/test_client_auth.sh

# === Guardrails diagnostics ===
guardrails-list: require-stack
	@echo "🛡️  LiteLLM registered guardrails"
	@if [ ! -f .env ]; then echo "❌ .env not found"; exit 1; fi
	@eval "$$(grep LITELLM_MASTER_KEY .env | sed 's/^/export /')" && \
		response=$$(curl -sS -H "Authorization: Bearer $$LITELLM_MASTER_KEY" http://localhost:4000/guardrails/list); \
		if command -v jq >/dev/null 2>&1; then printf "%s\n" "$$response" | jq .; else printf "%s\n" "$$response"; fi

guardrails-smoke: require-stack
	@bash tests/e2e/test_guardrails_smoke.sh

# === Routing diagnostics ===
routing-smoke: require-stack
	@echo "🧭 LiteLLM sticky routing smoke"
	@if [ ! -f .env ]; then echo "❌ .env not found"; exit 1; fi
	@eval "$$(grep -E '^(LITELLM_MASTER_KEY|LITELLM_ROUTING_TEST_KEY)=' .env | sed 's/^/export /')" && \
		token="$${LITELLM_ROUTING_TEST_KEY:-$$LITELLM_MASTER_KEY}" && \
		if [ -z "$$token" ]; then echo "❌ LITELLM_MASTER_KEY or LITELLM_ROUTING_TEST_KEY is required"; exit 1; fi && \
		first_headers=$$(mktemp) && second_headers=$$(mktemp) && first_body=$$(mktemp) && second_body=$$(mktemp) && \
		trap 'rm -f "$$first_headers" "$$second_headers" "$$first_body" "$$second_body"' EXIT && \
		run_completion() { \
			label="$$1"; headers_file="$$2"; body_file="$$3"; payload="$$4"; \
			error_file=$$(mktemp); \
			status=$$(curl -sS -D "$$headers_file" -o "$$body_file" -w "%{http_code}" http://localhost:4000/v1/chat/completions \
				-H "Authorization: Bearer $$token" \
				-H "Content-Type: application/json" \
				-d "$$payload" 2>"$$error_file"); \
			curl_exit=$$?; \
			if [ "$$curl_exit" -ne 0 ]; then \
				echo "❌ $$label request failed to reach LiteLLM"; \
				if [ -s "$$error_file" ]; then sed -n '1,5p' "$$error_file"; fi; \
				rm -f "$$error_file"; \
				return 1; \
			fi; \
			rm -f "$$error_file"; \
			case "$$status" in \
				2??) return 0 ;; \
				*) \
					echo "❌ $$label request returned HTTP $$status"; \
					if [ -s "$$headers_file" ]; then \
						echo "Response diagnostics:"; \
						awk 'tolower($$0) ~ /^(content-type|x-request-id|x-litellm-call-id|x-litellm-model-id):/ {gsub(/\r/, "", $$0); print $$0}' "$$headers_file"; \
					fi; \
					return 1; \
					;; \
			esac; \
		}; \
		routing_model="$${ROUTING_SMOKE_MODEL:-glm-5.2}" && \
		run_completion "first" "$$first_headers" "$$first_body" "{\"model\":\"$$routing_model\",\"messages\":[{\"role\":\"user\",\"content\":\"Коротко ответь: routing smoke 1\"}],\"max_tokens\":16}" && \
		run_completion "second" "$$second_headers" "$$second_body" "{\"model\":\"$$routing_model\",\"messages\":[{\"role\":\"user\",\"content\":\"Коротко ответь: routing smoke 2\"}],\"max_tokens\":16}" && \
		first_model=$$(awk 'tolower($$0) ~ /^x-litellm-model-id:/ {sub(/^[^:]*:[[:space:]]*/, "", $$0); gsub(/\r/, "", $$0); print $$0; exit}' "$$first_headers") && \
		second_model=$$(awk 'tolower($$0) ~ /^x-litellm-model-id:/ {sub(/^[^:]*:[[:space:]]*/, "", $$0); gsub(/\r/, "", $$0); print $$0; exit}' "$$second_headers") && \
		if [ -z "$$first_model" ] || [ -z "$$second_model" ]; then echo "❌ x-litellm-model-id header not found"; exit 1; fi && \
		echo "First deployment:  $$first_model" && \
		echo "Second deployment: $$second_model" && \
		if [ "$$first_model" = "$$second_model" ]; then echo "✅ Same key stayed on one deployment"; else echo "❌ Deployment changed for the same key"; exit 1; fi

# === Monitoring diagnostics ===
metrics: require-stack
	@echo "📈 LiteLLM /metrics"
	@tmp=$$(mktemp) && \
		curl -L -sf http://localhost:4000/metrics > "$$tmp" && \
		sed -n '1,120p' "$$tmp"; \
		status=$$?; rm -f "$$tmp"; exit $$status

monitor-smoke: require-stack
	@echo "📈 Monitoring smoke check"
	@$(MAKE) health STACK="$(STACK)" ENV_FILE="$(ENV_FILE)"
	@$(MAKE) guardrails-list STACK="$(STACK)" ENV_FILE="$(ENV_FILE)"
	@analyzer_health=$$(curl -sf http://localhost:5001/api/v1/health); \
		if printf "%s" "$$analyzer_health" | grep -q '"ner_state":"ready"' && printf "%s" "$$analyzer_health" | grep -q '"ner_warmed_up":true'; then echo "✅ Pinned Hugging Face NER ready"; else echo "❌ Pinned Hugging Face NER is not ready"; printf "%s\n" "$$analyzer_health"; exit 1; fi
	@tmp=$$(mktemp) && \
		if ! curl -L -sf http://localhost:4000/metrics > "$$tmp"; then echo "❌ LiteLLM metrics endpoint is not reachable"; rm -f "$$tmp"; exit 1; fi; \
		if grep -q "litellm_" "$$tmp"; then echo "✅ LiteLLM metrics exposed"; else echo "❌ LiteLLM metrics not found"; rm -f "$$tmp"; exit 1; fi; \
		if grep -q "ru_pii_guardrail_" "$$tmp"; then echo "✅ PII guardrail metrics exposed"; else echo "⚠️  PII guardrail metrics not emitted yet; run a PII request and retry"; fi; \
		rm -f "$$tmp"
	@tmp=$$(mktemp) && \
		if ! curl -sf http://localhost:5001/metrics > "$$tmp"; then echo "❌ Presidio Analyzer metrics endpoint is not reachable"; rm -f "$$tmp"; exit 1; fi; \
		if grep -q "ru_presidio_analyzer_" "$$tmp"; then echo "✅ Presidio Analyzer metrics exposed"; else echo "❌ Presidio Analyzer metrics not found"; rm -f "$$tmp"; exit 1; fi; \
		rm -f "$$tmp"

# === LiteLLM update ===
update-litellm: require-stack
	@echo "⬇️  Rebuilding LiteLLM from the latest configured base image"
	$(COMPOSE) build --pull litellm
	$(COMPOSE) up -d --force-recreate --no-deps litellm
	@echo "✅ LiteLLM image updated and proxy container recreated"
