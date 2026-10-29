"""Standard-library probe of each Triton replica against its built artifact."""
import argparse
import json
from pathlib import Path
import urllib.request

try:
    from .model_artifact import load_manifest
    from .model_contract import MODEL_ID, MODEL_REVISION, validate_triton_config
    from .onnx_artifact import load_export_manifest
except ImportError:
    from model_artifact import load_manifest
    from model_contract import MODEL_ID, MODEL_REVISION, validate_triton_config
    from onnx_artifact import load_export_manifest

MODEL_DIRECTORY = Path("/models/tiny2")


def trusted_identity(directory=MODEL_DIRECTORY):
    source = load_manifest(Path(__file__).with_name("model_manifest.json"))
    artifact = load_export_manifest(directory / "export-manifest.json", source)
    return {"model_id": MODEL_ID, "revision": MODEL_REVISION,
            "onnx_sha256": artifact["onnx_sha256"]}, artifact


def check_service(directory=MODEL_DIRECTORY, url="http://127.0.0.1:8000"):
    identity, artifact = trusted_identity(directory)
    # No corporate proxy for loopback, no redirects, no unbounded error body.
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *_args, **_kwargs):
            return None
    client = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    path = url + "/v2/models/tiny2/versions/1"
    for suffix, limit in (("/ready", 4096), ("/config", 262144)):
        with client.open(path + suffix, timeout=3) as response:
            body = response.read(limit + 1)
            if response.status != 200 or len(body) > limit:
                raise ValueError("Triton probe response is invalid")
        if suffix == "/config":
            expected = {key: artifact[key] for key in (
                "source_model_sha256", "tokenizer_sha256", "label_contract_sha256", "onnx_sha256")}
            expected.update(precision="fp32", model_profile="tiny2")
            validate_triton_config(json.loads(body), expected)
    return identity


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--identity-only", action="store_true")
    args = parser.parse_args()
    try:
        result = trusted_identity()[0] if args.identity_only else check_service()
    except Exception:
        raise SystemExit("Triton replica is unavailable or does not match its trusted artifact") from None
    print(json.dumps(result, sort_keys=True))
