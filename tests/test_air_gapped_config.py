"""Static contracts for the isolated corporate environment branch."""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _read(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def test_proxy_ca_is_local_and_used_by_all_built_images():
    gitignore = _read(".gitignore")
    analyzer = _read("presidio/Dockerfile")
    litellm = _read("litellm/Dockerfile")
    guardrail_tests = _read("tests/Dockerfile.guardrails")

    assert "certs/*.crt" in gitignore
    assert (ROOT / "certs" / "README.md").is_file()
    for dockerfile in (analyzer, litellm, guardrail_tests):
        assert "COPY certs/" in dockerfile
        assert "proxy-certs" in dockerfile
        assert "SSL_VERIFY=False" not in dockerfile


def test_compose_routes_only_litellm_runtime_egress_through_proxy():
    compose = _read("docker-compose.yml")

    assert "nginx:" in compose
    assert '"${NGINX_HTTP_PORT:-80}:80"' in compose
    assert "dockerfile: litellm/Dockerfile" in compose
    assert "DISABLE_AIOHTTP_TRANSPORT=True" in compose
    assert "AIOHTTP_TRUST_ENV=True" in compose
    assert "NO_PROXY=${NO_PROXY:-localhost,127.0.0.1,db,redis,presidio-analyzer,litellm,nginx}" in compose
    assert "dockerfile: presidio/Dockerfile" in compose
    assert "NER_MODEL_PROXY: ${NER_MODEL_PROXY:-}" in compose

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
