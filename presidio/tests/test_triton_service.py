"""Per-replica probes share the same trusted contract as the runtime client."""

import io
import json

import pytest

from presidio import verify_triton_service as probe
from presidio.model_contract import INPUT_NAMES, validate_triton_config
from presidio.tests.test_onnx_artifact import fixture_metadata


def service_config(artifact):
    expected = {key: artifact[key] for key in (
        "source_model_sha256", "tokenizer_sha256", "label_contract_sha256", "onnx_sha256")}
    expected.update(precision="fp32", model_profile="tiny2")
    return {
        "parameters": {key: {"string_value": value} for key, value in expected.items()},
        "backend": "onnxruntime", "max_batch_size": 32,
        "input": [{"name": name, "data_type": "TYPE_INT64", "dims": [386]} for name in INPUT_NAMES],
        "output": [{"name": "logits", "data_type": "TYPE_FP32", "dims": [386, 17]}],
        "instance_group": [{"kind": "KIND_CPU"}],
    }, expected


def test_probe_reads_local_build_metadata_and_checks_serving_identity(tmp_path, monkeypatch):
    _, artifact = fixture_metadata()
    (tmp_path / "export-manifest.json").write_text(json.dumps(artifact))
    expected, _ = probe.trusted_identity(tmp_path)
    config, _ = service_config(artifact)
    calls = []
    class Response(io.BytesIO):
        status = 200
    class Client:
        def open(self, path, timeout):
            calls.append((path, timeout))
            return Response(json.dumps(config).encode() if path.endswith("/config") else b"")
    monkeypatch.setattr(probe.urllib.request, "build_opener", lambda *_: Client())
    assert probe.check_service(tmp_path) == expected
    assert len(calls) == 2 and all(timeout == 3 for _, timeout in calls)
    config["parameters"]["onnx_sha256"]["string_value"] = "b" * 64
    with pytest.raises(ValueError, match="identity"):
        probe.check_service(tmp_path)


@pytest.mark.parametrize("field,value", [("input", [None]), ("output", {}),
                                         ("instance_group", ["cpu"]), ("parameters", [])])
def test_malformed_serving_contract_is_rejected_explicitly(field, value):
    _, artifact = fixture_metadata()
    config, expected = service_config(artifact)
    config[field] = value
    with pytest.raises(ValueError):
        validate_triton_config(config, expected)


def test_probe_refuses_oversized_readiness_body(tmp_path, monkeypatch):
    _, artifact = fixture_metadata()
    (tmp_path / "export-manifest.json").write_text(json.dumps(artifact))
    class Response(io.BytesIO):
        status = 200
    class Client:
        def open(self, *_args, **_kwargs):
            return Response(b"x" * 4097)
    monkeypatch.setattr(probe.urllib.request, "build_opener", lambda *_: Client())
    with pytest.raises(ValueError, match="invalid"):
        probe.check_service(tmp_path)
