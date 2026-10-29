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
        PRESIDIO / "requirements-export-cpu.txt",
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
    assert "FROM analyzer-common-build AS analyzer-test" in dockerfile
    assert "COPY presidio/requirements-analyzer-test.txt ./" in dockerfile
    assert "python -m pip install -r requirements-analyzer-test.txt" in dockerfile


def test_spacy_map_dependency_is_pinned_to_the_loadable_version():
    requirements = _read(PRESIDIO / "requirements-analyzer.txt")
    assert "preshed==3.0.13" in requirements


def test_test_image_does_not_include_the_large_ner_artifact():
    dockerfile = _read(PRESIDIO / "Dockerfile")
    test_stage = dockerfile.split(
        "FROM analyzer-common-build AS analyzer-test",
        maxsplit=1,
    )[1].split("FROM model-download AS runtime-artifact", maxsplit=1)[0]

    assert "download_hf_model.py" not in test_stage
    assert "model-download" not in test_stage
    assert "/opt/models/" not in test_stage


def test_production_image_uses_verified_tokenizer_only():
    dockerfile = _read(PRESIDIO / "Dockerfile")
    recognizer = _read(PRESIDIO / "ner/triton_recognizer.py")
    assert "FROM python-base AS model-download" in dockerfile
    assert "python download_hf_model.py" in dockerfile
    assert "COPY --from=runtime-artifact /runtime-models /opt/models" in dockerfile
    assert "COPY --from=analyzer-common-build /usr/local /usr/local" in dockerfile
    assert "load_and_verify_runtime_artifact" in recognizer
    assert "ContentTokenizer(self.model_directory)" in recognizer
    assert "AutoModel" not in recognizer and "import torch" not in recognizer
    assert "COPY presidio/text_chunking.py ." in dockerfile
    assert "COPY presidio/string_spans.py ." in dockerfile


def test_production_image_has_no_execution_stack():
    requirements = _read(PRESIDIO / "requirements-analyzer.txt")
    verifier = _read(PRESIDIO / "verify_cpu_runtime.py")
    assert "tokenizers==0.22.2" in requirements
    assert "transformers==" not in requirements and "torch==" not in requirements
    assert '"torch", "transformers", "onnx", "onnxruntime", "safetensors"' in verifier
    assert "find_spec(name)" in verifier
    assert 'FORBIDDEN_EXECUTABLES = ("curl", "gcc", "g++")' in verifier
    assert 'rglob("*.safetensors")' in verifier
    assert "RUN python verify_cpu_runtime.py" in _read(PRESIDIO / "Dockerfile")
    assert "torch==2.13.0+cpu" in _read(PRESIDIO / "requirements-export-cpu.txt")


def test_old_gpu_and_local_execution_paths_are_absent():
    dockerfile = _read(PRESIDIO / "Dockerfile")
    assert " AS analyzer-gpu" not in dockerfile and "PYTORCH_GPU" not in dockerfile
    assert not (PRESIDIO / "requirements-analyzer-gpu.txt").exists()
    assert not (PRESIDIO / "verify_gpu_runtime.py").exists()
    assert not (PRESIDIO / "ner/huggingface_recognizer.py").exists()
    assert "nvcr.io/nvidia/tritonserver:26.08-py3@sha256:9185ba5b33d2c85c9204b208957b4bbfc6b2b89066efc9192e0f60c2ab58cec0" in dockerfile


def test_makefile_builds_the_single_cpu_stack_without_model_or_gpu_switches():
    makefile = _read(ROOT / "Makefile")

    assert "ANALYZER_PROFILE" not in makefile
    assert "docker-compose.gpu.yml" not in makefile
    assert "--gpus all" not in makefile
    assert "presidio-analyzer ner-triton" in makefile
    assert "tests/e2e/ner_integration.py" in makefile
    runner = _read(ROOT / "tests/e2e/ner_integration.py")
    assert "from model_smoke import run as model_smoke" in runner


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
    assert '"base_model": "cointegrated/rubert-tiny2"' in manifest
    assert not (PRESIDIO / "model_manifest.tiny2.json").exists()


def test_export_has_one_automatic_source_and_build_produced_trust_anchor():
    dockerfile = _read(PRESIDIO / "Dockerfile")
    assert dockerfile.index("FROM triton-export-build AS triton-model-export") < dockerfile.index("FROM analyzer-runtime AS analyzer")
    export_stage = dockerfile.split("FROM triton-export-build AS triton-model-export")[1].split("FROM nvcr.io/")[0]
    assert "presidio/string_spans.py" in export_stage
    assert "presidio/model_contract.py" in export_stage
    assert "presidio/onnx_artifact.py" in export_stage
    assert "ARG NER_MODEL_PROFILE" not in dockerfile
    assert "model-artifacts" not in dockerfile
    assert "COPY --from=triton-model-export /export/models/tiny2/export-manifest.json /opt/ner-export/export-manifest.json" in dockerfile
    assert not (PRESIDIO / "model-artifacts/tiny2/README.md").exists()


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
