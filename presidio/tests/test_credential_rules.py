"""Regression tests for native credential and command-line recognizers."""

import json
import sys
from pathlib import Path

import pytest


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from recognizers.credential_rules import (  # noqa: E402
    APPROVED_REVIEW_RULE_IDS,
    RULE_ID_METADATA_KEY,
    RULE_SOURCE,
    RULE_SOURCE_METADATA_KEY,
    AuthTokenRecognizer,
    CommandLineCredentialRecognizer,
    LoginRecognizer,
    PasswordRecognizer,
    SecretKeyRecognizer,
    accepted_rule_ids,
)


def _results(recognizer, text, entities=None):
    return recognizer.analyze(
        text=text,
        entities=entities or recognizer.supported_entities,
        nlp_artifacts=None,
    )


def _rule_results(recognizer, text, rule_id):
    return [
        result
        for result in _results(recognizer, text)
        if result.recognition_metadata.get(RULE_ID_METADATA_KEY) == rule_id
    ]


def _assert_rule_match(recognizer, text, rule_id, entity_type, expected_value):
    results = _rule_results(recognizer, text, rule_id)
    assert len(results) == 1
    result = results[0]
    assert result.entity_type == entity_type
    assert text[result.start : result.end] == expected_value
    assert result.recognition_metadata[RULE_SOURCE_METADATA_KEY] == RULE_SOURCE


AWS_ACCESS_TOKEN = "AKIA" + "A2B3C4D5E6F7G2H3"
BEDROCK_LONG_KEY = "ABSK" + "A" * 109
BEDROCK_SHORT_KEY = "bedrock-api-key-YmVkcm9jay5hbWF6b25hd3MuY29t"
GITHUB_APP_TOKEN = "ghu_" + "A" * 36
GITHUB_FINE_GRAINED_PAT = "github_pat_" + "A" * 82
GITHUB_OAUTH_TOKEN = "gho_" + "A" * 36
GITHUB_REFRESH_TOKEN = "ghr_" + "A" * 36
HUGGINGFACE_ACCESS_TOKEN = "hf_" + "A" * 34
HUGGINGFACE_ORG_TOKEN = "api_org_" + "A" * 34
SLACK_APP_TOKEN = "xapp-1-ABCDEF123456-1234567890-abcdef123456"
SLACK_CONFIG_ACCESS_TOKEN = "xoxe.xoxb-1-" + "A" * 163
SLACK_CONFIG_REFRESH_TOKEN = "xoxe-1-" + "A" * 146
SLACK_LEGACY_BOT_TOKEN = "xoxb-12345678-" + "A" * 18
SLACK_LEGACY_TOKEN = "xoxo-1-2-3-" + "abcdef1234567890"
SLACK_WORKSPACE_TOKEN = "xoxa-" + "A" * 12
SLACK_USER_TOKEN = (
    "xoxp-1234567890-1234567890-1234567890-" + "A" * 28
)
VAULT_BATCH_TOKEN = "hvb." + "A" * 138
VAULT_SERVICE_TOKEN = "hvs." + "A" * 90
YANDEX_ACCESS_TOKEN = "t1.abcd." + "A" * 86
YANDEX_API_KEY = "AQVN" + "A" * 35
YANDEX_AWS_ACCESS_TOKEN = "YC" + "A" * 38


PROVIDER_RULE_CASES = (
    (
        "aws-access-token",
        SecretKeyRecognizer,
        AWS_ACCESS_TOKEN,
        "SECRET_KEY",
        "BKIA" + AWS_ACCESS_TOKEN[4:],
    ),
    (
        "aws-amazon-bedrock-api-key-long-lived",
        SecretKeyRecognizer,
        BEDROCK_LONG_KEY,
        "SECRET_KEY",
        "BBSK" + BEDROCK_LONG_KEY[4:],
    ),
    (
        "aws-amazon-bedrock-api-key-short-lived",
        SecretKeyRecognizer,
        BEDROCK_SHORT_KEY,
        "SECRET_KEY",
        "bedrock-api-key-invalid",
    ),
    (
        "github-app-token",
        AuthTokenRecognizer,
        GITHUB_APP_TOKEN,
        "AUTH_TOKEN",
        "gzu_" + GITHUB_APP_TOKEN[4:],
    ),
    (
        "github-fine-grained-pat",
        AuthTokenRecognizer,
        GITHUB_FINE_GRAINED_PAT,
        "AUTH_TOKEN",
        "gitlab_pat_" + GITHUB_FINE_GRAINED_PAT[11:],
    ),
    (
        "github-oauth",
        AuthTokenRecognizer,
        GITHUB_OAUTH_TOKEN,
        "AUTH_TOKEN",
        "gzo_" + GITHUB_OAUTH_TOKEN[4:],
    ),
    (
        "github-refresh-token",
        AuthTokenRecognizer,
        GITHUB_REFRESH_TOKEN,
        "AUTH_TOKEN",
        "gzr_" + GITHUB_REFRESH_TOKEN[4:],
    ),
    (
        "huggingface-access-token",
        AuthTokenRecognizer,
        HUGGINGFACE_ACCESS_TOKEN,
        "AUTH_TOKEN",
        "hg_" + HUGGINGFACE_ACCESS_TOKEN[3:],
    ),
    (
        "huggingface-organization-api-token",
        AuthTokenRecognizer,
        HUGGINGFACE_ORG_TOKEN,
        "AUTH_TOKEN",
        "org_api_" + HUGGINGFACE_ORG_TOKEN[8:],
    ),
    (
        "slack-app-token",
        AuthTokenRecognizer,
        SLACK_APP_TOKEN,
        "AUTH_TOKEN",
        "zapp" + SLACK_APP_TOKEN[4:],
    ),
    (
        "slack-config-access-token",
        AuthTokenRecognizer,
        SLACK_CONFIG_ACCESS_TOKEN,
        "AUTH_TOKEN",
        "zoxe" + SLACK_CONFIG_ACCESS_TOKEN[4:],
    ),
    (
        "slack-config-refresh-token",
        AuthTokenRecognizer,
        SLACK_CONFIG_REFRESH_TOKEN,
        "AUTH_TOKEN",
        "zoxe" + SLACK_CONFIG_REFRESH_TOKEN[4:],
    ),
    (
        "slack-legacy-bot-token",
        AuthTokenRecognizer,
        SLACK_LEGACY_BOT_TOKEN,
        "AUTH_TOKEN",
        "zoxb" + SLACK_LEGACY_BOT_TOKEN[4:],
    ),
    (
        "slack-legacy-token",
        AuthTokenRecognizer,
        SLACK_LEGACY_TOKEN,
        "AUTH_TOKEN",
        "zoxo" + SLACK_LEGACY_TOKEN[4:],
    ),
    (
        "slack-legacy-workspace-token",
        AuthTokenRecognizer,
        SLACK_WORKSPACE_TOKEN,
        "AUTH_TOKEN",
        "zoxa" + SLACK_WORKSPACE_TOKEN[4:],
    ),
    (
        "slack-user-token",
        AuthTokenRecognizer,
        SLACK_USER_TOKEN,
        "AUTH_TOKEN",
        "zoxp" + SLACK_USER_TOKEN[4:],
    ),
    (
        "vault-batch-token",
        AuthTokenRecognizer,
        VAULT_BATCH_TOKEN,
        "AUTH_TOKEN",
        "hva." + VAULT_BATCH_TOKEN[4:],
    ),
    (
        "vault-service-token",
        AuthTokenRecognizer,
        VAULT_SERVICE_TOKEN,
        "AUTH_TOKEN",
        "hvt." + VAULT_SERVICE_TOKEN[4:],
    ),
    (
        "yandex-access-token",
        AuthTokenRecognizer,
        YANDEX_ACCESS_TOKEN,
        "AUTH_TOKEN",
        "t2" + YANDEX_ACCESS_TOKEN[2:],
    ),
    (
        "yandex-api-key",
        SecretKeyRecognizer,
        YANDEX_API_KEY,
        "SECRET_KEY",
        "BQVN" + YANDEX_API_KEY[4:],
    ),
    (
        "yandex-aws-access-token",
        SecretKeyRecognizer,
        YANDEX_AWS_ACCESS_TOKEN,
        "SECRET_KEY",
        "XZ" + YANDEX_AWS_ACCESS_TOKEN[2:],
    ),
)


def test_runtime_rule_set_matches_review_decision():
    inventory_path = (
        Path(__file__).resolve().parents[1]
        / "evaluation"
        / "reports"
        / "deterministic-rule-inventory.json"
    )
    inventory = json.loads(inventory_path.read_text(encoding="utf-8"))
    reviewed_ids = {
        item["source_id"]
        for item in inventory["rules"]
        if item["recommendation"] == "adapt"
    }

    assert reviewed_ids == APPROVED_REVIEW_RULE_IDS
    assert APPROVED_REVIEW_RULE_IDS <= set(accepted_rule_ids())


@pytest.mark.parametrize(
    "rule_id, recognizer_class, value, entity_type, near_miss",
    PROVIDER_RULE_CASES,
    ids=[case[0] for case in PROVIDER_RULE_CASES],
)
def test_approved_provider_rule_contract(
    rule_id, recognizer_class, value, entity_type, near_miss
):
    recognizer = recognizer_class()
    prefix = "BEDROCK_API_KEY=" if rule_id.endswith("short-lived") else "Секрет: "
    positive_text = prefix + value

    _assert_rule_match(recognizer, positive_text, rule_id, entity_type, value)
    assert _rule_results(recognizer, "Секрет: " + near_miss, rule_id) == []
    assert _rule_results(recognizer, prefix + "X" + value + "Y", rule_id) == []
    assert _rule_results(recognizer, "AUTH_TOKEN=<your-token>", rule_id) == []


@pytest.mark.parametrize(
    "recognizer, text, rule_id, entity_type, expected_value",
    [
        (LoginRecognizer(), "LOGIN=svc.bot", "credential.kv.login", "LOGIN", "svc.bot"),
        (
            PasswordRecognizer(),
            "password='Synthetic password value'",
            "credential.kv.password",
            "PASSWORD",
            "Synthetic password value",
        ),
        (
            PasswordRecognizer(),
            "PGPASSWORD=SyntheticPgPass",
            "credential.kv.password",
            "PASSWORD",
            "SyntheticPgPass",
        ),
        (
            PasswordRecognizer(),
            "database.password=SyntheticDbPass",
            "credential.kv.password",
            "PASSWORD",
            "SyntheticDbPass",
        ),
        (
            SecretKeyRecognizer(),
            "ZAI_API_KEY_2=SyntheticSecretKey42",
            "credential.kv.secret-key",
            "SECRET_KEY",
            "SyntheticSecretKey42",
        ),
        (
            SecretKeyRecognizer(),
            "clientSecret=SyntheticClientSecret42",
            "credential.kv.secret-key",
            "SECRET_KEY",
            "SyntheticClientSecret42",
        ),
        (
            AuthTokenRecognizer(),
            "REFRESH_TOKEN=SyntheticRefreshToken42",
            "credential.kv.auth-token",
            "AUTH_TOKEN",
            "SyntheticRefreshToken42",
        ),
    ],
)
def test_key_value_rules_return_only_the_credential_value(
    recognizer, text, rule_id, entity_type, expected_value
):
    _assert_rule_match(recognizer, text, rule_id, entity_type, expected_value)


@pytest.mark.parametrize(
    "recognizer, text, rule_id, entity_type, expected_value",
    [
        (
            LoginRecognizer(),
            "Логин пользователя: service.user-01.",
            "credential.kv.login",
            "LOGIN",
            "service.user-01",
        ),
        (
            LoginRecognizer(),
            "Пользователь входит как tester@example.test.",
            "credential.context.login-email",
            "LOGIN",
            "tester@example.test",
        ),
        (
            PasswordRecognizer(),
            "Новый пароль — «Три слова 42!».",
            "credential.kv.password",
            "PASSWORD",
            "Три слова 42!",
        ),
        (
            SecretKeyRecognizer(),
            "Секретный ключ сервиса: service-secret-ru-2026-0042.",
            "credential.kv.secret-key",
            "SECRET_KEY",
            "service-secret-ru-2026-0042",
        ),
        (
            SecretKeyRecognizer(),
            "Настройки:\nsecret: multiline-secret-synthetic-77\nрежим: закрытый",
            "credential.kv.secret-key",
            "SECRET_KEY",
            "multiline-secret-synthetic-77",
        ),
        (
            AuthTokenRecognizer(),
            "Токен авторизации: ru-auth-token-2026-session-07.",
            "credential.kv.auth-token",
            "AUTH_TOKEN",
            "ru-auth-token-2026-session-07",
        ),
        (
            AuthTokenRecognizer(),
            "X-Auth-Token: header-token-synthetic-1031",
            "credential.kv.auth-token",
            "AUTH_TOKEN",
            "header-token-synthetic-1031",
        ),
        (
            AuthTokenRecognizer(),
            "Authorization: Bearer bearer-synthetic-session-00042",
            "credential.context.authorization-bearer",
            "AUTH_TOKEN",
            "bearer-synthetic-session-00042",
        ),
        (
            AuthTokenRecognizer(),
            "Основной токен session-token-alpha-001",
            "credential.context.named-token",
            "AUTH_TOKEN",
            "session-token-alpha-001",
        ),
        (
            AuthTokenRecognizer(),
            "Резервный токен session-token-beta-002",
            "credential.context.named-token",
            "AUTH_TOKEN",
            "session-token-beta-002",
        ),
        (
            AuthTokenRecognizer(),
            (
                "Основной токен session-token-alpha-001, "
                "резервный session-token-beta-002"
            ),
            "credential.context.backup-token",
            "AUTH_TOKEN",
            "session-token-beta-002",
        ),
    ],
)
def test_context_bound_credential_rules_return_only_values(
    recognizer,
    text,
    rule_id,
    entity_type,
    expected_value,
):
    _assert_rule_match(recognizer, text, rule_id, entity_type, expected_value)


@pytest.mark.parametrize(
    "recognizer, text",
    [
        (LoginRecognizer(), "LOGIN=<your-login>"),
        (LoginRecognizer(), "USERNAME=${SERVICE_USER}"),
        (PasswordRecognizer(), "PASSWORD='{{ vault_password }}'"),
        (PasswordRecognizer(), "PGPASSWORD=$PGPASSWORD"),
        (PasswordRecognizer(), "password=${DB_PASSWORD}"),
        (PasswordRecognizer(), "password=[REDACTED:PASSWORD]"),
        (PasswordRecognizer(), "password=$(secret-tool lookup service demo)"),
        (SecretKeyRecognizer(), "API_KEY=<your-key>"),
        (SecretKeyRecognizer(), "CLIENT_SECRET=settings.client_secret"),
        (AuthTokenRecognizer(), "AUTH_TOKEN=<your-token>"),
        (AuthTokenRecognizer(), "REFRESH_TOKEN=process.env.REFRESH_TOKEN"),
        (AuthTokenRecognizer(), "auth_token=os.environ[TOKEN]"),
        (AuthTokenRecognizer(), "X-Auth-Token: <your-token>"),
        (AuthTokenRecognizer(), "Authorization: Bearer ${ACCESS_TOKEN}"),
        (SecretKeyRecognizer(), "secret: placeholder"),
        (PasswordRecognizer(), "Новый пароль — «changeme»"),
    ],
)
def test_key_value_rules_ignore_placeholders_and_code_references(recognizer, text):
    assert _results(recognizer, text) == []


@pytest.mark.parametrize(
    "recognizer, text",
    [
        (LoginRecognizer(), "USER_ID=12345"),
        (PasswordRecognizer(), "Объясни слово password"),
        (SecretKeyRecognizer(), "aB3dE5fG7hJ9kL2mN4pQ6rS8tU0vW1xY"),
        (AuthTokenRecognizer(), "token bucket limits requests"),
        (AuthTokenRecognizer(), "Основной токен недоступен"),
        (AuthTokenRecognizer(), "Резервный идентификатор backup-service-001"),
        (LoginRecognizer(), "Пользователь входит как администратор"),
        (SecretKeyRecognizer(), "Секретный ключ сервиса не настроен"),
    ],
)
def test_native_rules_ignore_weak_context_and_standalone_entropy(recognizer, text):
    assert _results(recognizer, text) == []


@pytest.mark.parametrize(
    "text, expected",
    [
        ("user=alice", "alice"),
        ("PGUSER=analytics", "analytics"),
        ("smtp_user=mailer_bot", "mailer_bot"),
        ("Логин в панель: ci_runner_12", "ci_runner_12"),
        ("Пользователь k8s-controller-us1 в кластере", "k8s-controller-us1"),
        ("ssh deploy@go-i-ml-01.", "deploy"),
    ],
)
def test_login_rules_cover_structured_and_contextual_forms(text, expected):
    results = _results(LoginRecognizer(), text)

    assert [text[result.start : result.end] for result in results] == [expected]


@pytest.mark.parametrize(
    "text",
    [
        "Пользователь Ivan согласовал документ",
        "Пользователь системы выполняет операцию",
        "user story описывает новый сценарий",
    ],
)
def test_contextual_login_rule_rejects_ordinary_prose(text):
    assert _results(LoginRecognizer(), text) == []


@pytest.mark.parametrize(
    "text, rule_id, entity_type, expected_value",
    [
        (
            "curl -u demo:SyntheticCurlPass https://api.example.test",
            "curl-auth-user",
            "PASSWORD",
            "SyntheticCurlPass",
        ),
        (
            "curl -H 'Authorization: Bearer SyntheticCurlToken42' "
            "https://api.example.test",
            "curl-auth-header",
            "AUTH_TOKEN",
            "SyntheticCurlToken42",
        ),
        (
            "curl --oauth2-bearer SyntheticOAuthToken42 https://api.example.test",
            "curl-oauth2-bearer",
            "AUTH_TOKEN",
            "SyntheticOAuthToken42",
        ),
        (
            "wget --http-password=SyntheticWgetPass https://example.test/file",
            "wget-password",
            "PASSWORD",
            "SyntheticWgetPass",
        ),
        (
            "mysql -pSyntheticMysqlPass database",
            "mysql-password",
            "PASSWORD",
            "SyntheticMysqlPass",
        ),
        (
            "http --auth demo:SyntheticHttpiePass https://example.test",
            "httpie-auth",
            "PASSWORD",
            "SyntheticHttpiePass",
        ),
        (
            "redis-cli -a SyntheticRedisPass ping",
            "redis-password",
            "PASSWORD",
            "SyntheticRedisPass",
        ),
        (
            "backup-tool --password SyntheticBackupPass",
            "generic-password",
            "PASSWORD",
            "SyntheticBackupPass",
        ),
        (
            "backup-tool --password=--SyntheticBackupPass",
            "generic-password",
            "PASSWORD",
            "--SyntheticBackupPass",
        ),
        (
            "backup-tool --password '--SyntheticBackupPass'",
            "generic-password",
            "PASSWORD",
            "--SyntheticBackupPass",
        ),
    ],
)
def test_command_line_rules_return_only_literal_credential_values(
    text, rule_id, entity_type, expected_value
):
    _assert_rule_match(
        CommandLineCredentialRecognizer(),
        text,
        rule_id,
        entity_type,
        expected_value,
    )


@pytest.mark.parametrize(
    "text, rule_id",
    [
        ("curl -u demo:${CURL_PASSWORD} https://example.test", "curl-auth-user"),
        (
            "curl -H 'Authorization: Bearer ${ACCESS_TOKEN}' https://example.test",
            "curl-auth-header",
        ),
        ("curl -u demo:x https://example.test", "curl-auth-user"),
        ("curl -H 'X-Trace: SyntheticTraceValue42' https://example.test", "curl-auth-header"),
        ("wget --http-password=$WGET_PASSWORD https://example.test", "wget-password"),
        ("mysql --password=$(secret-tool lookup mysql password)", "mysql-password"),
        ("python -pSyntheticPythonValue", "mysql-password"),
    ],
)
def test_command_line_rules_ignore_placeholders_and_near_misses(text, rule_id):
    assert _rule_results(CommandLineCredentialRecognizer(), text, rule_id) == []


@pytest.mark.parametrize(
    "text",
    [
        "backup-tool --password --verbose",
        "backup-tool --passwd --verbose",
        "backup-tool --pass --verbose",
        "curl --user --verbose https://example.test",
        "curl --proxy-user --verbose https://example.test",
        "curl --oauth2-bearer --location https://example.test",
        "curl --header --compressed https://example.test",
        "curl -u -v https://example.test",
        "curl -U -v https://example.test",
        "curl -H --compressed https://example.test",
        "wget --http-password --quiet https://example.test/file",
        "wget --proxy-password --quiet https://example.test/file",
        "wget --ftp-password --quiet https://example.test/file",
        "mysql --password --host database.internal",
        "mysql -p --host database.internal",
        "http --auth --verbose https://example.test",
        "http -a --verbose https://example.test",
        "https --auth --verbose https://example.test",
        "https -a --verbose https://example.test",
        "redis-cli --pass --tls ping",
        "redis-cli -a --tls ping",
    ],
)
def test_command_line_rules_do_not_treat_following_option_as_credential(text):
    assert _results(CommandLineCredentialRecognizer(), text) == []
