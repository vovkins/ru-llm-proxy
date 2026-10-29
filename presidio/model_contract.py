"""Fixed source identity and tensor contract shared by export and serving."""

MODEL_ID = "Katerina-Bir/rubert-tiny2-secret-detection"
MODEL_REVISION = "4137142e79d52b0b69370e0e3dceb2d6ad82591d"
MODEL_VARIANT = "tiny2-mix-a-s1-ft-spacy-free-20261013-a-e2-b2"
MODEL_ARCHITECTURE = "BertForTokenClassification"
TRITON_URL = "http://nginx:8000"
TRITON_MODEL_NAME = "tiny2"
TRITON_MODEL_VERSION = "1"

# These are rejected, not interpreted as compatibility switches. Only names
# enter diagnostics: values may come from an administrator's secret env file.
REMOVED_SETTINGS = frozenset({
    "ANALYZER_PROFILE", "LOAD_ANALYZER_PROFILE", "NER_MODEL_PROFILE",
    "PRESIDIO_ANALYZER_NER_MODEL_PROFILE", "PRESIDIO_ANALYZER_NER_BACKEND",
    "PRESIDIO_ANALYZER_DEVICE_PROFILE", "PRESIDIO_ANALYZER_GPU_PRECISION",
    "PYTORCH_GPU_INDEX_URL", "TINY2_ANALYZER_WORKERS", "TINY2_ANALYZER_CONCURRENCY_LIMIT",
    "TINY2_ANALYZER_QUEUE_LIMIT", "TINY2_ANALYZER_QUEUE_TIMEOUT_SECONDS",
    "TINY2_NER_BOUNDARY_RECHECK", "PRESIDIO_ANALYZER_NER_O_LOGIT_BIAS",
    "PRESIDIO_ANALYZER_NER_SPAN_POSTPROCESSING", "PRESIDIO_ANALYZER_NER_BOUNDARY_RECHECK",
    "PRESIDIO_ANALYZER_TRITON_URL", "PRESIDIO_ANALYZER_TRITON_MODEL_NAME",
    "PRESIDIO_ANALYZER_TRITON_MODEL_VERSION", "NER_EVALUATION_MODEL_SHA256",
    "NER_EVALUATION_SYSTEM",
})


def reject_removed_settings(environ):
    removed = sorted(REMOVED_SETTINGS.intersection(environ))
    if removed:
        raise ValueError("Remove obsolete NER settings: " + ", ".join(removed))


SEQUENCE_LENGTH = 386
MAX_BATCH_SIZE = 32
ONNX_OPSET = 17
INPUT_NAMES = ("input_ids", "attention_mask", "token_type_ids")
EXPECTED_ID2LABEL = {
    0: "O", 1: "B-PERSON", 2: "I-PERSON",
    3: "B-LOCATION", 4: "I-LOCATION",
    5: "B-ORGANIZATION", 6: "I-ORGANIZATION",
    7: "B-LOGIN", 8: "I-LOGIN", 9: "B-PASSWORD", 10: "I-PASSWORD",
    11: "B-AUTH_TOKEN", 12: "I-AUTH_TOKEN",
    13: "B-SECRET_KEY", 14: "I-SECRET_KEY",
    15: "B-CONTRACT_NUMBER", 16: "I-CONTRACT_NUMBER",
}


def validate_triton_config(config, expected):
    """Validate serving identity/geometry using only the standard library."""
    if not isinstance(config, dict) or not isinstance(config.get("parameters"), dict):
        raise ValueError("Triton configuration must contain identity parameters")
    parameters = config.get("parameters", {})
    actual = {key: value.get("string_value") for key, value in parameters.items()
              if isinstance(value, dict)}
    if any(actual.get(key) != value for key, value in expected.items()):
        raise ValueError("Triton identity does not match the trusted artifact")
    inputs = config.get("input", [])
    outputs = config.get("output", [])
    instances = config.get("instance_group", [])
    if any(not isinstance(items, list) or any(not isinstance(item, dict) for item in items)
           for items in (inputs, outputs, instances)):
        raise ValueError("Triton configuration arrays are invalid")
    if (config.get("backend") != "onnxruntime" or len(inputs) != 3 or
        {item.get("name") for item in inputs} != set(INPUT_NAMES) or
        any(item.get("data_type") != "TYPE_INT64" or item.get("dims") != [SEQUENCE_LENGTH] for item in inputs) or
        len(outputs) != 1 or outputs[0].get("name") != "logits" or
        outputs[0].get("data_type") != "TYPE_FP32" or outputs[0].get("dims") != [SEQUENCE_LENGTH, len(EXPECTED_ID2LABEL)]):
        raise ValueError("Triton tensor contract is invalid")
    if not instances or any(group.get("kind") != "KIND_CPU" for group in instances):
        raise ValueError("Triton requires CPU instances")
    maximum = config.get("max_batch_size")
    if type(maximum) is not int or not 1 <= maximum <= MAX_BATCH_SIZE:
        raise ValueError("Triton maximum batch size is invalid")
    return maximum
