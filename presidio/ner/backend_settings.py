"""Explicit NER execution backend; Transformers remains the default."""
from dataclasses import dataclass
import math
import os
import re
from urllib.parse import urlsplit


@dataclass(frozen=True)
class BackendSettings:
    backend: str = 'transformers'
    url: str = 'http://ner-triton:8000'
    model_name: str = 'tiny2'
    model_version: str = '1'
    timeout: float = 60.


def resolve_backend(environ=None):
    env = os.environ if environ is None else environ
    name = env.get('PRESIDIO_ANALYZER_NER_BACKEND', 'transformers').strip().lower()
    if name not in {'transformers', 'triton'}:
        raise ValueError('NER backend must be transformers or triton')
    if name == 'transformers':
        return BackendSettings()
    url = env.get('PRESIDIO_ANALYZER_TRITON_URL', 'http://ner-triton:8000').strip().rstrip('/')
    parsed = urlsplit(url)
    if parsed.scheme not in {'http', 'https'} or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError('Triton URL must be HTTP(S) without credentials, query or fragment')
    model = env.get('PRESIDIO_ANALYZER_TRITON_MODEL_NAME', 'tiny2').strip()
    version = env.get('PRESIDIO_ANALYZER_TRITON_MODEL_VERSION', '1').strip()
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', model) or not re.fullmatch(r'[1-9][0-9]{0,8}', version):
        raise ValueError('Invalid Triton model name or version')
    try:
        timeout = float(env.get('PRESIDIO_ANALYZER_TRITON_TIMEOUT_SECONDS', '60'))
    except ValueError as exc:
        raise ValueError('Invalid Triton timeout') from exc
    if not math.isfinite(timeout) or not 0 < timeout <= 600:
        raise ValueError('Triton timeout must be finite and between 0 and 600 seconds')
    return BackendSettings(name, url, model, version, timeout)
