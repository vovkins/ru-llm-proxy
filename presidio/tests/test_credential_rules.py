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


@pytest.mark.parametrize("value", ["-$", "!", "$$", "@#%&", "()[]{}", "\\", "\"\"", " -$ "])
@pytest.mark.parametrize("key", ["password", "DB_PASSWORD", "Пароль"])
def test_explicit_punctuation_password_is_covered_in_full(value, key):
    text = f"{key}='{value}'"
    _assert_rule_match(PasswordRecognizer(), text, "credential.kv.password", "PASSWORD", value)


@pytest.mark.parametrize("text", ["bash-$", "zsh-$", "-$", "echo '-$'", "password='   '",
                                  "password=${?}", "password=$()", "password=$PASSWORD",
                                  "password=<REDACTED>", "password=[REDACTED]"])
def test_punctuation_fix_keeps_prompts_and_template_references_inert(text):
    assert PasswordRecognizer().analyze(text, ["PASSWORD"]) == []


def test_short_alphabetic_password_policy_and_other_recognizers_are_unchanged():
    assert PasswordRecognizer().analyze("password='ab'", ["PASSWORD"]) == []
    assert LoginRecognizer().analyze("login='-$'", ["LOGIN"]) == []


@pytest.mark.parametrize("text", ["password == 'value'", "password={}", "password => other",
                                  "password='{}{}'.format(first, second)"])
def test_punctuation_rule_does_not_capture_operator_or_template_prefixes(text):
    assert PasswordRecognizer().analyze(text, ["PASSWORD"]) == []


def test_bare_punctuation_password_and_quoted_braces_are_literal_values():
    _assert_rule_match(PasswordRecognizer(), "password=-$", "credential.kv.password", "PASSWORD", "-$")
    _assert_rule_match(PasswordRecognizer(), "password='{}{}'", "credential.kv.password", "PASSWORD", "{}{}")


@pytest.mark.parametrize("value", ["!?$", "??!", "???", "?", "-$"])
@pytest.mark.parametrize("key,separator", [("Пароль", ": "), ("PASSWORD", "="), ("DB_PASSWORD", "=")])
@pytest.mark.parametrize("quoted", [False, True])
def test_explicit_question_mark_password_is_covered_in_full(value, key, separator, quoted):
    literal = json.dumps(value) if quoted else value
    _assert_rule_match(
        PasswordRecognizer(), f"{key}{separator}{literal}",
        "credential.kv.password", "PASSWORD", value,
    )


@pytest.mark.parametrize("suffix", ["", "\nordinary text", " next=visible", ", next=visible", "; next=visible"])
def test_question_mark_password_does_not_consume_adjacent_fields(suffix):
    text = "PASSWORD=!?$" + suffix
    _assert_rule_match(PasswordRecognizer(), text, "credential.kv.password", "PASSWORD", "!?$")


@pytest.mark.parametrize("value", ["!?$", "???", "?"])
def test_question_mark_password_preserves_json_field_boundaries(value):
    text = json.dumps({"password": value, "next": "ordinary"})
    _assert_rule_match(PasswordRecognizer(), text, "credential.kv.password", "PASSWORD", value)


@pytest.mark.parametrize("quoted", [False, True])
@pytest.mark.parametrize("length,accepted", [(1, True), (256, True), (257, False)])
def test_question_mark_password_length_limit_is_not_a_partial_match(quoted, length, accepted):
    value = "?" * length
    text = "PASSWORD=" + (json.dumps(value) if quoted else value)
    results = _results(PasswordRecognizer(), text)
    if accepted:
        assert len(results) == 1 and results[0].entity_type == "PASSWORD"
        assert text[results[0].start:results[0].end] == value
    else:
        assert results == []


@pytest.mark.parametrize("text", [
    "Что означает пароль?", "Просто пунктуация: !?$", "???", "bash-?", "echo '!?$'",
    "password=${SECRET}", "password=${?}", "password=config.password", "password=$()",
    "password == '?'", "password => other", "password='{}{}'.format(first, second)",
    "password=", "password='   '", "password='ab'", "password=changeme",
])
def test_question_mark_password_keeps_context_and_reference_exclusions(text):
    assert _results(PasswordRecognizer(), text) == []


@pytest.mark.parametrize("recognizer,entity,key", [
    (LoginRecognizer, "LOGIN", "login"), (AuthTokenRecognizer, "AUTH_TOKEN", "AUTH_TOKEN"),
    (SecretKeyRecognizer, "SECRET_KEY", "SECRET_KEY"),
])
def test_question_mark_password_does_not_change_other_credential_types(recognizer, entity, key):
    assert recognizer().analyze(f"{key}=!?$", [entity]) == []


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
    "utterance",
    [
        "what is 2+2?",
        "what command should I run to list files?",
        "what command should I run to watch files?",
        "what files are in this directory?",
        "which file contains the implementation?",
        "write tests for new feature",
        "Where are errors from the client handled?",
        "Explain the result of the calculation.",
    ],
)
@pytest.mark.parametrize("markup", [False, True])
def test_login_rule_does_not_treat_dialogue_role_as_credential(utterance, markup):
    text = f"user: {utterance}\nassistant: Done."
    if markup:
        text = f"<example>\n{text}\n</example>"
    assert _results(LoginRecognizer(), text) == []


@pytest.mark.parametrize("indent", ["", "  ", "\t"])
@pytest.mark.parametrize("newline", ["\n", "\r\n"])
def test_dialogue_role_refinement_preserves_offsets_and_repeated_turns(indent, newline):
    text = newline.join(
        [
            "Instructions:",
            f"{indent}USER: what is 2+2?",
            f"{indent}Assistant: 4",
            f"{indent}user: which file is needed? LOGIN=dialogue_user_12",
            f"{indent}assistant: Done.",
            "USER=outside_user_34",
        ]
    )
    results = _results(LoginRecognizer(), text)
    assert [text[r.start:r.end] for r in sorted(results, key=lambda r: r.start)] == [
        "dialogue_user_12", "outside_user_34"
    ]


@pytest.mark.parametrize(
    "text, expected",
    [
        ("user: analytics", "analytics"),
        ("user: analytics\nassistant: helper", "analytics"),
        ("user: analytics # account\nassistant: helper", "analytics"),
        ("user: 'analytics' # account\nassistant: helper", "analytics"),
        ('{"user": "analytics", "assistant": "helper"}', "analytics"),
        ("user: what is needed?\nother: Done.", "what"),
        ("user: what is needed?\n  assistant: Done.", "what"),
        ("user: what is needed?\nassistant:", "what"),
        ("prefix user: what is needed?\nassistant: Done.", "what"),
        ("USER=analytics\nassistant: Done.", "analytics"),
        ("config.user=analytics\nassistant: Done.", "analytics"),
        ("PGUSER=analytics\nassistant: Done.", "analytics"),
        ("smtp_user=mailer_bot\nassistant: Done.", "mailer_bot"),
        ("username: ci_runner_12\nassistant: Done.", "ci_runner_12"),
        ('user: "analytics" details\nassistant: Done.', "analytics"),
    ],
)
def test_dialogue_role_refinement_keeps_credential_fields_and_ambiguous_text(text, expected):
    results = _results(LoginRecognizer(), text)
    assert [text[r.start:r.end] for r in results] == [expected]


def test_dialogue_role_refinement_keeps_nested_credentials_and_other_classes():
    text = (
        "<example>\nuser: what is LOGIN=dialogue_user_12 "
        "PASSWORD=DialoguePass-42 AUTH_TOKEN=dialogue-auth-token-42 "
        "SECRET_KEY=dialogue-secret-key-42\nassistant: Done.\n</example>"
    )
    expected = {
        "LOGIN": "dialogue_user_12",
        "PASSWORD": "DialoguePass-42",
        "AUTH_TOKEN": "dialogue-auth-token-42",
        "SECRET_KEY": "dialogue-secret-key-42",
    }
    for recognizer in (LoginRecognizer(), PasswordRecognizer(), AuthTokenRecognizer(), SecretKeyRecognizer()):
        results = _results(recognizer, text)
        assert [(r.entity_type, text[r.start:r.end]) for r in results] == [
            (recognizer.supported_entities[0], expected[recognizer.supported_entities[0]])
        ]


def test_dialogue_role_refinement_does_not_ignore_user_assignment_inside_replica():
    text = (
        "user: Please review user: svc_account_42 and ssh deploy@go-i-ml-01\n"
        "assistant: Done."
    )
    results = _results(LoginRecognizer(), text)
    assert [text[r.start:r.end] for r in sorted(results, key=lambda r: r.start)] == [
        "svc_account_42", "deploy"
    ]
    assert _results(LoginRecognizer(), text, entities=["PASSWORD"]) == []


@pytest.mark.parametrize("utterance", ["which file is needed?", "write tests for this feature", "Where are errors handled?", "what is 2+2?"])
@pytest.mark.parametrize("structure", ["object", "list", "nested", "string"])
def test_json_dialogue_roles_do_not_become_logins(utterance, structure):
    source = f"user: {utterance}\nassistant: Done."
    value = {
        "object": {"source": source},
        "list": [source],
        "nested": {"history": [{"source": source}]},
        "string": source,
    }[structure]
    text = json.dumps(value)
    assert _results(LoginRecognizer(), text) == []


@pytest.mark.parametrize("newline", ["\n", "\r\n"])
@pytest.mark.parametrize("unicode_escapes", [False, True])
def test_json_dialogue_offsets_keep_independent_credentials(newline, unicode_escapes):
    from recognizers.json_strings import JsonStringRecognizer

    source = newline.join([
        'Quote " and slash \\ and emoji 😀',
        'user: which file is needed? LOGIN=dialogue_user_12',
        'assistant: Done.',
        'user: write tests for this feature',
        'assistant: Done.',
    ])
    text = json.dumps({"source": source, "USER": "outside_user_34"})
    if unicode_escapes:
        text = text.replace('\\n', '\\u000a').replace('\\r', '\\u000d').replace('which', '\\u0077hich')
    results = _results(LoginRecognizer(), text) + _results(JsonStringRecognizer(), text, entities=["LOGIN"])
    assert {(r.entity_type, r.start, r.end) for r in results} == {
        ("LOGIN", text.index(value), text.index(value) + len(value))
        for value in ("dialogue_user_12", "outside_user_34")
    }


@pytest.mark.parametrize("value", [
    'user: analytics',
    'user: analytics\nassistant: helper',
    'user: analytics # account\nassistant: helper',
    'user: "analytics" details\nassistant: Done.',
    'USER=analytics\nassistant: Done.',
    'PGUSER=analytics\nassistant: Done.',
    'smtp_user=analytics\nassistant: Done.',
])
def test_json_dialogue_refinement_keeps_actual_and_ambiguous_logins(value):
    from recognizers.json_strings import JsonStringRecognizer

    text = json.dumps({"source": value})
    results = _results(LoginRecognizer(), text) + _results(JsonStringRecognizer(), text, entities=["LOGIN"])
    assert {(r.entity_type, text[r.start:r.end]) for r in results} == {("LOGIN", "analytics")}


@pytest.mark.parametrize("text", [
    '{"source":"user: which file is needed?\\nassistant: Done.","USER":"acct_12"',
    '{"source":"user: which file is needed?\\nassistant: Done.","USER":"acct_12",}',
    '{"source":"user: which file is needed?\\nassistant: Done.","number":NaN,"USER":"acct_12"}',
    '{"source":"user: which file is needed?\\nassistant: Done."} trailing USER=acct_12',
    '{"source":"\nuser: which file is needed?\nassistant: Done.\n","USER":"acct_12"}',
])
def test_invalid_json_never_suppresses_raw_login_detections(text):
    results = _results(LoginRecognizer(), text)
    assert {text[r.start:r.end] for r in results} == {"which", "acct_12"}


def test_json_dialogue_in_key_does_not_suppress_native_detections():
    text = json.dumps({'user: which file is needed?\nassistant: Done.': 'ordinary'})
    results = _results(LoginRecognizer(), text)
    assert [text[r.start:r.end] for r in results] == ["which"]


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
