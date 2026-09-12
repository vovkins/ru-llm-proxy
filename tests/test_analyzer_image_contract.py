"""Static contract for the Analyzer Docker image and dependency boundary."""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PRESIDIO = ROOT / "presidio"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_legacy_ner_runtime_files_are_removed():
    for path in (
        PRESIDIO / "download_model.py",
        PRESIDIO / "requirements-torchcrf.txt",
        PRESIDIO / "ner" / "deeppavlov_recognizer.py",
    ):
        assert not path.exists(), path


def test_active_analyzer_path_has_no_deeppavlov_or_crf_runtime():
    active_paths = [
        ROOT / "docker-compose.yml",
        PRESIDIO / "Dockerfile",
        PRESIDIO / "requirements-analyzer-cpu.txt",
        PRESIDIO / "requirements-analyzer.txt",
        PRESIDIO / "requirements-analyzer-test.txt",
        PRESIDIO / "analyzer_server.py",
        *sorted((PRESIDIO / "ner").glob("*.py")),
    ]
    active_source = "\n".join(_read(path).lower() for path in active_paths)

    for forbidden in (
        "deeppavlov",
        "pytorch-crf",
        "torchcrf",
        "deeppavlov_ner_required",
    ):
        assert forbidden not in active_source


def test_test_dependencies_are_not_installed_in_production_runtime():
    runtime_requirements = _read(PRESIDIO / "requirements-analyzer.txt")
    test_requirements = _read(PRESIDIO / "requirements-analyzer-test.txt")
    dockerfile = _read(PRESIDIO / "Dockerfile")

    assert "pytest" not in runtime_requirements.lower()
    assert "pytest>=8.0,<9.0" in test_requirements
    assert "FROM analyzer-build AS analyzer-test" in dockerfile
    assert "COPY presidio/requirements-analyzer-test.txt ./" in dockerfile
    assert "python -m pip install -r requirements-analyzer-test.txt" in dockerfile


def test_test_image_does_not_include_the_large_ner_artifact():
    dockerfile = _read(PRESIDIO / "Dockerfile")
    test_stage = dockerfile.split(
        "FROM analyzer-build AS analyzer-test",
        maxsplit=1,
    )[1].split("FROM python-base AS analyzer", maxsplit=1)[0]

    assert "download_hf_model.py" not in test_stage
    assert "model-download" not in test_stage
    assert "/opt/models/" not in test_stage


def test_production_image_uses_verified_local_only_model():
    dockerfile = _read(PRESIDIO / "Dockerfile")
    recognizer = _read(PRESIDIO / "ner" / "huggingface_recognizer.py")

    assert "FROM python-base AS model-download" in dockerfile
    assert "FROM python-base AS analyzer" in dockerfile
    assert "python download_hf_model.py" in dockerfile
    assert "COPY --from=model-download" in dockerfile
    assert "COPY --from=analyzer-build /usr/local /usr/local" in dockerfile
    assert "HF_HUB_OFFLINE=1" in dockerfile
    assert "TRANSFORMERS_OFFLINE=1" in dockerfile
    assert recognizer.count("local_files_only=True") == 2
    assert recognizer.count("trust_remote_code=False") == 2
    assert "COPY presidio/text_chunking.py ." in dockerfile


def test_production_image_enforces_cpu_only_runtime():
    cpu_requirements = _read(PRESIDIO / "requirements-analyzer-cpu.txt")
    runtime_requirements = _read(PRESIDIO / "requirements-analyzer.txt")
    dockerfile = _read(PRESIDIO / "Dockerfile")
    verifier = _read(PRESIDIO / "verify_cpu_runtime.py")

    assert "--index-url" not in cpu_requirements
    assert "torch==2.13.0+cpu" in cpu_requirements
    assert "torch>=" not in runtime_requirements
    assert "torch<" not in runtime_requirements
    assert 'ARG PYTORCH_INDEX_URL="https://download.pytorch.org/whl/cpu"' in dockerfile
    assert 'PIP_INDEX_URL="$PYTORCH_INDEX_URL"' in dockerfile
    assert "COPY presidio/requirements-analyzer-cpu.txt ./" in dockerfile
    assert "python -m pip install -r requirements-analyzer-cpu.txt" in dockerfile
    assert "apt-get install -y --no-install-recommends libgomp1" in dockerfile
    assert "COPY presidio/verify_cpu_runtime.py ." in dockerfile
    assert "RUN python verify_cpu_runtime.py" in dockerfile
    assert 'EXPECTED_TORCH_VERSION = "2.13.0+cpu"' in verifier
    assert 'FORBIDDEN_EXECUTABLES = ("curl", "gcc", "g++")' in verifier
    assert 'name.startswith(("nvidia-", "cuda-", "triton"))' in verifier
    assert "torch.version.cuda is not None" in verifier
    assert "torch.cuda.is_available()" in verifier


def test_analyzer_healthchecks_use_python_standard_library():
    dockerfile = _read(PRESIDIO / "Dockerfile")
    compose_files = (
        _read(ROOT / "docker-compose.yml"),
        _read(ROOT / "tests" / "e2e" / "docker-compose.ner-proxy.yml"),
    )

    assert "curl" not in dockerfile
    for compose in compose_files:
        assert "urllib.request.urlopen" in compose
        assert '["CMD", "curl"' not in compose


def test_deeppavlov_references_are_preserved_as_historical_evidence():
    evaluation_readme = _read(PRESIDIO / "evaluation" / "README.md")
    baseline = _read(ROOT / "docs" / "research" / "ner-migration-baseline.md")
    manifest = _read(PRESIDIO / "model_manifest.json")

    assert "неизменяемые исходные показатели старой модели" in evaluation_readme
    assert "не должны перезаписываться" in evaluation_readme
    assert "исторический" in baseline.lower()
    assert '"base_model": "DeepPavlov/rubert-base-cased"' in manifest


def test_docs_describe_test_and_production_image_boundary():
    architecture = _read(ROOT / "docs" / "architecture.md")
    configuration = _read(ROOT / "docs" / "configuration.md")
    provenance = _read(ROOT / "docs" / "research" / "ner-model-provenance.md")

    for document in (architecture, configuration, provenance):
        assert "analyzer-test" in document
        assert "pytest" in document
        assert "вес" in document

    assert "requirements-analyzer-test.txt" in architecture
    assert "--network none" in provenance
    assert "Активного адаптера\nDeepPavlov" in provenance
