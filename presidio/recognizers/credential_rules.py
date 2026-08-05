"""Native deterministic recognizers for credential values and command snippets."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable

from presidio_analyzer import EntityRecognizer, RecognizerResult

from result_merging import (
    DETECTION_SOURCE_METADATA_KEY,
    SOURCE_NATIVE_CREDENTIAL,
)


RULE_SOURCE_METADATA_KEY = "deterministic_rule_source"
RULE_ID_METADATA_KEY = "deterministic_rule_id"
RULE_SOURCE = "ru-llm-proxy:issue-67-native-adaptation"
APPROVED_REVIEW_RULE_IDS = frozenset(
    {
        "aws-access-token",
        "aws-amazon-bedrock-api-key-long-lived",
        "aws-amazon-bedrock-api-key-short-lived",
        "curl-auth-header",
        "curl-auth-user",
        "github-app-token",
        "github-fine-grained-pat",
        "github-oauth",
        "github-refresh-token",
        "huggingface-access-token",
        "huggingface-organization-api-token",
        "slack-app-token",
        "slack-config-access-token",
        "slack-config-refresh-token",
        "slack-legacy-bot-token",
        "slack-legacy-token",
        "slack-legacy-workspace-token",
        "slack-user-token",
        "vault-batch-token",
        "vault-service-token",
        "yandex-access-token",
        "yandex-api-key",
        "yandex-aws-access-token",
    }
)

_COMMON_PLACEHOLDERS = frozenset(
    {
        "changeme",
        "example",
        "example.com",
        "false",
        "none",
        "null",
        "optional",
        "placeholder",
        "replace",
        "replace-me",
        "sample",
        "test",
        "true",
        "your-key",
        "your-token",
        "your_api_key",
    }
)
_PLACEHOLDER_RE = re.compile(
    r"^(?:"
    r"\$\{[^}\r\n]+\}|\$[A-Za-z_]\w*|%[A-Za-z_]\w*%|"
    r"\{\{[^}\r\n]+\}\}|<[^>\r\n]+>|"
    r"\[(?:PII_)?REDACTED(?::[A-Z_]+)?\]|"
    r"(?:YOUR|REPLACE|EXAMPLE|PLACEHOLDER)(?:[-_][A-Z0-9_-]+)?"
    r")$",
    re.IGNORECASE,
)
_CODE_REFERENCE_RE = re.compile(
    r"^(?:"
    r"os\.(?:environ\[[^]\r\n]+\]|getenv\([^\r\n]+\))|"
    r"(?:config|settings|env|process\.env)(?:\.[A-Za-z_$][\w$]*)+|"
    r"System\.getenv\([^\r\n]+\)|"
    r"[A-Za-z_$][\w$]*\[[\"'][^\r\n]+[\"']\]"
    r")$",
    re.IGNORECASE,
)
_QUOTE_PAIRS = {'"': '"', "'": "'", "«": "»", "“": "”"}
_ASSIGNMENT_VALUE = (
    r"(?P<value>"
    r"\$\{[^}\r\n]+\}|\{\{[^}\r\n]+\}\}|<[^>\r\n]+>|"
    r"\[(?:PII_)?REDACTED(?::[A-Z_]+)?\]|"
    r"os\.(?:environ\[[^]\r\n]+\]|getenv\([^\r\n]+\))|"
    r"System\.getenv\([^\r\n]+\)|"
    r"[A-Za-z_$][\w$]*\[[\"'][^\r\n]+[\"']\]|"
    r"(?:config|settings|env|process\.env)(?:\.[A-Za-z_$][\w$]*)+|"
    r'"[^"\r\n]{1,256}"|'
    r"'[^'\r\n]{1,256}'|"
    r"«[^»\r\n]{1,256}»|"
    r"“[^”\r\n]{1,256}”|"
    r"[^\s,;&}\]\r\n]{1,256}"
    r")"
)


@dataclass(frozen=True)
class _CredentialRule:
    rule_id: str
    regex: re.Pattern[str]
    score: float


@dataclass(frozen=True)
class _ShellToken:
    start: int
    end: int
    value_start: int
    value_end: int
    value: str


def _assignment_rule(rule_id: str, key_regex: str, score: float) -> _CredentialRule:
    return _CredentialRule(
        rule_id=rule_id,
        regex=re.compile(
            rf"(?<![\w.-])[\"']?(?:{key_regex})[\"']?"
            rf"(?:\s*[:=]\s*|\s+[—–]\s+)"
            rf"{_ASSIGNMENT_VALUE}",
            re.IGNORECASE | re.MULTILINE,
        ),
        score=score,
    )


def _value_rule(rule_id: str, value_regex: str, score: float = 0.95) -> _CredentialRule:
    return _CredentialRule(
        rule_id=rule_id,
        regex=re.compile(
            rf"(?<![A-Za-z0-9_-])(?P<value>{value_regex})(?![A-Za-z0-9_-])"
        ),
        score=score,
    )


def _strip_value_wrapper(text: str, start: int, end: int) -> tuple[int, int, str]:
    while start < end and text[start].isspace():
        start += 1
    while end > start and text[end - 1].isspace():
        end -= 1
    if end - start >= 2 and _QUOTE_PAIRS.get(text[start]) == text[end - 1]:
        start += 1
        end -= 1
    else:
        value = text[start:end]
        preserves_closing_delimiter = (
            re.search(r"\$\{[^{}\r\n]+\}$", value) is not None
            or re.search(r"\{\{[^{}\r\n]+\}\}$", value) is not None
            or re.search(r"(?:os\.environ|[A-Za-z_$][\w$]*)\[[^]\r\n]+\]$", value)
            is not None
            or re.search(r"(?:os|System)\.getenv\([^()\r\n]+\)$", value)
            is not None
            or (value.startswith("[") and value.endswith("]"))
        )
        trailing_punctuation = ".,;" if preserves_closing_delimiter else ".,;)]}"
        while end > start and text[end - 1] in trailing_punctuation:
            end -= 1
    return start, end, text[start:end]


def _is_inert_credential_value(value: str) -> bool:
    normalized = value.strip().strip('"\'«»“”')
    lowered = normalized.lower()
    if not normalized or not any(character.isalnum() for character in normalized):
        return True
    if lowered in _COMMON_PLACEHOLDERS:
        return True
    if _PLACEHOLDER_RE.fullmatch(normalized) or _CODE_REFERENCE_RE.fullmatch(normalized):
        return True
    if normalized.startswith(("$(", "`")):
        return True
    if lowered.startswith(("your-", "your_", "replace-", "replace_", "example-", "example_")):
        return True
    return lowered.startswith(("http://", "https://", "//"))


def _recognition_metadata(recognizer: EntityRecognizer, rule_id: str) -> dict[str, str]:
    return {
        RecognizerResult.RECOGNIZER_NAME_KEY: recognizer.name,
        RecognizerResult.RECOGNIZER_IDENTIFIER_KEY: recognizer.id,
        RULE_SOURCE_METADATA_KEY: RULE_SOURCE,
        RULE_ID_METADATA_KEY: rule_id,
        DETECTION_SOURCE_METADATA_KEY: SOURCE_NATIVE_CREDENTIAL,
    }


class _CapturedCredentialRecognizer(EntityRecognizer):
    """Recognize a named value group and return value-only source offsets."""

    RULES: tuple[_CredentialRule, ...] = ()

    def __init__(
        self,
        *,
        supported_entity: str,
        name: str,
        supported_language: str = "ru",
    ):
        super().__init__(
            supported_entities=[supported_entity],
            name=name,
            supported_language=supported_language,
            version="2.0.0",
        )

    def load(self) -> None:
        """No external assets are required."""

    def analyze(self, text, entities, nlp_artifacts=None):
        entity_type = self.supported_entities[0]
        if entities and entity_type not in entities:
            return []
        results = []
        for rule in self.RULES:
            for match in rule.regex.finditer(text):
                start, end = match.span("value")
                start, end, value = _strip_value_wrapper(text, start, end)
                if start >= end or not self._accept_value(value, rule.rule_id):
                    continue
                results.append(
                    RecognizerResult(
                        entity_type=entity_type,
                        start=start,
                        end=end,
                        score=rule.score,
                        recognition_metadata=_recognition_metadata(self, rule.rule_id),
                    )
                )
        return EntityRecognizer.remove_duplicates(results)

    def _accept_value(self, value: str, rule_id: str) -> bool:
        return not _is_inert_credential_value(value)


_ENV_PREFIX = r"(?:[A-Za-z][A-Za-z0-9]{0,31}[._]){0,3}"


class LoginRecognizer(_CapturedCredentialRecognizer):
    """Recognize login values behind strict login and username keys."""

    RULES = (
        _assignment_rule(
            "credential.kv.login",
            rf"{_ENV_PREFIX}(?:LOGIN|USERNAME|USER_NAME)|"
            rf"логин(?:\s+пользователя)?",
            0.75,
        ),
        _CredentialRule(
            "credential.context.login-email",
            re.compile(
                r"(?i)(?<![\w])пользователь\s+входит\s+как\s+"
                r"(?P<value>[A-Za-z0-9._%+-]+@"
                r"[A-Za-z0-9.-]+\.[A-Za-z]{2,63})"
            ),
            0.85,
        ),
    )

    def __init__(
        self,
        name: str = "LoginRecognizer",
        supported_language: str = "ru",
        supported_entity: str = "LOGIN",
    ):
        super().__init__(
            supported_entity=supported_entity,
            name=name,
            supported_language=supported_language,
        )

    def _accept_value(self, value: str, rule_id: str) -> bool:
        return (
            not _is_inert_credential_value(value)
            and 3 <= len(value) <= 64
            and not value.isdigit()
            and re.fullmatch(r"[A-Za-zА-Яа-яЁё0-9._@-]+", value) is not None
        )


class PasswordRecognizer(_CapturedCredentialRecognizer):
    """Recognize password values behind strict password keys."""

    RULES = (
        _assignment_rule(
            "credential.kv.password",
            rf"(?:{_ENV_PREFIX}(?:PASSWORD|PASSWD|PWD)|PGPASSWORD|пароль)",
            0.9,
        ),
    )

    def __init__(
        self,
        name: str = "PasswordRecognizer",
        supported_language: str = "ru",
        supported_entity: str = "PASSWORD",
    ):
        super().__init__(
            supported_entity=supported_entity,
            name=name,
            supported_language=supported_language,
        )

    def _accept_value(self, value: str, rule_id: str) -> bool:
        return not _is_inert_credential_value(value) and 4 <= len(value) <= 256


class SecretKeyRecognizer(_CapturedCredentialRecognizer):
    """Recognize generic secret-key assignments and approved strict formats."""

    RULES = (
        _assignment_rule(
            "credential.kv.secret-key",
            rf"{_ENV_PREFIX}(?:API_KEY|CLIENT_SECRET|SECRET_KEY|PRIVATE_KEY|"
            rf"SIGNING_KEY|ENCRYPTION_KEY|APIKEY|CLIENTSECRET|SECRETKEY)"
            rf"(?:_\d+)?|SECRET|секретный\s+ключ(?:\s+сервиса)?",
            0.9,
        ),
        _value_rule(
            "aws-access-token",
            r"(?:A3T[A-Z0-9]|AKIA|ASIA|ABIA|ACCA)[A-Z2-7]{16}",
        ),
        _value_rule(
            "aws-amazon-bedrock-api-key-long-lived",
            r"ABSK[A-Za-z0-9+/]{109,269}={0,2}",
        ),
        _CredentialRule(
            "aws-amazon-bedrock-api-key-short-lived",
            re.compile(
                r"(?i)(?<![\w.-])(?:AWS_)?BEDROCK_API_KEY\s*[:=]\s*[\"']?"
                r"(?P<value>bedrock-api-key-YmVkcm9jay5hbWF6b25hd3MuY29t)"
                r"[\"']?(?![A-Za-z0-9_-])"
            ),
            0.95,
        ),
        _value_rule("yandex-api-key", r"AQVN[A-Za-z0-9_-]{35,38}"),
        _value_rule("yandex-aws-access-token", r"YC[A-Za-z0-9_-]{38}"),
    )

    def __init__(
        self,
        name: str = "SecretKeyRecognizer",
        supported_language: str = "ru",
        supported_entity: str = "SECRET_KEY",
    ):
        super().__init__(
            supported_entity=supported_entity,
            name=name,
            supported_language=supported_language,
        )

    def _accept_value(self, value: str, rule_id: str) -> bool:
        if rule_id != "credential.kv.secret-key":
            return True
        return (
            not _is_inert_credential_value(value)
            and 8 <= len(value) <= 256
            and not value.isdigit()
        )


class AuthTokenRecognizer(_CapturedCredentialRecognizer):
    """Recognize auth-token assignments and approved strict token formats."""

    RULES = (
        _assignment_rule(
            "credential.kv.auth-token",
            rf"{_ENV_PREFIX}(?:ACCESS_TOKEN|REFRESH_TOKEN|AUTH_TOKEN|ID_TOKEN|"
            rf"BEARER_TOKEN|JWT_TOKEN|ACCESSTOKEN|REFRESHTOKEN|AUTHTOKEN|"
            rf"IDTOKEN|BEARERTOKEN|JWTTOKEN)|X-AUTH-TOKEN|"
            rf"токен(?:\s+авторизации)?",
            0.9,
        ),
        _CredentialRule(
            "credential.context.authorization-bearer",
            re.compile(
                r"(?i)(?<![\w.-])Authorization\s*:\s*Bearer\s+"
                r"(?P<value>[A-Za-z0-9_=~@.+/-]{8,256})"
            ),
            0.95,
        ),
        _CredentialRule(
            "credential.context.named-token",
            re.compile(
                r"(?i)(?<![\w])(?:основной|резервный)\s+токен\s+"
                r"(?P<value>[A-Za-z0-9_=~@.+/-]{8,256})"
            ),
            0.9,
        ),
        _CredentialRule(
            "credential.context.backup-token",
            re.compile(
                r"(?i)(?<![\w])основной\s+токен\s+"
                r"[A-Za-z0-9_=~@.+/-]{8,256}\s*,\s*"
                r"резервный(?:\s+токен)?\s+"
                r"(?P<value>[A-Za-z0-9_=~@.+/-]{8,256})"
            ),
            0.9,
        ),
        _value_rule("github-app-token", r"gh[us]_[A-Za-z0-9]{36}"),
        _value_rule("github-fine-grained-pat", r"github_pat_[A-Za-z0-9_]{82}"),
        _value_rule("github-oauth", r"gho_[A-Za-z0-9]{36}"),
        _value_rule("github-refresh-token", r"ghr_[A-Za-z0-9]{36}"),
        _value_rule("github-pat-existing", r"ghp_[A-Za-z0-9]{36}"),
        _value_rule("gitlab-pat-existing", r"glpat-[A-Za-z0-9_-]{20}"),
        _value_rule("huggingface-access-token", r"hf_[A-Za-z]{34}"),
        _value_rule("huggingface-organization-api-token", r"api_org_[A-Za-z]{34}"),
        _value_rule("slack-app-token", r"xapp-\d-[A-Za-z0-9]+-\d+-[A-Za-z0-9]+"),
        _value_rule("slack-config-access-token", r"xoxe\.xox[bp]-\d-[A-Za-z0-9]{163,166}"),
        _value_rule("slack-config-refresh-token", r"xoxe-\d-[A-Za-z0-9]{146}"),
        _value_rule("slack-legacy-bot-token", r"xoxb-[0-9]{8,14}-[A-Za-z0-9]{18,26}"),
        _value_rule("slack-legacy-token", r"xox[os]-\d+-\d+-\d+-[A-Fa-f0-9]+"),
        _value_rule("slack-legacy-workspace-token", r"xox[ar]-(?:\d-)?[A-Za-z0-9]{8,48}"),
        _value_rule("slack-user-token", r"xox[pe](?:-[0-9]{10,13}){3}-[A-Za-z0-9-]{28,34}"),
        _value_rule("vault-batch-token", r"hvb\.[A-Za-z0-9_-]{138,300}"),
        _value_rule("vault-service-token", r"(?:hvs\.[A-Za-z0-9_-]{90,120}|s\.[A-Za-z0-9]{24})"),
        _value_rule("yandex-access-token", r"t1\.[A-Za-z0-9_-]+={0,2}\.[A-Za-z0-9_-]{86}={0,2}"),
    )

    def __init__(
        self,
        name: str = "AuthTokenRecognizer",
        supported_language: str = "ru",
        supported_entity: str = "AUTH_TOKEN",
    ):
        super().__init__(
            supported_entity=supported_entity,
            name=name,
            supported_language=supported_language,
        )

    def _accept_value(self, value: str, rule_id: str) -> bool:
        if rule_id != "credential.kv.auth-token":
            return True
        return (
            not _is_inert_credential_value(value)
            and 8 <= len(value) <= 256
            and not value.isdigit()
        )


_SHELL_SEGMENT_RE = re.compile(r"[^;&|\r\n]+")
_SHELL_TOKEN_RE = re.compile(r'''"(?:\\.|[^"\\])*"|'[^']*'|[^\s]+''')
_SHELL_WRAPPERS = frozenset({"command", "env", "sudo", "xargs"})
_ENV_ASSIGNMENT_RE = re.compile(r"[A-Za-z_]\w*=.*", re.DOTALL)


def _shell_tokens(text: str, segment_start: int, segment_end: int) -> list[_ShellToken]:
    tokens = []
    segment = text[segment_start:segment_end]
    for match in _SHELL_TOKEN_RE.finditer(segment):
        start = segment_start + match.start()
        end = segment_start + match.end()
        value_start, value_end, value = _strip_value_wrapper(text, start, end)
        tokens.append(_ShellToken(start, end, value_start, value_end, value))
    return tokens


def _command_index(tokens: list[_ShellToken]) -> tuple[int, str] | None:
    for index, token in enumerate(tokens):
        normalized = token.value.rsplit("/", 1)[-1].lower()
        normalized = re.sub(r"\.exe$", "", normalized)
        if normalized in _SHELL_WRAPPERS or normalized.startswith("-"):
            continue
        if _ENV_ASSIGNMENT_RE.fullmatch(token.value):
            continue
        return index, normalized
    return None


def _inline_value(text: str, token: _ShellToken, offset: int) -> tuple[int, int, str]:
    start = token.value_start + offset
    return _strip_value_wrapper(text, start, token.value_end)


def _is_explicitly_quoted(text: str, token: _ShellToken) -> bool:
    return (
        token.end - token.start >= 2
        and _QUOTE_PAIRS.get(text[token.start]) == text[token.end - 1]
    )


def _next_value(text: str, tokens: list[_ShellToken], index: int) -> tuple[int, int, str] | None:
    if index + 1 >= len(tokens):
        return None
    token = tokens[index + 1]
    if token.value.startswith("-") and not _is_explicitly_quoted(text, token):
        return None
    return _strip_value_wrapper(text, token.start, token.end)


class CommandLineCredentialRecognizer(EntityRecognizer):
    """Recognize literal credentials in selected command-line forms."""

    def __init__(
        self,
        name: str = "CommandLineCredentialRecognizer",
        supported_language: str = "ru",
    ):
        super().__init__(
            supported_entities=["PASSWORD", "AUTH_TOKEN", "SECRET_KEY"],
            name=name,
            supported_language=supported_language,
            version="1.0.0",
        )

    def load(self) -> None:
        """No external assets are required."""

    def analyze(self, text, entities, nlp_artifacts=None):
        requested = set(entities or self.supported_entities)
        results = []
        for segment_match in _SHELL_SEGMENT_RE.finditer(text):
            tokens = _shell_tokens(text, segment_match.start(), segment_match.end())
            command_data = _command_index(tokens)
            if command_data is None:
                continue
            command_index, command = command_data
            results.extend(self._analyze_tokens(text, tokens, command_index, command, requested))
        return EntityRecognizer.remove_duplicates(results)

    def _analyze_tokens(self, text, tokens, command_index, command, requested):
        results = []
        for index in range(command_index + 1, len(tokens)):
            token = tokens[index]
            flag, value = self._flag_value(text, tokens, index, command)
            if flag is None or value is None:
                continue
            start, end, raw_value = value
            if flag in {"curl-auth-user", "httpie-auth"}:
                colon = raw_value.find(":")
                if colon < 0:
                    continue
                start += colon + 1
                raw_value = raw_value[colon + 1 :]
                end = start + len(raw_value)
                results.extend(
                    self._result("PASSWORD", start, end, raw_value, flag, requested)
                )
            elif flag == "curl-auth-header":
                results.extend(self._header_results(raw_value, start, requested))
            else:
                entity_type = "AUTH_TOKEN" if flag == "curl-oauth2-bearer" else "PASSWORD"
                results.extend(
                    self._result(entity_type, start, end, raw_value, flag, requested)
                )
        return results

    def _flag_value(self, text, tokens, index, command):
        token = tokens[index]
        raw = token.value
        long_flag, separator, attached = raw.partition("=")

        long_specs = {
            "--password": "generic-password",
            "--passwd": "generic-password",
            "--pass": "generic-password",
        }
        command_specs = {
            "curl": {
                "--user": "curl-auth-user",
                "--proxy-user": "curl-auth-user",
                "--oauth2-bearer": "curl-oauth2-bearer",
                "--header": "curl-auth-header",
            },
            "wget": {
                "--http-password": "wget-password",
                "--proxy-password": "wget-password",
                "--ftp-password": "wget-password",
            },
            "mysql": {"--password": "mysql-password"},
            "http": {"--auth": "httpie-auth"},
            "https": {"--auth": "httpie-auth"},
            "redis-cli": {"--pass": "redis-password"},
        }
        rule_id = command_specs.get(command, {}).get(long_flag) or long_specs.get(long_flag)
        if rule_id:
            value = _inline_value(text, token, len(long_flag) + 1) if separator else _next_value(text, tokens, index)
            return rule_id, value

        short_specs = {
            ("curl", "-u"): "curl-auth-user",
            ("curl", "-U"): "curl-auth-user",
            ("curl", "-H"): "curl-auth-header",
            ("mysql", "-p"): "mysql-password",
            ("http", "-a"): "httpie-auth",
            ("https", "-a"): "httpie-auth",
            ("redis-cli", "-a"): "redis-password",
        }
        for (expected_command, short_flag), candidate_rule_id in short_specs.items():
            if command != expected_command or not raw.startswith(short_flag):
                continue
            if raw == short_flag:
                return candidate_rule_id, _next_value(text, tokens, index)
            return candidate_rule_id, _inline_value(text, token, len(short_flag))
        return None, None

    def _header_results(self, header: str, source_start: int, requested: set[str]):
        patterns = (
            (
                "AUTH_TOKEN",
                "curl-auth-header",
                re.compile(
                    r"(?i)^Authorization\s*:\s*(?:Basic|Bearer|Api-?Token|Token)?\s*"
                    r"(?P<value>[A-Za-z0-9_=~@.+/-]{8,})$"
                ),
            ),
            (
                "SECRET_KEY",
                "curl-auth-header",
                re.compile(
                    r"(?i)^(?:X-[A-Za-z]+-)?(?:Api-?)?(?:Key|Token)\s*:\s*"
                    r"(?P<value>[A-Za-z0-9_=~@.+/-]{8,})$"
                ),
            ),
        )
        for entity_type, rule_id, pattern in patterns:
            match = pattern.fullmatch(header)
            if not match:
                continue
            start, end = match.span("value")
            value = match.group("value")
            return self._result(
                entity_type,
                source_start + start,
                source_start + end,
                value,
                rule_id,
                requested,
            )
        return []

    def _result(self, entity_type, start, end, value, rule_id, requested):
        if entity_type not in requested or _is_inert_credential_value(value):
            return []
        if entity_type == "PASSWORD" and len(value) < 4:
            return []
        if entity_type != "PASSWORD" and (len(value) < 8 or value.isdigit()):
            return []
        return [
            RecognizerResult(
                entity_type=entity_type,
                start=start,
                end=end,
                score=0.95,
                recognition_metadata=_recognition_metadata(self, rule_id),
            )
        ]


def accepted_rule_ids() -> tuple[str, ...]:
    """Return stable identifiers for all native rules approved in issue #67."""
    recognizer_rules: Iterable[_CredentialRule] = (
        *LoginRecognizer.RULES,
        *PasswordRecognizer.RULES,
        *SecretKeyRecognizer.RULES,
        *AuthTokenRecognizer.RULES,
    )
    native_ids = {rule.rule_id for rule in recognizer_rules}
    native_ids.update({"curl-auth-header", "curl-auth-user"})
    return tuple(sorted(native_ids))
