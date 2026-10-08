"""Context, boundaries, conservative decoding and the unchanged DKB corpus."""

import base64
import hashlib
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from recognizers import ALL_RECOGNIZERS
from recognizers.base64_data import Base64DataRecognizer, MAX_CANDIDATES, decode_candidate
from recognizers.sql_credentials import SqlCredentialRecognizer
from recognizers.json_strings import MAX_JSON_CHARS, JsonStringRecognizer, decoded_string_offsets
from recognizers.credential_rules import AuthTokenRecognizer, PasswordRecognizer
from result_merging import merge_results

ROOT = Path(__file__).resolve().parents[2]
CORPUS = ROOT / "tests/fixtures/dkb"
CASES = json.loads((CORPUS / "manifest.json").read_text())


def deterministic_entities(text):
    results = []
    for cls in ALL_RECOGNIZERS:
        recognizer = cls()
        results.extend(recognizer.analyze(text, recognizer.supported_entities, None))
    return merge_results(text, results, score_threshold=0.35).results


@pytest.mark.parametrize("case", CASES, ids=lambda case: case["file"])
def test_corpus_bytes_and_deterministic_spans(case):
    raw = (CORPUS / case["file"]).read_bytes()
    assert hashlib.sha256(raw).hexdigest() == case["sha256"]
    text = raw.decode("utf-8")
    results = deterministic_entities(text)
    for expected in case["expected"]:
        if not expected["deterministic"]:
            continue
        assert any(
            result.start <= expected["start"] and result.end >= expected["end"]
            and result.entity_type in expected["entities"] and result.score >= 0.35
            for result in results
        ), (case["file"], expected["start"], expected["entities"])


def test_binary_fixture_preserves_unpadded_base64_scenario():
    text = (CORPUS / "17.txt").read_bytes().decode("utf-8")
    value = text.removeprefix("Base64: ").strip()
    assert len(value) == 44 and "=" not in value
    decoded = base64.b64decode(value, validate=True)
    assert decoded == b"DKB-TEST-017:" + bytes(range(0xE0, 0xF4))
    with pytest.raises(UnicodeDecodeError):
        decoded.decode("utf-8")
    assert Base64DataRecognizer().analyze(value, ["BASE64_DATA"]) == []
    results = Base64DataRecognizer().analyze(text, ["BASE64_DATA"])
    assert [text[result.start:result.end] for result in results] == [value]


@pytest.mark.parametrize("value", ["test", "2026", "AAAA", "bad@encoding", "invalid", "a" * 20000])
@pytest.mark.parametrize("key,entity", [("Base64", "BASE64_DATA"), ("payload_b64", "BASE64_DATA"), ("password_base64", "PASSWORD")])
def test_declared_values_do_not_require_valid_encoding(value, key, entity):
    text = f'{key}: "{value}"; next=ordinary'
    results = Base64DataRecognizer().analyze(text, [entity])
    assert [(r.entity_type, text[r.start:r.end]) for r in results] == [(entity, value)]


@pytest.mark.parametrize("inner", [
    'password="Synthetic-Pass-2026"',
    '{"credentials":"synthetic-credentials-2026"}',
    'Email: person@example.org',
    '-----BEGIN PRIVATE KEY-----\n' + 'A' * 64 + '\n-----END PRIVATE KEY-----',
    'INSERT INTO users (username, password_hash) VALUES (\'demo_user\', \'12c18f9642947bb56ca779b5cbb33ebb\');',
])
@pytest.mark.parametrize("urlsafe", [False, True])
@pytest.mark.parametrize("padding", [False, True])
def test_undeclared_sensitive_content_masks_whole_encoding(inner, urlsafe, padding):
    encode = base64.urlsafe_b64encode if urlsafe else base64.b64encode
    value = encode(inner.encode()).decode()
    if not padding:
        value = value.rstrip("=")
    text = "Передано значение " + value + "."
    results = Base64DataRecognizer().analyze(text, ["BASE64_DATA"])
    assert [text[r.start:r.end] for r in results] == [value]


@pytest.mark.parametrize("value", [
    "test", "2026", "deadbeef", "a" * 32, "a" * 64,
    base64.b64encode(b"ordinary text without secrets").decode(),
    base64.b64encode(b"UnlabelledPassword2026!").decode(),
    base64.b64encode(bytes(range(32))).decode(),
    "ordinary_identifier_2026", "YWJj===", "YR==",
])
def test_unconfirmed_values_are_not_masked(value):
    assert Base64DataRecognizer().analyze(value, ["BASE64_DATA"]) == []


def test_multiline_declared_value_keeps_source_offsets():
    value = "YWJjZGVm\n    Z2hpamts"
    text = "Base64: " + value + "\nnext: ordinary"
    results = Base64DataRecognizer().analyze(text, ["BASE64_DATA"])
    assert [text[r.start:r.end] for r in results] == [value]


def test_decode_budget_never_affects_later_declared_fields():
    text = ("YWJjZGVm " * (MAX_CANDIDATES + 1)) + 'Base64: "test"'
    results = Base64DataRecognizer().analyze(text, ["BASE64_DATA"])
    assert [text[r.start:r.end] for r in results] == ["test"]


def test_no_recursive_decode_and_entity_filter_is_respected():
    inner = base64.b64encode(b'password="Synthetic-Pass-2026"')
    value = base64.b64encode(inner).decode()
    assert Base64DataRecognizer().analyze(value, ["BASE64_DATA"]) == []
    assert Base64DataRecognizer().analyze("Base64: dGVzdA==", ["PERSON"]) == []


@pytest.mark.parametrize("value", ["YR==", "YQ===", "YWJj-+/", "invalid!"])
def test_noncanonical_values_are_rejected_without_context(value):
    assert decode_candidate(value) is None


@pytest.mark.parametrize("body", [
    "VALUES ('user', 'hash'), ('other', 'second-hash');",
    "VALUES ('user', 'password,with)paren');",
    "VALUES ('user', 'quote''inside');",
])
def test_sql_literal_mapping_and_offsets(body):
    text = "INSERT INTO users (username, password_hash) " + body
    results = SqlCredentialRecognizer().analyze(text, ["PASSWORD"])
    assert results and all(r.entity_type == "PASSWORD" for r in results)
    assert all(text[r.start - 1] == "'" and text[r.end] == "'" for r in results)


@pytest.mark.parametrize("text", [
    "INSERT INTO users (username, password_hash) VALUES ('user', SHA256('pass'));",
    "INSERT INTO users (username, password_hash) VALUES ('user');",
    "INSERT INTO users (checksum) VALUES ('12c18f9642947bb56ca779b5cbb33ebb');",
    "INSERT INTO users (password_hash) SELECT hash FROM backup;",
])
def test_sql_expressions_and_unrelated_hashes_are_not_inferred(text):
    assert SqlCredentialRecognizer().analyze(text, ["PASSWORD"]) == []


def test_code_references_and_literals_are_distinct():
    text = 'import db\nDB_PASSWORD="Synthetic-Password-2026"\nconn(password=DB_PASSWORD)'
    results = PasswordRecognizer().analyze(text, ["PASSWORD"])
    assert [text[r.start:r.end] for r in results] == ["Synthetic-Password-2026"]
    results = PasswordRecognizer().analyze('password="DB_PASSWORD"', ["PASSWORD"])
    assert len(results) == 1
    assert PasswordRecognizer().analyze("PASSWORD=UNQUOTED_SECRET", ["PASSWORD"])
    text = 'DB_PASSWORD="Synthetic-Password-2026"\nPASSWORD=DB_PASSWORD'
    assert len(PasswordRecognizer().analyze(text, ["PASSWORD"])) == 2


def test_basic_json_and_multiline_curl_cover_full_value():
    value = "ZXh1ZXJuYWw6NXN4QjQyTjI0MzcyUDlk0DY82eA=="
    for text in ('{"authorization": "Basic ' + value + '"}',
                 "curl https://example.org \\\n --header 'Authorization: Basic " + value + "'"):
        results = AuthTokenRecognizer().analyze(text, ["AUTH_TOKEN"])
        assert [text[r.start:r.end] for r in results] == [value]


@pytest.mark.parametrize("key,entity", [("PASSWORD", "PASSWORD"), ("API_SECRET", "SECRET_KEY"), ("credentials", "AUTH_TOKEN"), ("basic_auth", "AUTH_TOKEN")])
@pytest.mark.parametrize("quoted", [False, True])
def test_long_secret_assignments_never_leave_a_tail(key, entity, quoted):
    from recognizers.credential_rules import SecretKeyRecognizer
    value = "A" * 20000
    text = f'{key}="{value}"' if quoted else f"{key}={value}"
    recognizer = {"PASSWORD": PasswordRecognizer, "SECRET_KEY": SecretKeyRecognizer, "AUTH_TOKEN": AuthTokenRecognizer}[entity]()
    results = recognizer.analyze(text, [entity])
    assert [text[r.start:r.end] for r in results] == [value]


def test_malformed_basic_alphabet_is_still_fully_protected():
    text = '{"Authorization": "Basic abcdef@not-base64!"}'
    results = AuthTokenRecognizer().analyze(text, ["AUTH_TOKEN"])
    assert [text[r.start:r.end] for r in results] == ["abcdef@not-base64!"]


@pytest.mark.parametrize("case", CASES, ids=lambda case: case["file"])
def test_deterministic_corpus_inside_json_string(case):
    source = (CORPUS / case["file"]).read_bytes().decode("utf-8")
    text = json.dumps({"source": source}, ensure_ascii=False)
    start = text.index(': ') + 2
    decoded, offsets = decoded_string_offsets(text, start, len(text) - 1)
    assert decoded == source
    results = deterministic_entities(text)
    for expected in case["expected"]:
        if not expected["deterministic"]:
            continue
        assert any(r.start <= offsets[expected["start"]] and r.end >= offsets[expected["end"]] for r in results), (case["file"], expected["start"])


def test_json_unicode_surrogates_and_escape_boundaries():
    source = '😀 пароль="Synthetic-Pass-2026"\r\n'
    text = json.dumps({"source": source})
    results = JsonStringRecognizer().analyze(text, ["PASSWORD"])
    assert len(results) == 1
    assert text[results[0].start:results[0].end] == "Synthetic-Pass-2026"
    assert JsonStringRecognizer().analyze('{"source": broken}', ["PASSWORD"]) == []


def test_secret_json_field_with_escaped_quotes_is_masked_completely():
    text = json.dumps({"password": 'Synthetic-"Quoted"-Password-2026', "source": "ordinary"})
    results = deterministic_entities(text)
    assert len(results) == 1
    value = text[results[0].start:results[0].end]
    assert json.loads('"' + value + '"') == 'Synthetic-"Quoted"-Password-2026'


@pytest.mark.parametrize("field,entity", [("Base64", "BASE64_DATA"), ("password", "PASSWORD")])
def test_declared_container_does_not_leave_an_inner_key_tail(field, entity):
    value = '-----BEGIN PRIVATE KEY-----\n' + 'A' * 64 + '\n-----END PRIVATE KEY----- suffix'
    text = json.dumps({field: value}) if field == "password" else f'{field}: "{value}"'
    results = deterministic_entities(text)
    assert len(results) == 1
    assert results[0].entity_type == entity
    masked_value = text[results[0].start:results[0].end]
    assert masked_value.endswith("suffix")
    assert "BEGIN PRIVATE KEY" in masked_value


@pytest.mark.parametrize("text", [
    '{"ordinary":"YWJjZGVm","checksum":"deadbeef"}',
    '{"password":"placeholder","source":"ordinary text"}',
    '{"source": broken}',
    json.dumps({"source": "a" * MAX_JSON_CHARS}),
])
def test_json_projection_is_conservative_and_bounded(text):
    assert JsonStringRecognizer().analyze(text, ["PASSWORD", "BASE64_DATA"]) == []
