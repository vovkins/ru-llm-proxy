"""Static contract for the optional codex-lb Compose overlay."""

import re
import subprocess
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]
OVERLAY_PATH = ROOT / "docker-compose.codex-lb.yml"
CODEX_LB_IMAGE = (
    "ghcr.io/soju06/codex-lb:1.24.0-beta.3@"
    "sha256:d9df6fdef5d900bf96cd6e183b5d2d8abf9387f9cca642e317206ced5362c704"
)
POSTGRES_IMAGE = (
    "postgres:16-alpine@"
    "sha256:cf78e76683b9ca8c5733cbbdce6c9262b45b6767934dd0a95e671f9a0fc20685"
)


def _overlay() -> dict:
    return yaml.safe_load(OVERLAY_PATH.read_text(encoding="utf-8"))


def test_codex_lb_is_an_optional_overlay_not_part_of_base_compose():
    base = yaml.safe_load(
        (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    )
    overlay = _overlay()

    assert "codex-lb" not in base["services"]
    assert set(overlay["services"]) == {
        "nginx",
        "litellm",
        "codex-lb",
        "codex-lb-db",
    }


def test_codex_lb_overlay_connects_litellm_only_with_explicit_credentials():
    litellm = _overlay()["services"]["litellm"]

    assert litellm["volumes"] == [
        "./litellm-config.codex-lb.yaml:/app/config.yaml:ro"
    ]
    assert litellm["environment"] == [
        "CODEX_LB_API_KEY=${CODEX_LB_API_KEY:?Set CODEX_LB_API_KEY}"
    ]
    assert litellm["depends_on"] == {
        "codex-lb": {"condition": "service_healthy"}
    }
    assert litellm["networks"] == ["ru-llm-proxy", "codex-lb-proxy"]


def test_codex_lb_images_are_versioned_and_digest_pinned():
    services = _overlay()["services"]
    dockerfile = (ROOT / "codex-lb" / "Dockerfile").read_text(encoding="utf-8")

    assert services["codex-lb"]["build"] == {
        "context": ".",
        "dockerfile": "codex-lb/Dockerfile",
    }
    assert f"FROM {CODEX_LB_IMAGE}" in dockerfile
    assert services["codex-lb-db"]["image"] == POSTGRES_IMAGE
    assert "image" not in services["codex-lb"]
    assert "build" not in services["codex-lb-db"]


def test_codex_lb_uses_a_dedicated_postgres_service_without_host_port():
    services = _overlay()["services"]
    app = services["codex-lb"]
    database = services["codex-lb-db"]

    assert app["depends_on"] == {
        "codex-lb-db": {"condition": "service_healthy"}
    }
    assert "@codex-lb-db:5432/" in app["environment"]["CODEX_LB_DATABASE_URL"]
    assert database["environment"] == {
        "POSTGRES_USER": "codex_lb",
        "POSTGRES_PASSWORD": (
            "${CODEX_LB_POSTGRES_PASSWORD:?Set CODEX_LB_POSTGRES_PASSWORD}"
        ),
        "POSTGRES_DB": "codex_lb",
    }
    assert "ports" not in database


def test_codex_lb_persists_database_and_encryption_material_separately():
    overlay = _overlay()
    services = overlay["services"]

    assert set(overlay["volumes"]) == {"codex-lb-data", "codex-lb-pgdata"}
    assert services["codex-lb"]["volumes"] == [
        "codex-lb-data:/var/lib/codex-lb"
    ]
    assert services["codex-lb-db"]["volumes"] == [
        "codex-lb-pgdata:/var/lib/postgresql/data"
    ]
    assert services["codex-lb"]["environment"]["CODEX_LB_DATA_DIR"] == (
        "/var/lib/codex-lb"
    )


def test_codex_lb_overlay_contains_no_literal_credentials():
    overlay = _overlay()
    services = overlay["services"]

    assert "${CODEX_LB_POSTGRES_PASSWORD:?" in services["codex-lb"][
        "environment"
    ]["CODEX_LB_DATABASE_URL"]
    assert services["codex-lb-db"]["environment"]["POSTGRES_PASSWORD"].startswith(
        "${CODEX_LB_POSTGRES_PASSWORD:?"
    )


def test_existing_nginx_publishes_dashboard_while_codex_lb_publishes_metrics():
    services = _overlay()["services"]
    app = services["codex-lb"]
    nginx = services["nginx"]

    assert nginx["ports"] == ["${CODEX_LB_PORT:-2455}:2455"]
    assert nginx["depends_on"] == {
        "codex-lb": {"condition": "service_healthy"}
    }
    assert app["ports"] == ["${CODEX_LB_METRICS_PORT:-9090}:9090"]
    assert app["environment"]["CODEX_LB_METRICS_ENABLED"] == "true"
    assert app["environment"]["CODEX_LB_DASHBOARD_AUTH_MODE"] == "standard"
    assert app["environment"]["CODEX_LB_TELEMETRY_ENABLED"] == "false"
    assert app["environment"]["CODEX_LB_HTTP_RESPONSES_SESSION_BRIDGE_ENABLED"] == (
        "${CODEX_LB_HTTP_RESPONSES_SESSION_BRIDGE_ENABLED:-true}"
    )
    assert app["environment"]["CODEX_LB_UPSTREAM_STREAM_TRANSPORT"] == (
        "${CODEX_LB_UPSTREAM_STREAM_TRANSPORT:-auto}"
    )
    assert "1455" not in OVERLAY_PATH.read_text(encoding="utf-8")
    assert "BIND_ADDRESS" not in OVERLAY_PATH.read_text(encoding="utf-8")


def test_nginx_routes_codex_lb_dashboard_without_a_path_prefix():
    nginx = (ROOT / "nginx" / "conf.d" / "default.conf").read_text(
        encoding="utf-8"
    )

    assert "listen 80;" in nginx
    assert "http://litellm:4000" in nginx
    assert "listen 2455;" in nginx
    assert "http://codex-lb:2455" in nginx
    assert "proxy_set_header Upgrade" in nginx
    assert "$connection_upgrade" in nginx
    assert "location /codex-lb" not in nginx


def test_codex_lb_uses_standard_corporate_proxy_environment():
    environment = _overlay()["services"]["codex-lb"]["environment"]

    assert environment["HTTP_PROXY"] == "${HTTP_PROXY:-}"
    assert environment["HTTPS_PROXY"] == "${HTTPS_PROXY:-}"
    assert environment["http_proxy"] == "${HTTP_PROXY:-}"
    assert environment["https_proxy"] == "${HTTPS_PROXY:-}"
    for name in ("NO_PROXY", "no_proxy"):
        assert "codex-lb" in environment[name]
        assert "codex-lb-db" in environment[name]


def test_codex_lb_image_uses_the_same_corporate_ca_contract_as_litellm():
    dockerfile = (ROOT / "codex-lb" / "Dockerfile").read_text(encoding="utf-8")

    assert "USER root" in dockerfile
    assert "COPY certs/" in dockerfile
    assert "openssl crl2pkcs7" in dockerfile
    assert "/opt/venv/bin/python -c 'import certifi" in dockerfile
    assert "SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt" in dockerfile
    assert dockerfile.rstrip().endswith("USER app")


def test_codex_lb_database_network_is_internal_and_proxy_network_isolated():
    overlay = _overlay()
    services = overlay["services"]

    assert overlay["networks"] == {
        "codex-lb-proxy": {"driver": "bridge"},
        "codex-lb-database": {"driver": "bridge", "internal": True},
    }
    assert services["litellm"]["networks"] == [
        "ru-llm-proxy",
        "codex-lb-proxy",
    ]
    assert services["nginx"]["networks"] == [
        "ru-llm-proxy",
        "codex-lb-proxy",
    ]
    assert services["codex-lb"]["networks"] == [
        "codex-lb-proxy",
        "codex-lb-database",
    ]
    assert services["codex-lb-db"]["networks"] == ["codex-lb-database"]


def test_codex_lb_and_postgres_have_readiness_checks():
    services = _overlay()["services"]
    app_health = services["codex-lb"]["healthcheck"]
    database_health = services["codex-lb-db"]["healthcheck"]

    assert app_health["test"][:3] == ["CMD", "python", "-c"]
    assert "/health/ready" in app_health["test"][3]
    assert app_health["start_period"] == "30s"
    assert database_health["test"][0] == "CMD-SHELL"
    assert "pg_isready" in database_health["test"][1]
    assert database_health["start_period"] == "10s"


def test_codex_lb_operator_environment_is_minimal_and_documented():
    env_example = (ROOT / ".env.example").read_text(encoding="utf-8")
    configuration = (ROOT / "docs" / "configuration.md").read_text(
        encoding="utf-8"
    )

    assert "# === Экспериментальный пул ChatGPT OAuth-подписок codex-lb ===" in (
        env_example
    )
    assert "CODEX_LB_POSTGRES_PASSWORD=***" in env_example
    assert "CODEX_LB_API_KEY=***" in env_example
    assert "CODEX_LB_HTTP_RESPONSES_SESSION_BRIDGE_ENABLED=true" in env_example
    assert "CODEX_LB_UPSTREAM_STREAM_TRANSPORT=auto" in env_example
    assert "CODEX_LB_PORT=" not in env_example
    assert "CODEX_LB_METRICS_PORT=" not in env_example

    for name in (
        "CODEX_LB_POSTGRES_PASSWORD",
        "CODEX_LB_API_KEY",
        "CODEX_LB_PORT",
        "CODEX_LB_METRICS_PORT",
        "CODEX_LB_DATA_DIR",
        "CODEX_LB_DATABASE_URL",
        "CODEX_LB_DASHBOARD_AUTH_MODE",
        "CODEX_LB_METRICS_ENABLED",
        "CODEX_LB_TELEMETRY_ENABLED",
        "CODEX_LB_HTTP_RESPONSES_SESSION_BRIDGE_ENABLED",
        "CODEX_LB_UPSTREAM_STREAM_TRANSPORT",
    ):
        assert f"`{name}`" in configuration


def test_setup_env_generates_codex_lb_db_password_but_not_service_key(tmp_path):
    env_file = tmp_path / ".env"
    example_file = tmp_path / ".env.example"
    example_file.write_text(
        "CODEX_LB_POSTGRES_PASSWORD=***\nCODEX_LB_API_KEY=***\n",
        encoding="utf-8",
    )

    command = [
        "bash",
        str(ROOT / "scripts" / "setup_env.sh"),
        str(env_file),
        str(example_file),
        "litellm-presidio-codex-lb",
    ]
    subprocess.run(command, cwd=ROOT, check=True, capture_output=True, text=True)
    first_values = dict(
        line.split("=", 1)
        for line in env_file.read_text(encoding="utf-8").splitlines()
        if "=" in line
    )

    password = first_values["CODEX_LB_POSTGRES_PASSWORD"]
    assert password != "***"
    assert len(password) >= 40
    assert re.fullmatch(r"[A-Za-z0-9_-]+", password)
    assert first_values["CODEX_LB_API_KEY"] == "***"

    subprocess.run(command, cwd=ROOT, check=True, capture_output=True, text=True)
    second_values = dict(
        line.split("=", 1)
        for line in env_file.read_text(encoding="utf-8").splitlines()
        if "=" in line
    )
    assert second_values["CODEX_LB_POSTGRES_PASSWORD"] == password
    assert second_values["CODEX_LB_API_KEY"] == "***"


def test_nginx_has_an_upstream_independent_bootstrap_healthcheck():
    base = yaml.safe_load(
        (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    )
    nginx = base["services"]["nginx"]
    config = (ROOT / "nginx" / "conf.d" / "default.conf").read_text(
        encoding="utf-8"
    )

    assert "depends_on" not in nginx
    assert nginx["healthcheck"]["test"][-1].endswith("/nginx-health")
    assert "location = /nginx-health" in config


def test_codex_lb_runbook_covers_the_operational_contract():
    runbook = (ROOT / "docs" / "codex-lb.md").read_text(encoding="utf-8")

    for required in (
        "docker-compose.codex-lb.yml",
        "API key authentication",
        "codex-lb-db",
        "codex-lb-data",
        "CODEX_LB_TELEMETRY_ENABLED=false",
        "CODEX_LB_HTTP_RESPONSES_SESSION_BRIDGE_ENABLED=false",
        "CODEX_LB_UPSTREAM_STREAM_TRANSPORT=http",
        "chatgpt.com:443",
        "auth.openai.com:443",
        "Порт обратного вызова `1455`",
        "gpt-5.6-luna",
        "encrypted_content",
    ):
        assert required in runbook


def test_primary_docs_link_to_codex_lb_runbook():
    paths = (
        "README.md",
        "docs/README.md",
        "docs/admin-access.md",
        "docs/architecture.md",
        "docs/clients/codex.md",
        "docs/compliance.md",
        "docs/configuration.md",
        "docs/egress-controls.md",
        "docs/examples.md",
        "docs/monitoring.md",
        "docs/routing.md",
    )

    for path in paths:
        assert "codex-lb.md" in (ROOT / path).read_text(encoding="utf-8"), path


def test_codex_lb_runbook_local_links_resolve():
    runbook_path = ROOT / "docs" / "codex-lb.md"
    runbook = runbook_path.read_text(encoding="utf-8")

    for target in re.findall(r"\[[^]]+\]\(([^)]+)\)", runbook):
        if target.startswith(("http://", "https://", "#")):
            continue
        local_target = target.split("#", 1)[0]
        assert (runbook_path.parent / local_target).exists(), target
