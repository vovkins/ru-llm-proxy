"""Static contracts for the isolated corporate environment branch."""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _read(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def test_proxy_ca_is_local_and_used_by_all_built_images():
    dockerignore = _read(".dockerignore")
    gitignore = _read(".gitignore")
    analyzer = _read("presidio/Dockerfile")
    litellm = _read("litellm/Dockerfile")
    codex_lb = _read("codex-lb/Dockerfile")
    guardrail_tests = _read("tests/Dockerfile.guardrails")

    ignore_patterns = {
        line.strip()
        for line in dockerignore.splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    }
    assert not any(pattern.startswith("certs/") for pattern in ignore_patterns)
    assert "certs/*.crt" in gitignore
    assert (ROOT / "certs" / "README.md").is_file()
    for dockerfile in (analyzer, litellm, codex_lb, guardrail_tests):
        assert "COPY certs/" in dockerfile
        assert "proxy-certs" in dockerfile
        assert "/tmp/proxy-ca.pem" in dockerfile
        assert "SSL_VERIFY=False" not in dockerfile


def test_litellm_validates_proxy_ca_and_configures_all_trust_bundles():
    dockerfile = _read("litellm/Dockerfile")

    assert "-exec awk 1 {} +" in dockerfile
    assert "openssl crl2pkcs7" in dockerfile
    assert "openssl pkcs7 -print_certs" in dockerfile
    assert "SYSTEM_BUNDLE=/etc/ssl/certs/ca-certificates.crt" in dockerfile
    assert "import certifi; print(certifi.where())" in dockerfile
    assert 'if [ "$CERTIFI_BUNDLE" != "$SYSTEM_BUNDLE" ]' in dockerfile
    for variable in (
        "SSL_CERT_FILE",
        "REQUESTS_CA_BUNDLE",
        "CURL_CA_BUNDLE",
        "NODE_EXTRA_CA_CERTS",
    ):
        assert f"{variable}=/etc/ssl/certs/ca-certificates.crt" in dockerfile


def test_python_images_validate_proxy_ca_before_using_it():
    for path in ("presidio/Dockerfile", "tests/Dockerfile.guardrails"):
        dockerfile = _read(path)

        assert "rstrip(b'\\\\r\\\\n') + b'\\\\n'" in dockerfile
        assert "ssl.create_default_context(cafile='/tmp/proxy-ca.pem')" in dockerfile
        assert (
            "ssl.create_default_context(cafile='/etc/ssl/certs/ca-certificates.crt')"
            in dockerfile
        )


def test_compose_routes_external_runtime_egress_through_proxy():
    compose = _read("docker-compose.yml")
    codex_overlay = _read("docker-compose.codex-lb.yml")

    assert "nginx:" in compose
    assert '"${NGINX_HTTP_PORT:-80}:80"' in compose
    assert "dockerfile: litellm/Dockerfile" in compose
    assert "DISABLE_AIOHTTP_TRANSPORT=True" in compose
    assert "AIOHTTP_TRUST_ENV=True" in compose
    assert "NO_PROXY=${NO_PROXY:-localhost,127.0.0.1,db,redis,presidio-analyzer,litellm,nginx,codex-lb,codex-lb-db}" in compose
    assert "dockerfile: presidio/Dockerfile" in compose
    assert "NER_MODEL_PROXY: ${NER_MODEL_PROXY:-}" in compose
    assert "HTTP_PROXY: ${HTTP_PROXY:-}" in codex_overlay
    assert "HTTPS_PROXY: ${HTTPS_PROXY:-}" in codex_overlay
    assert "dockerfile: codex-lb/Dockerfile" in codex_overlay

    analyzer_environment = compose.split("presidio-analyzer:", maxsplit=1)[1].split(
        "# === PostgreSQL", maxsplit=1
    )[0].split("environment:", maxsplit=1)[1]
    assert "HTTP_PROXY=" not in analyzer_environment
    assert "HTTPS_PROXY=" not in analyzer_environment


def test_analyzer_keeps_verified_offline_cpu_model_contract():
    dockerfile = _read("presidio/Dockerfile")
    cpu_requirements = _read("presidio/requirements-analyzer-cpu.txt")

    assert "DEEPPAVLOV" not in dockerfile.upper()
    assert "python download_hf_model.py" in dockerfile
    assert "COPY --from=model-download" in dockerfile
    assert "HF_HUB_OFFLINE=1" in dockerfile
    assert "TRANSFORMERS_OFFLINE=1" in dockerfile
    assert "torch==2.13.0+cpu" in cpu_requirements
    assert "--index-url" not in cpu_requirements
    assert "PYTORCH_INDEX_URL" in dockerfile


def test_build_context_and_internal_package_sources_are_consistent():
    compose = _read("docker-compose.yml")
    workflow = _read(".github/workflows/ner-model-integration.yml")

    assert "context: ./presidio" not in compose
    assert "context: ./presidio" not in workflow
    assert "PIP_INDEX_URL" in compose
    assert "PIP_TRUSTED_HOST" in compose
    assert "PIP_PROXY" in compose
    assert "PYTORCH_INDEX_URL" in compose


def test_air_gapped_variables_and_branch_boundary_are_documented():
    env_example = _read(".env.example")
    configuration = _read("docs/configuration.md")
    guide = _read("docs/air-gapped.md")

    variables = (
        "NGINX_HTTP_PORT",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "NO_PROXY",
        "PIP_INDEX_URL",
        "PIP_TRUSTED_HOST",
        "PIP_PROXY",
        "PYTORCH_INDEX_URL",
        "NER_MODEL_PROXY",
    )
    for variable in variables:
        assert f"{variable}=" in env_example
        assert f"`{variable}`" in configuration

    assert "не предназначены для слияния в `main`" in guide
    assert "Analyzer использует только модель внутри образа" in guide
