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
        "load-presidio-analyzer",
        "load-litellm",
        "load-key-manager",
        "load-generator",
    }
    assert services["load-db"]["image"].startswith("postgres:16-alpine@sha256:")
    assert services["load-redis"]["image"] == "redis:7-alpine"
    assert services["load-presidio-analyzer"]["build"]["target"] == "analyzer"
    assert services["load-litellm"]["depends_on"]["load-db"]["condition"] == (
        "service_healthy"
    )


def test_load_contour_cannot_send_requests_to_a_real_provider():
    compose_text = COMPOSE_PATH.read_text(encoding="utf-8")
    config = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8"))

    assert "api.openai.com" not in compose_text
    assert "api.z.ai" not in compose_text
    assert config["model_list"][0]["litellm_params"]["api_base"] == (
        "http://load-mock-upstream:8080/v1"
    )
    assert config["model_list"][0]["model_name"] == "mock-chat"


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
    assert '"${COMPOSE[@]}" --profile load down -v --remove-orphans' in script
    assert "/Applications/Docker.app/Contents/Resources/bin" in script
    assert 'export PATH="$DOCKER_DESKTOP_BIN:$PATH"' in script
    assert "mock-provider-capture.json" in script
    assert "analyzer-metrics.prom" in script
    assert 'if [ -z "$container_ids" ]' in script
    assert 'LOAD_EXIT_CODE_ON_ERROR=${LOAD_EXIT_CODE_ON_ERROR:-0}' in script
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
