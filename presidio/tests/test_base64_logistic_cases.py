"""Synthetic analogues of reviewed cases (security-team document, Codex datasets).

No real credentials are stored here. Checks run through find_spans, the function
used by Analyzer; masking itself is performed by the Gateway.
"""
import base64
import random
import string
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from recognizers.base64_logistic import find_spans

R = random.Random(1)
b64 = lambda b: base64.b64encode(b).decode()
rnd = lambda n: bytes(R.randrange(256) for _ in range(n))
rid = lambda n: "".join(R.choice(string.ascii_letters + string.digits) for _ in range(n))


def covered(text, value):
    start = text.index(value)
    cursor = start
    for a, b in find_spans(text):
        if a <= cursor < b:
            cursor = b
    return cursor >= start + len(value)


PEM_BLOCK = "-----BEGIN PRIVATE KEY-----\n" + "\n".join(b64(rnd(48)) for _ in range(5)) + "\n-----END PRIVATE KEY-----"
DER = "MIIEpAIBAAKCAQEA" + b64(rnd(24))
# name -> (text, value that must be fully covered)
MUST_MASK = {name: (pre + payload + post, payload) for name, (pre, payload, post) in {
    "логин:пароль в Basic auth":           ("Authorization: Basic ", b64(b"svc_user:Str0ng_P@ss_2026!"), ""),
    "пароль в base64 в JSON":              ('{"credentials": "', b64(b"app_user:S3cure_Test_2026!"), '"}'),
    "короткий пароль с паддингом (8-11)":  ("Base64: ", b64(b"Qwerty7x"), ""),
    "пароль 11 байт с паддингом":          ("Base64: ", b64(b"Helper12345"), ""),
    "id:бинарный секрет":                  ("token ", b64(b"573798680237:" + rnd(20)), ""),
    "id:бинарный секрет, лишний символ":   ("token ", b64(b"991336981330:" + rnd(20)) + "x", ""),  # 45 символов
    "PEM-ключ":                            ("", PEM_BLOCK, ""),
    "обрезанный DER-ключ в JS-строке":     ('"-----BEGIN RSA PRIVATE KEY-----\\n" +\n    "', DER, '"'),
    "заголовок JWT":                       ("Bearer ", "eyJhbGciOiJIUzI1NiJ9", ".test.signature"),
    "русские ПДн в base64":                ("payload=", b64("Иванов Иван Иванович, паспорт 4510 123456".encode()), ""),
    "токен GitHub":                        ("GITHUB_TOKEN=", "ghp_" + rid(36), ""),
    "токен GitLab":                        ("token: ", "glpat-" + rid(20), ""),
    "ключ Stripe":                         ("STRIPE_KEY=", "sk_live_" + rid(24), ""),
    "паспорт в base64 без паддинга":       ("doc=", b64("4510 123456".encode()).rstrip("="), ""),
    "дата рождения без паддинга":          ("dob=", b64(b"15.03.1985").rstrip("="), ""),
    "числовой пароль рядом с пометкой":    ('{"password": {"encoding": "base64", "value": "', b64(b"504456"), '"}}'),
    "короткий логин рядом с пометкой":     ('{"username": {"encoding": "base64", "value": "', b64(b"tb9203"), '"}}'),
    "сразу после «Base64:»":               ("Base64: ", "YQ==", ""),
    "ключ *_base64 в JSON":                ('{"document_base64":"', b64(b"hello").rstrip("="), '"}'),
    "Python: литерал + b64decode":         ('blob = "', b64(b"secret7"), '"\nmsg = base64.b64decode(blob)'),
    "картинка data URI":                   ('"url": "data:image/png;base64,', b64(b"\x89PNG\r\n\x1a\n" + rnd(300)), '"'),
    "unquoted key=value":                  ("export SESSION_BLOB=", b64(rnd(24)), ""),
    "бинарь с нулевыми байтами":           ("header ", b64(b"\x01\x00\x00\x00\x10\x00\x00\x00" + bytes(16)), ""),
    "MIME-перенос по 76":                  ("Content-Transfer-Encoding: base64\n\n",
                                            "\n".join(b64(rnd(57)) for _ in range(3)) + "\n" + b64(rnd(10)), ""),
}.items()}

MUST_NOT_MASK = {
    "md5 hex":                   "92c18f9642015bb56ca989b5cbb94ebb",
    "sha256 hex":                "0779d1e0b300a8ae30936a2dd1498c7ee861125ccc4e45cf61383c5da7d71935",
    "UUID":                      "550e8400-e29b-41d4-a716-446655440000",
    "разделитель-комментарий":   "/" * 65,
    "путь":                      "src/main/java/org/springframework/samples/petclinic",
    "kebab-имя модели":          "Qwen2.5-72B-Instruct",
    "camelCase":                 "getUserProfileById",
    "имена в коде":              "def get_config(self, defaults=None):\n    return self.config.outputs['padding']",
    "Java-импорт":               "org.glassfish.jersey.internal.util.Base64",
    "слово в кавычках рядом с Base64":  "# Legacy interface: RFC 2045 Base64\n    'encode', 'decode', 'fmt', 'big5'",
    "kwarg после строки с b64":         'x=self.unique_id_b64,\n    ctx="M2NTrainerInitInfo",',
    "переменные рядом с b64decode":     "data = base64.b64decode(payload)\nresult = data.decode('utf-8')",
    "ПДн открытым текстом":      "Топорков Дмитрий Валерьевич, +7 900 123-45-67, г.Калуга, Проспект Ленина 33",
    "русский текст с терминами": "Посмотри конфиг CPU/GPU, BERT baseline и username в headers.",
}


@pytest.mark.parametrize("name", MUST_MASK)
def test_encoded_value_is_fully_covered(name):
    text, payload = MUST_MASK[name]
    assert covered(text, payload)


@pytest.mark.parametrize("name", MUST_NOT_MASK)
def test_ordinary_text_has_no_spans(name):
    assert find_spans(MUST_NOT_MASK[name]) == []


def test_native_password_type_wins_over_cascade_span():
    from recognizers.base64_data import Base64DataRecognizer
    value = b64(b"synthetic-password-value")
    text = f'password_base64="{value}"'
    results = Base64DataRecognizer(policy="logistic").analyze(text, ["PASSWORD"])
    start = text.index(value)
    assert [(r.start, r.end, r.entity_type) for r in results] == [(start, start + len(value), "PASSWORD")]
