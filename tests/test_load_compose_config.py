"""Static safety and topology checks for the isolated load-test contour."""

from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]
COMPOSE_PATH = ROOT / "tests" / "load" / "docker-compose.yml"
CONFIG_PATH = ROOT / "tests" / "load" / "litellm-config.yaml"
LOCUST_IMAGE = (
    "locustio/locust:2.46.5@"
    "sha256:02109fe359906ebea8a1a1f4c1567771e815990739b9400c33d6c2692ac00af5"
)


def _compose() -> dict:
    return yaml.safe_load(COMPOSE_PATH.read_text(encoding="utf-8"))


def test_load_contour_is_isolated_and_uses_real_stateful_dependencies():
    compose = _compose()
    services = compose["services"]

    assert compose["name"] == "ru-llm-proxy-load"
    assert set(services) == {
        "load-db",
        "load-redis",
        "load-mock-upstream",
        "load-mock-upstream-a",
        "load-mock-upstream-b",
        "load-presidio-analyzer",
        "load-analyzer-router",
        "load-litellm",
        "load-litellm-router",
        "load-key-manager",
        "load-generator",
    }
    assert services["load-db"]["image"].startswith("postgres:16-alpine@sha256:")
    assert services["load-redis"]["image"] == "redis:7-alpine"
    assert services["load-presidio-analyzer"]["build"]["target"] == "analyzer"
    assert "ports" not in services["load-presidio-analyzer"]
    assert "ports" not in services["load-litellm"]
    assert services["load-analyzer-router"]["image"] == "nginx:1.27-alpine"
    assert services["load-litellm-router"]["image"] == "nginx:1.27-alpine"
    assert services["load-litellm"]["environment"]["PRESIDIO_ANALYZER_URL"] == (
        "${LOAD_ANALYZER_URL:-http://load-analyzer-router:5001}"
    )
    assert services["load-litellm"]["depends_on"]["load-db"]["condition"] == (
        "service_healthy"
    )


def test_load_contour_cannot_send_requests_to_a_real_provider():
    compose_text = COMPOSE_PATH.read_text(encoding="utf-8")
    compose = yaml.safe_load(compose_text)
    services = compose["services"]
    config = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8"))

    assert "api.openai.com" not in compose_text
    assert "api.z.ai" not in compose_text
    mock_chat = [
        item for item in config["model_list"] if item["model_name"] == "mock-chat"
    ]
    assert {item["litellm_params"]["api_base"] for item in mock_chat} == {
        "http://load-mock-upstream-a:8080/v1",
        "http://load-mock-upstream-b:8080/v1",
    }
    assert {item["model_info"]["id"] for item in mock_chat} == {
        "load-mock-a",
        "load-mock-b",
    }
    assert config["router_settings"]["optional_pre_call_checks"] == [
        "deployment_affinity"
    ]
    assert config["router_settings"]["deployment_affinity_ttl_seconds"] == 86400
    assert config["general_settings"]["user_api_key_cache_ttl"] == 5
    assert services["load-litellm"]["environment"]["PII_MAPPING_TTL_SECONDS"] == (
        "${LOAD_PII_MAPPING_TTL_SECONDS:-7200}"
    )


def test_load_balancers_refresh_scaled_services_and_expire_idle_upstreams():
    for name in ("analyzer.conf", "litellm.conf"):
        config = (ROOT / "tests" / "load" / "nginx" / name).read_text(
            encoding="utf-8"
        )
        assert "resolver 127.0.0.11 valid=1s ipv6=off;" in config
        assert " resolve;" in config
        assert "proxy_next_upstream_tries 2;" in config
        assert "keepalive_timeout 4s;" in config

    analyzer = (ROOT / "tests" / "load" / "nginx" / "analyzer.conf").read_text(
        encoding="utf-8"
    )
    litellm = (ROOT / "tests" / "load" / "nginx" / "litellm.conf").read_text(
        encoding="utf-8"
    )
    assert "proxy_next_upstream error timeout http_502 http_503 non_idempotent;" in analyzer
    assert "proxy_next_upstream error timeout;" in litellm


def test_locust_and_key_manager_are_pinned_and_explicitly_profiled():
    services = _compose()["services"]
    for name in ("load-key-manager", "load-generator"):
        assert services[name]["image"] == LOCUST_IMAGE
        assert services[name]["profiles"] == ["load"]

    manager = services["load-key-manager"]
    generator = services["load-generator"]
    assert manager["environment"]["LOAD_USERS"] == "${LOAD_USERS:-400}"
    assert manager["user"] == "0:0"
    assert manager["environment"]["LOAD_KEY_FILE_UID"] == "1000"
    assert "user" not in generator
    assert "load-state:/state" in manager["volumes"]
    assert "load-state:/state:ro" in generator["volumes"]
    assert generator["environment"]["LOAD_EXPECTED_USERS"] == "${LOAD_USERS:-8}"
    assert generator["environment"]["LOAD_KEY_SHARD_INDEX"] == "${LOAD_KEY_SHARD_INDEX:-0}"
    assert generator["environment"]["LOAD_REPORT_NODE"] == "${LOAD_REPORT_NODE:-local}"
    assert generator["environment"]["LOAD_INPUT_VARIATION"] == (
        "${LOAD_INPUT_VARIATION:-repeat}"
    )
    assert generator["environment"]["LOAD_ANALYZER_REPLICAS"] == (
        "${LOAD_ANALYZER_REPLICAS:-1}"
    )
    assert generator["environment"]["LOAD_ANALYZER_BACKEND"] == (
        "${LOAD_ANALYZER_BACKEND:-real}"
    )
    assert generator["environment"]["LOAD_ANALYZER_QUEUE_LIMIT"] == (
        "${LOAD_ANALYZER_QUEUE_LIMIT:-8}"
    )
    assert generator["environment"]["LOAD_GUARDRAIL_ANALYZER_MAX_CONNECTIONS"] == (
        "${LOAD_GUARDRAIL_ANALYZER_MAX_CONNECTIONS:-20}"
    )
    assert generator["environment"]["LOAD_GUARDRAIL_REDIS_MAX_CONNECTIONS"] == (
        "${LOAD_GUARDRAIL_REDIS_MAX_CONNECTIONS:-20}"
    )


def test_load_proxy_uses_project_guardrails_with_safe_failure_defaults():
    service = _compose()["services"]["load-litellm"]
    config = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8"))

    assert service["environment"]["PII_GUARDRAIL_MODE"] == "mask"
    assert service["environment"]["PII_GUARDRAIL_FAILURE_MODE"] == "fail_closed"
    assert service["environment"]["FINAL_PAYLOAD_LEAK_CHECK_MODE"] == "block"
    assert service["environment"]["DATABASE_URL"].startswith("postgresql://load:")
    assert {item["litellm_params"]["mode"] for item in config["guardrails"]} == {
        "pre_call",
        "post_call",
    }
    assert config["litellm_settings"]["require_auth_for_metrics_endpoint"] is False


def test_run_script_requires_explicit_consent_for_real_provider_load():
    script = (ROOT / "tests" / "load" / "run.sh").read_text(encoding="utf-8")

    assert 'LOAD_CONTOUR=${LOAD_CONTOUR:-mock}' in script
    assert 'LOAD_ALLOW_REAL_PROVIDER:-false' in script
    assert "LOAD_EXTERNAL_KEY_FILE" in script
    assert "compose_scaffold_created=true" in script
    assert "LOAD_VALIDATE_MAPPING=false" in script
    assert 'LOAD_REPORT_NODE=${LOAD_REPORT_NODE:-local}' in script
    assert '-e LOAD_REPORT_NODE="$LOAD_REPORT_NODE"' in script
    assert '"${COMPOSE[@]}" --profile load down -v --remove-orphans' in script
    assert "/Applications/Docker.app/Contents/Resources/bin" in script
    assert 'export PATH="$DOCKER_DESKTOP_BIN:$PATH"' in script
    assert '"$LOAD_RESULTS_DIR/$mock_service-capture.json"' in script
    assert "stateful_checks.py" in script
    assert "LOAD_STATEFUL_CHECKS=true" in script
    assert "LOAD_STATEFUL_REVOCATION_TIMEOUT_SECONDS" in script
    assert "LOAD_STATEFUL_REVOCATION_REQUIRED_DENIALS" in script
    assert "LOAD_GUARDRAIL_ANALYZER_TIMEOUT_SECONDS:-90" in script
    assert "LOAD_READ_TIMEOUT_SECONDS:-150" in script
    assert "LOAD_SPAWN_RATE=${LOAD_SPAWN_RATE:-0.5}" in script
    assert "resilience)" in script
    assert "resilience_checks.py" in script
    assert "LOAD_RESILIENCE_CHECKS=true" in script
    assert "LOAD_PII_MAPPING_TTL_SECONDS=${LOAD_PII_MAPPING_TTL_SECONDS:-15}" in script
    assert "LOAD_MOCK_FAILURE_DELAY_SECONDS=${LOAD_MOCK_FAILURE_DELAY_SECONDS:-60}" in script
    assert "analyzer-metrics-$replica.prom" in script
    assert "guardrail-metrics-$replica.prom" in script
    assert "sample_metrics.py" in script
    assert "summarize_run.py" in script
    assert '--stop-timeout "$LOAD_STOP_TIMEOUT_SECONDS"' in script
    assert 'LOAD_CONTOUR" = "mock-direct' in script
    assert 'LOAD_ANALYZER_BACKEND must be real or mock' in script
    assert '--scale "load-presidio-analyzer=$LOAD_ANALYZER_REPLICAS"' in script
    assert '--scale "load-litellm=$LOAD_LITELLM_REPLICAS"' in script
    assert '--profile load run --rm --no-deps load-key-manager create' in script
    assert 'compose_run_args=(--no-TTY --no-deps)' in script
    assert 'if [ -z "$container_ids" ]' in script
    assert 'LOAD_EXIT_CODE_ON_ERROR=${LOAD_EXIT_CODE_ON_ERROR:-0}' in script
    assert (
        "LOAD_GUARDRAIL_REDIS_MAX_CONNECTIONS="
        "${LOAD_GUARDRAIL_REDIS_MAX_CONNECTIONS:-20}"
    ) in script
    assert "export LOAD_GUARDRAIL_REDIS_MAX_CONNECTIONS" in script
    assert "LOAD_ALLOW_LARGE_CONCURRENT" in (
        ROOT / "tests" / "load" / "load_support.py"
    ).read_text(encoding="utf-8")


def test_profile_defaults_are_resolved_before_common_fallbacks():
    script = (ROOT / "tests" / "load" / "run.sh").read_text(encoding="utf-8")

    assert script.index('LOAD_API=${LOAD_API:-responses}') < script.index(
        'LOAD_API=${LOAD_API:-mixed}'
    )
    assert script.index('LOAD_CONTEXT_MODE=${LOAD_CONTEXT_MODE:-one-shot}') < (
        script.index('LOAD_CONTEXT_MODE=${LOAD_CONTEXT_MODE:-mixed}')
    )
    assert script.index('LOAD_STREAM=${LOAD_STREAM:-false}') < script.index(
        'LOAD_STREAM=${LOAD_STREAM:-mixed}'
    )
    assert script.index(
        'LOAD_MOCK_STREAM_HOLD_SECONDS=${LOAD_MOCK_STREAM_HOLD_SECONDS:-45}'
    ) < script.index(
        'LOAD_MOCK_STREAM_HOLD_SECONDS=${LOAD_MOCK_STREAM_HOLD_SECONDS:-0}'
    )


def test_reports_use_tmp_by_default_and_never_mount_the_project_for_writes():
    generator = _compose()["services"]["load-generator"]

    assert "../..:/workspace:ro" in generator["volumes"]
    assert (
        "${LOAD_RESULTS_DIR:-/tmp/ru-llm-proxy-load-results}:/results"
        in generator["volumes"]
    )


def test_scaled_services_have_configurable_resource_limits():
    services = _compose()["services"]

    assert services["load-presidio-analyzer"]["cpus"] == (
        "${LOAD_ANALYZER_CPUS:-4.0}"
    )
    assert services["load-presidio-analyzer"]["mem_limit"] == (
        "${LOAD_ANALYZER_MEMORY:-4g}"
    )
    assert services["load-litellm"]["cpus"] == "${LOAD_LITELLM_CPUS:-2.0}"
    assert services["load-litellm"]["mem_limit"] == "${LOAD_LITELLM_MEMORY:-2g}"


def test_scaling_matrix_is_explicit_and_uses_unique_synthetic_input():
    script = (ROOT / "tests" / "load" / "run_matrix.sh").read_text(
        encoding="utf-8"
    )

    assert "LOAD_ALLOW_SCALING_MATRIX=true" in script
    assert "LOAD_INPUT_VARIATION=unique" in script
    assert "LOAD_ANALYZER_MATRIX:-1 2 4" in script
    assert "LOAD_ANALYZER_MATRIX_LITELLM_REPLICAS:-2" in script
    assert "LOAD_LITELLM_MATRIX:-1 2 4" in script
    assert "LOAD_ANALYZER_URL=\"$analyzer_url\"" in script
    assert '1 "$replicas" mock' in script
    assert "compare_runs.py" in script
