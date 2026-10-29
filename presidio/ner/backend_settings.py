"""Connection settings for the only supported NER execution path."""
from dataclasses import dataclass
import math
import os
try:
    from model_contract import TRITON_URL, TRITON_MODEL_NAME, TRITON_MODEL_VERSION, reject_removed_settings
except ImportError:
    from presidio.model_contract import TRITON_URL, TRITON_MODEL_NAME, TRITON_MODEL_VERSION, reject_removed_settings


@dataclass(frozen=True)
class BackendSettings:
    backend: str = 'triton'
    url: str = TRITON_URL
    model_name: str = TRITON_MODEL_NAME
    model_version: str = TRITON_MODEL_VERSION
    timeout: float = 60.


def resolve_backend(environ=None):
    env = os.environ if environ is None else environ
    reject_removed_settings(env)
    try:
        timeout = float(env.get('PRESIDIO_ANALYZER_TRITON_TIMEOUT_SECONDS', '60'))
    except ValueError as exc:
        raise ValueError('Invalid Triton timeout') from exc
    if not math.isfinite(timeout) or not 0 < timeout <= 600:
        raise ValueError('Triton timeout must be finite and between 0 and 600 seconds')
    return BackendSettings(timeout=timeout)
