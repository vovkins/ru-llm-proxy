.PHONY: setup build up down restart logs test test-unit test-static test-recognizers test-recognizer-api test-ner-evaluation test-hf-model test-hf-model-run test-ner-proxy test-ner-integration ner-evaluate test-guardrail test-flow test-routing-diagnostics test-e2e test-pre-egress-proxy test-final-leak-proxy test-egress-security test-observability-gates virtual-key-create client-auth-smoke guardrails-list guardrails-smoke routing-smoke metrics monitor-smoke update-litellm health clean help require-stack require-analyzer-profile require-deployment

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
ANALYZER_PROFILE ?= cpu
ANALYZER_PROFILE_CPU := cpu
ANALYZER_PROFILE_GPU := gpu
ANALYZER_COMPOSE_FILE = $(if $(filter $(ANALYZER_PROFILE_GPU),$(ANALYZER_PROFILE)),-f docker-compose.gpu.yml,)
ANALYZER_DOCKER_FLAGS = $(if $(filter $(ANALYZER_PROFILE_GPU),$(ANALYZER_PROFILE)),--gpus all,)
PYTEST_DOCKER_FLAGS = --rm --no-deps --build \
	-e PYTHONPATH=/workspace:/workspace/presidio \
	-e PYTHONDONTWRITEBYTECODE=1 \
	-v .:/workspace:ro \
	-w /workspace

STACK_LITELLM_PRESIDIO := litellm-presidio
STACK_CODEX_LB := litellm-presidio-codex-lb
STACK_START_TIMEOUT ?= 180
ENV_FILE ?= .env
TOPOLOGY ?= functional
PII_MODE_ORIGIN := $(origin PII_GUARDRAIL_MODE)
export STACK ENV_FILE TOPOLOGY TOPOLOGY_ENV ANALYZER_PROFILE STACK_START_TIMEOUT PII_MODE_ORIGIN
DEPLOY = $(PYTHON_LOCAL) scripts/deployment.py
BUILD_ENV_FILE := $(shell if [ -f "$(ENV_FILE)" ]; then printf "%s" "$(ENV_FILE)"; else printf "%s" ".env.example"; fi)
BASE_COMPOSE = docker compose --env-file "$(ENV_FILE)" -f docker-compose.yml $(ANALYZER_COMPOSE_FILE)
COMPOSE = $(DEPLOY) compose
BUILD_COMPOSE = CODEX_LB_POSTGRES_PASSWORD=build-only CODEX_LB_API_KEY=build-only docker compose --env-file "$(BUILD_ENV_FILE)" -f docker-compose.yml $(ANALYZER_COMPOSE_FILE) -f docker-compose.codex-lb.yml --profile test

# Default target
help:
	@echo "ru-llm-proxy — команды:"
	@echo ""
	@echo "Составы для setup/up/down/restart/logs/health/clean и проверок стенда:"
	@echo "  STACK=$(STACK_LITELLM_PRESIDIO)"
	@echo "  STACK=$(STACK_CODEX_LB)"
	@echo "  ANALYZER_PROFILE=$(ANALYZER_PROFILE_CPU)  — Analyzer на CPU (по умолчанию)"
	@echo "  ANALYZER_PROFILE=$(ANALYZER_PROFILE_GPU)  — Analyzer на одной NVIDIA GPU"
	@echo "  TOPOLOGY=functional|production — один экземпляр или CPU-пул 2 LiteLLM / 4 Analyzer"
	@echo "  TOPOLOGY_ENV=<файл> — дополнительные количества и ресурсы production"
	@echo "  PII_GUARDRAIL_MODE=MASK|BLOCK — явный выбор сохраняется в ENV_FILE"
	@echo "  Приоритет режима: параметр make, окружение, ENV_FILE, mask"
	@echo ""
	@echo "Жизненный цикл:"
	@echo "  make build ANALYZER_PROFILE=<профиль> — собрать и загрузить образы обоих составов"
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
	@echo "  make test-hf-model ANALYZER_PROFILE=<профиль> — собрать и проверить модель без сети"
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

require-analyzer-profile:
	@case "$(ANALYZER_PROFILE)" in \
		"$(ANALYZER_PROFILE_CPU)"|"$(ANALYZER_PROFILE_GPU)") ;; \
		*) echo "❌ Укажите ANALYZER_PROFILE=$(ANALYZER_PROFILE_CPU) или ANALYZER_PROFILE=$(ANALYZER_PROFILE_GPU)"; exit 2 ;; \
	esac

# === Setup ===
require-deployment:
	@$(DEPLOY) validate

setup: require-stack require-analyzer-profile require-deployment
	@$(DEPLOY) setup

# === Build ===
build: require-analyzer-profile
	@echo "⬇️  Загрузка готовых образов обоих составов"
	$(BUILD_COMPOSE) pull nginx redis db codex-lb-db
	@echo "🔨 Сборка прикладных и тестовых образов обоих составов"
	$(BUILD_COMPOSE) build --no-cache litellm presidio-analyzer codex-lb guardrail-tests presidio-analyzer-tests

# === Up ===
up: require-stack require-analyzer-profile require-deployment
	@$(DEPLOY) start --no-build

# === Down ===
down: require-stack require-analyzer-profile require-deployment
	bash scripts/stack_guard.sh mutation "$(STACK)" "$(ENV_FILE)"
	$(COMPOSE) down

# === Restart (recreate containers and reread configuration) ===
restart: require-stack require-analyzer-profile require-deployment
	@$(DEPLOY) start --no-build --force-recreate

# === Logs ===
logs: require-stack require-analyzer-profile require-deployment
	@$(DEPLOY) guard mutation
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
		tests/test_load_profile.py \
		tests/test_load_compose_config.py \
		tests/test_model_profile_config.py \
		tests/test_codex_lb_compose_config.py \
		tests/test_codex_lb_litellm_config.py \
		tests/test_stack_lifecycle.py \
		tests/test_deployment_topologies.py \
		tests/test_guardrail_entity_contract.py \
		tests/test_recognizer_calibration_config.py \
		tests/test_repository_status_docs.py \
		tests/test_guardrail_dependency_config.py \
		tests/test_pre_egress_policy_config.py \
		tests/test_final_payload_leak_check_config.py \
		tests/test_nonstream_disconnect_config.py \
		tests/test_controlled_sse.py \
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
		presidio/tests/test_encoded_secrets.py \
		presidio/tests/test_analyzer_api_thresholds.py

test-ner-evaluation:
	@echo "🧪 NER migration corpus and metrics tests"
	$(PYTHON_LOCAL) -m pytest -p no:cacheprovider -q presidio/tests/test_evaluation.py

test-hf-model: require-analyzer-profile
	@echo "🧪 Pinned Hugging Face model smoke test without network ($(ANALYZER_PROFILE))"
	$(BASE_COMPOSE) build presidio-analyzer
	@$(MAKE) test-hf-model-run ANALYZER_PROFILE="$(ANALYZER_PROFILE)" ANALYZER_IMAGE="$(ANALYZER_IMAGE)"

test-hf-model-run: require-analyzer-profile
	@echo "🧪 Run pinned Hugging Face model smoke without network ($(ANALYZER_PROFILE))"
	@if [ "$(ANALYZER_PROFILE)" = "$(ANALYZER_PROFILE_GPU)" ]; then \
		docker run --rm --network none $(ANALYZER_DOCKER_FLAGS) $(ANALYZER_IMAGE) python verify_gpu_runtime.py; \
	else \
		docker run --rm --network none $(ANALYZER_IMAGE) python verify_cpu_runtime.py; \
	fi
	docker run --rm --network none $(ANALYZER_DOCKER_FLAGS) $(ANALYZER_IMAGE) python real_model_smoke.py

test-ner-proxy:
	@echo "🧪 Real Analyzer proxy mask/block flow with mock provider"
	bash tests/e2e/test_ner_proxy_flow.sh
	@if [ "$(ANALYZER_PROFILE)" = cpu ]; then \
		$(BUILD_COMPOSE) build litellm codex-lb && \
		$(PYTHON_LOCAL) tests/e2e/deployment_topology_gate.py; \
	fi

test-ner-integration: require-analyzer-profile
	@echo "🧪 Full pinned NER integration gate"
	@$(MAKE) test-hf-model ANALYZER_PROFILE="$(ANALYZER_PROFILE)"
	@$(MAKE) test-ner-proxy ANALYZER_PROFILE="$(ANALYZER_PROFILE)"

ner-evaluate: require-stack require-deployment
	@$(DEPLOY) guard mutation
	@echo "📊 NER evaluation via $(ANALYZER_URL)"
	@runtime="$(NER_EVALUATION_RUNTIME_CONTAINER)"; \
		if [ "$(TOPOLOGY)" = production ] && [ "$$runtime" = presidio-analyzer ]; then \
			runtime=$$($(COMPOSE) ps -q presidio-analyzer | head -n 1); \
			if [ -z "$$runtime" ]; then echo "Analyzer не запущен"; exit 1; fi; \
		fi; \
	$(PYTHON_LOCAL) -m presidio.evaluation.run_baseline \
		--analyzer-url "$(ANALYZER_URL)" \
		--system "$(NER_EVALUATION_SYSTEM)" \
		--runtime-container "$$runtime" \
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
	bash tests/e2e/test_nonstream_disconnect.sh

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
health: require-stack require-analyzer-profile require-deployment
	bash scripts/stack_health.sh "$(STACK)" "$(ENV_FILE)"

# === Clean ===
clean:
	@case "$(STACK)" in \
		"$(STACK_LITELLM_PRESIDIO)"|"$(STACK_CODEX_LB)"|all) ;; \
		*) echo "❌ Укажите STACK=$(STACK_LITELLM_PRESIDIO), STACK=$(STACK_CODEX_LB) или STACK=all"; exit 2 ;; \
	esac
	@STACK=$(if $(filter all,$(STACK)),$(STACK_CODEX_LB),$(STACK)) $(DEPLOY) validate
	@if [ "$(STACK)" != "all" ]; then bash scripts/stack_guard.sh mutation "$(STACK)" "$(ENV_FILE)"; fi
	@echo "⚠️  Это удалит все данные (БД, Redis, Docker-образы)"
	@read -p "Продолжить? [y/N] " confirm && [ "$$confirm" = "y" ] || exit 1
	@if [ "$(STACK)" = "all" ] || [ "$(STACK)" = "$(STACK_CODEX_LB)" ]; then \
		STACK=$(STACK_CODEX_LB) $(DEPLOY) --env-file "$(BUILD_ENV_FILE)" compose down -v --rmi local --remove-orphans; \
	else \
		$(COMPOSE) down -v --rmi local --remove-orphans; \
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
test-e2e: require-stack require-deployment
	@$(DEPLOY) guard mutation
	@echo "🧪 Live smoke test (требуются запущенные сервисы и LLM provider key)"
	@if [ ! -f "$(ENV_FILE)" ]; then echo "❌ ENV_FILE not found"; exit 1; fi
	@RU_LLM_PROXY_TOKEN=$$(bash scripts/create_virtual_key.sh \
			--alias "e2e-$$(date +%Y%m%d%H%M%S)" \
			--env-file "$(ENV_FILE)" --base-url "$$( $(DEPLOY) url )" \
			--models standard,zai \
			--duration 30m | awk -F= '$$1 == "RU_LLM_PROXY_TOKEN" {print $$2; exit}'); \
		if [ -z "$$RU_LLM_PROXY_TOKEN" ]; then echo "❌ failed to create e2e virtual key"; exit 1; fi; \
		export RU_LLM_PROXY_TOKEN; \
		$(DEPLOY) exec bash tests/e2e/test_e2e.sh

# === Client access ===
virtual-key-create: require-stack require-deployment
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
		$(DEPLOY) exec bash scripts/create_virtual_key.sh

client-auth-smoke: require-stack require-deployment
	@$(DEPLOY) exec bash tests/e2e/test_client_auth.sh

# === Guardrails diagnostics ===
guardrails-list: require-stack require-deployment
	@$(DEPLOY) guard mutation
	@echo "🛡️  LiteLLM registered guardrails"
	@if [ ! -f "$(ENV_FILE)" ]; then echo "❌ ENV_FILE not found"; exit 1; fi
	@eval "$$(grep '^LITELLM_MASTER_KEY=' "$(ENV_FILE)" | sed 's/^/export /')" && \
		response=$$(curl -fsS -H "Authorization: Bearer $$LITELLM_MASTER_KEY" "$$( $(DEPLOY) url )/guardrails/list"); \
		if command -v jq >/dev/null 2>&1; then printf "%s\n" "$$response" | jq .; else printf "%s\n" "$$response"; fi

guardrails-smoke: require-stack require-deployment
	@$(DEPLOY) exec bash tests/e2e/test_guardrails_smoke.sh

# === Routing diagnostics ===
routing-smoke: require-stack require-deployment
	@$(DEPLOY) guard mutation
	@echo "🧭 LiteLLM sticky routing smoke"
	@if [ ! -f "$(ENV_FILE)" ]; then echo "❌ ENV_FILE not found"; exit 1; fi
	@export LITELLM_URL="$$( $(DEPLOY) url )" && \
		eval "$$(grep -E '^(LITELLM_MASTER_KEY|LITELLM_ROUTING_TEST_KEY)=' "$(ENV_FILE)" | sed 's/^/export /')" && \
		token="$${LITELLM_ROUTING_TEST_KEY:-$$LITELLM_MASTER_KEY}" && \
		if [ -z "$$token" ]; then echo "❌ LITELLM_MASTER_KEY or LITELLM_ROUTING_TEST_KEY is required"; exit 1; fi && \
		first_headers=$$(mktemp) && second_headers=$$(mktemp) && first_body=$$(mktemp) && second_body=$$(mktemp) && \
		trap 'rm -f "$$first_headers" "$$second_headers" "$$first_body" "$$second_body"' EXIT && \
		run_completion() { \
			label="$$1"; headers_file="$$2"; body_file="$$3"; payload="$$4"; \
			error_file=$$(mktemp); \
			status=$$(curl -sS -D "$$headers_file" -o "$$body_file" -w "%{http_code}" "$$LITELLM_URL/v1/chat/completions" \
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
		routing_model="$${ROUTING_SMOKE_MODEL:-glm-5.3-flash}" && \
		run_completion "first" "$$first_headers" "$$first_body" "{\"model\":\"$$routing_model\",\"messages\":[{\"role\":\"user\",\"content\":\"Коротко ответь: routing smoke 1\"}],\"max_tokens\":16}" && \
		run_completion "second" "$$second_headers" "$$second_body" "{\"model\":\"$$routing_model\",\"messages\":[{\"role\":\"user\",\"content\":\"Коротко ответь: routing smoke 2\"}],\"max_tokens\":16}" && \
		first_model=$$(awk 'tolower($$0) ~ /^x-litellm-model-id:/ {sub(/^[^:]*:[[:space:]]*/, "", $$0); gsub(/\r/, "", $$0); print $$0; exit}' "$$first_headers") && \
		second_model=$$(awk 'tolower($$0) ~ /^x-litellm-model-id:/ {sub(/^[^:]*:[[:space:]]*/, "", $$0); gsub(/\r/, "", $$0); print $$0; exit}' "$$second_headers") && \
		if [ -z "$$first_model" ] || [ -z "$$second_model" ]; then echo "❌ x-litellm-model-id header not found"; exit 1; fi && \
		echo "First deployment:  $$first_model" && \
		echo "Second deployment: $$second_model" && \
		if [ "$$first_model" = "$$second_model" ]; then echo "✅ Same key stayed on one deployment"; else echo "❌ Deployment changed for the same key"; exit 1; fi

# === Monitoring diagnostics ===
metrics: require-stack require-deployment
	@$(DEPLOY) metrics

monitor-smoke: require-stack require-deployment
	@echo "📈 Monitoring smoke check"
	@$(MAKE) health STACK="$(STACK)" ENV_FILE="$(ENV_FILE)"
	@$(MAKE) guardrails-list STACK="$(STACK)" ENV_FILE="$(ENV_FILE)"
	@$(DEPLOY) metrics

# === LiteLLM update ===
update-litellm: require-stack require-deployment
	@echo "⬇️  Rebuilding LiteLLM from the latest configured base image"
	$(COMPOSE) build --pull litellm
	@$(DEPLOY) start --no-build --force-recreate --only-litellm
	@echo "✅ LiteLLM image updated and proxy container recreated"
