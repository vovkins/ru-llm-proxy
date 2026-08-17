"""Infrastructure identifier and secret recognizers."""

import base64
import ipaddress
import json
import os
import re

from presidio_analyzer import (
    EntityRecognizer,
    Pattern,
    PatternRecognizer,
    RecognizerResult,
)

from recognizers.credential_rules import LoginRecognizer, PasswordRecognizer


_PRIVATE_IP_NETWORKS = tuple(
    ipaddress.ip_network(network)
    for network in (
        "10.0.0.0/8",
        "172.16.0.0/12",
        "192.168.0.0/16",
        "127.0.0.0/8",
        "169.254.0.0/16",
        "100.64.0.0/10",
        "::1/128",
        "fc00::/7",
        "fe80::/10",
    )
)
_DEFAULT_INTERNAL_DOMAIN_SUFFIXES = (
    "internal",
    "local",
    "lan",
    "corp",
    "corp.local",
    "cluster.local",
    "svc.cluster.local",
)
_HOST_LABEL = r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
_COMMON_NON_SECRET_VALUES = {
    "changeme",
    "example",
    "example.com",
    "false",
    "null",
    "none",
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
_COMMON_NON_HOST_VALUES = _COMMON_NON_SECRET_VALUES | {
    "enabled",
    "disabled",
    "primary",
    "secondary",
}


def _env_flag(name: str, *, default: bool = False) -> bool:
    """Return a boolean environment flag."""
    raw_value = os.getenv(name)
    if raw_value is None:
        return default
    return raw_value.strip().lower() not in {"0", "false", "no", "off", ""}


def _internal_domain_suffixes() -> tuple[str, ...]:
    """Return configured internal domain suffixes."""
    raw_value = os.getenv("PRESIDIO_ANALYZER_INTERNAL_DOMAIN_SUFFIXES")
    if raw_value is None:
        return tuple(sorted(_DEFAULT_INTERNAL_DOMAIN_SUFFIXES, key=len, reverse=True))
    suffixes = [
        suffix.strip().strip(".").lower()
        for suffix in re.split(r"[\s,]+", raw_value)
        if suffix.strip().strip(".")
    ]
    return tuple(sorted(dict.fromkeys(suffixes), key=len, reverse=True))


def _is_internal_ip(ip: ipaddress._BaseAddress) -> bool:
    """Return whether an IP address belongs to an internal-use range."""
    return any(ip in network for network in _PRIVATE_IP_NETWORKS)


def _extract_key_value_value(pattern_text: str) -> str:
    """Extract the right-hand value from a key-value style finding."""
    match = re.search(r"[:=]\s*([\"']?)(?P<value>[^\"'\s,;}\]]+)", pattern_text)
    if not match:
        return pattern_text.strip().strip("\"'")
    return match.group("value").strip().strip("\"'")


def _is_placeholder_secret(value: str) -> bool:
    """Return whether a value is clearly a documentation placeholder."""
    normalized = value.strip().strip("\"'").lower()
    if not normalized:
        return True
    if normalized in _COMMON_NON_SECRET_VALUES:
        return True
    return bool(
        re.fullmatch(
            r"(?:\$\{[^}]+\}|<[^>]+>|\[(?:PII_)?REDACTED(?::[A-Z_]+)?\]|"
            r"(?:your|replace|example|placeholder)(?:[-_][a-z0-9_-]+)?)",
            normalized,
            re.IGNORECASE,
        )
        or normalized.startswith(
            ("your-", "your_", "replace-", "replace_", "example-", "example_")
        )
    )


def _decode_base64url_json(segment: str) -> dict | None:
    """Decode a JWT base64url JSON segment."""
    try:
        padding = "=" * (-len(segment) % 4)
        raw = base64.urlsafe_b64decode(f"{segment}{padding}")
        decoded = json.loads(raw.decode("utf-8"))
    except Exception:
        return None
    return decoded if isinstance(decoded, dict) else None


class InternalIpRecognizer(PatternRecognizer):
    """Recognize valid IP addresses, with an internal-only policy override."""

    PATTERNS = [
        Pattern(
            name="internal_ipv4",
            regex=r"(?<![A-Za-z0-9_.-])(?:\d{1,3}\.){3}\d{1,3}(?![A-Za-z0-9_-]|\.\d)",
            score=0.6,
        ),
        Pattern(
            name="internal_ipv6",
            regex=(
                r"(?<![A-Fa-f0-9:])"
                r"(?:[A-Fa-f0-9]{0,4}:){2,7}[A-Fa-f0-9]{0,4}"
                r"(?:%[A-Za-z0-9_.-]+)?"
                r"(?![A-Fa-f0-9:])"
            ),
            score=0.6,
        ),
    ]

    CONTEXT = [
        "ip",
        "ip address",
        "host",
        "server",
        "endpoint",
        "адрес",
        "сервер",
        "хост",
    ]

    def __init__(
        self,
        name: str = "InternalIpRecognizer",
        supported_language: str = "ru",
        supported_entity: str = "INTERNAL_IP",
    ):
        super().__init__(
            supported_entity=supported_entity,
            patterns=self.PATTERNS,
            context=self.CONTEXT,
            name=name,
            supported_language=supported_language,
        )

    def validate_result(self, pattern_text: str):
        """Keep base score for valid internal IPs."""
        return None

    def invalidate_result(self, pattern_text: str) -> bool:
        """Reject invalid IPs and public IPs unless public detection is enabled."""
        value = pattern_text.strip().strip("[](){}<>,;'\"")
        if "%" in value:
            value = value.split("%", 1)[0]
        try:
            ip = ipaddress.ip_address(value)
        except ValueError:
            return True

        if _is_internal_ip(ip):
            return False
        return not _env_flag(
            "PRESIDIO_ANALYZER_DETECT_PUBLIC_IPS",
            default=True,
        )


class InternalDomainRecognizer(PatternRecognizer):
    """Recognize hostnames under configured internal domain suffixes."""

    CONTEXT = [
        "domain",
        "endpoint",
        "host",
        "hostname",
        "server",
        "домен",
        "сервер",
        "хост",
    ]

    def __init__(
        self,
        name: str = "InternalDomainRecognizer",
        supported_language: str = "ru",
        supported_entity: str = "INTERNAL_DOMAIN",
    ):
        suffixes = _internal_domain_suffixes()
        suffix_pattern = "|".join(re.escape(suffix) for suffix in suffixes)
        patterns = []
        if suffix_pattern:
            patterns.append(
                Pattern(
                    name="internal_domain_suffix",
                    regex=(
                        rf"(?<![@A-Za-z0-9_-])(?:{_HOST_LABEL}\.)+"
                        rf"(?:{suffix_pattern})(?![A-Za-z0-9_-]|\.[A-Za-z0-9])"
                    ),
                    score=0.55,
                )
            )
        super().__init__(
            supported_entity=supported_entity,
            patterns=patterns,
            context=self.CONTEXT,
            name=name,
            supported_language=supported_language,
        )


class HostnameRecognizer(EntityRecognizer):
    """Recognize context-bound single-label hostnames."""

    RULES = (
        (
            "hostname_key_value",
            re.compile(
                r"(?i)(?<![\w.-])(?:host(?:name)?|server|node|service|"
                r"endpoint|хост|сервер|узел|сервис|имя[ \t]+хоста)"
                rf"\s*[:=]\s*[\"']?(?P<value>{_HOST_LABEL})[\"']?"
                r"(?![A-Za-z0-9-]|\.[A-Za-z0-9])"
            ),
            0.75,
        ),
        (
            "hostname_context",
            re.compile(
                r"(?i)(?<![\w.-])(?:хост|сервер|узел|реплика)\s+"
                rf"(?P<value>{_HOST_LABEL})(?![A-Za-z0-9-]|\.[A-Za-z0-9])"
            ),
            0.75,
        ),
        (
            "hostname_ssh_target",
            re.compile(
                r"(?i)(?<![\w])ssh\s+(?:к\s+)?"
                rf"(?:[A-Za-z][A-Za-z0-9._-]{{2,63}}@)?"
                rf"(?P<value>{_HOST_LABEL})(?![A-Za-z0-9-]|\.[A-Za-z0-9])"
            ),
            0.85,
        ),
    )

    def __init__(
        self,
        name: str = "HostnameRecognizer",
        supported_language: str = "ru",
        supported_entity: str = "HOSTNAME",
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
        """Return value-only hostname results from bounded contexts."""
        entity_type = self.supported_entities[0]
        if entities and entity_type not in entities:
            return []

        results = []
        for rule_id, pattern, score in self.RULES:
            for match in pattern.finditer(text):
                start, end = match.span("value")
                value = match.group("value")
                if not self._accept_value(value):
                    continue
                results.append(
                    RecognizerResult(
                        entity_type=entity_type,
                        start=start,
                        end=end,
                        score=score,
                        recognition_metadata={
                            RecognizerResult.RECOGNIZER_NAME_KEY: self.name,
                            RecognizerResult.RECOGNIZER_IDENTIFIER_KEY: self.id,
                            "deterministic_rule_id": rule_id,
                        },
                    )
                )
        return EntityRecognizer.remove_duplicates(results)

    @staticmethod
    def _accept_value(value: str) -> bool:
        normalized = value.lower()
        if normalized in _COMMON_NON_HOST_VALUES:
            return False
        return (
            normalized == "localhost"
            or "-" in value
            or any(char.isdigit() for char in value)
        )


class CredentialUrlRecognizer(PatternRecognizer):
    """Recognize credential-bearing database or service URLs."""

    PATTERNS = [
        Pattern(
            name="credential_url",
            regex=(
                r"\b(?:(?:jdbc:)?(?:postgres(?:ql)?|mysql|mariadb|"
                r"mongodb(?:\+srv)?|redis|rediss|amqp|amqps|kafka|"
                r"clickhouse|elasticsearch|mssql|sqlserver|oracle)|"
                r"https?|grpcs?)://"
                r"[^:\s/@]+:[^@\s]+@[^\"'\s<>\])},]+"
            ),
            score=0.9,
        ),
    ]

    CONTEXT = [
        "database",
        "connection string",
        "dsn",
        "url",
        "uri",
        "db",
        "база данных",
        "строка подключения",
    ]

    def __init__(
        self,
        name: str = "CredentialUrlRecognizer",
        supported_language: str = "ru",
        supported_entity: str = "DB_URL",
    ):
        super().__init__(
            supported_entity=supported_entity,
            patterns=self.PATTERNS,
            context=self.CONTEXT,
            name=name,
            supported_language=supported_language,
        )


class JwtRecognizer(PatternRecognizer):
    """Recognize JWT-like values with decodable JSON header and payload."""

    PATTERNS = [
        Pattern(
            name="jwt",
            regex=(
                r"(?<![A-Za-z0-9_-])"
                r"eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"
                r"(?![A-Za-z0-9_-])"
            ),
            score=0.85,
        ),
    ]

    CONTEXT = ["jwt", "token", "authorization", "oidc", "bearer"]

    def __init__(
        self,
        name: str = "JwtRecognizer",
        supported_language: str = "ru",
        supported_entity: str = "JWT",
    ):
        super().__init__(
            supported_entity=supported_entity,
            patterns=self.PATTERNS,
            context=self.CONTEXT,
            name=name,
            supported_language=supported_language,
        )

    def validate_result(self, pattern_text: str):
        """Keep base score for structurally valid JWTs."""
        return None

    def invalidate_result(self, pattern_text: str) -> bool:
        """Reject JWT-like strings that do not decode to JSON header and payload."""
        parts = pattern_text.split(".")
        if len(parts) != 3:
            return True
        header = _decode_base64url_json(parts[0])
        payload = _decode_base64url_json(parts[1])
        return not (
            header
            and payload
            and isinstance(header.get("alg"), str)
            and ("sub" in payload or "iss" in payload or "aud" in payload)
        )


class BearerTokenRecognizer(PatternRecognizer):
    """Recognize bearer tokens in Authorization-style snippets."""

    PATTERNS = [
        Pattern(
            name="bearer_token",
            regex=(
                r"\b(?:Authorization\s*:\s*)?Bearer\s+"
                r"[A-Za-z0-9._~+/=-]{20,}\b"
            ),
            score=0.9,
        ),
    ]

    CONTEXT = ["authorization", "bearer", "token", "auth"]

    def __init__(
        self,
        name: str = "BearerTokenRecognizer",
        supported_language: str = "ru",
        supported_entity: str = "BEARER_TOKEN",
    ):
        super().__init__(
            supported_entity=supported_entity,
            patterns=self.PATTERNS,
            context=self.CONTEXT,
            name=name,
            supported_language=supported_language,
        )

    def invalidate_result(self, pattern_text: str) -> bool:
        """Reject obvious placeholder bearer values."""
        value = pattern_text.rsplit(None, 1)[-1]
        return _is_placeholder_secret(value)


class PrivateKeyRecognizer(PatternRecognizer):
    """Recognize PEM private key blocks."""

    PATTERNS = [
        Pattern(
            name="private_key_block",
            regex=(
                r"-----BEGIN (?:[A-Z0-9]+[ \t]+)*PRIVATE KEY(?:[ \t]+BLOCK)?-----"
                r"[\s\S]{0,4096}?"
                r"-----END (?:[A-Z0-9]+[ \t]+)*PRIVATE KEY(?:[ \t]+BLOCK)?-----"
            ),
            score=0.95,
        ),
    ]

    CONTEXT = ["private key", "pem", "key", "секретный ключ", "приватный ключ"]

    def __init__(
        self,
        name: str = "PrivateKeyRecognizer",
        supported_language: str = "ru",
        supported_entity: str = "PRIVATE_KEY",
    ):
        super().__init__(
            supported_entity=supported_entity,
            patterns=self.PATTERNS,
            context=self.CONTEXT,
            name=name,
            supported_language=supported_language,
        )


class ApiKeyRecognizer(PatternRecognizer):
    """Recognize high-confidence provider API key values."""

    PATTERNS = [
        Pattern(
            name="provider_api_key",
            regex=(
                r"(?<![A-Za-z0-9_-])(?:"
                r"sk-ant-[A-Za-z0-9_-]{20,}|"
                r"sk-[A-Za-z0-9_-]{20,}|"
                r"AIza[0-9A-Za-z_-]{20,}"
                r")(?![A-Za-z0-9_-])"
            ),
            score=0.9,
        ),
    ]

    CONTEXT = ["api key", "token", "secret", "credential", "ключ", "токен"]

    def __init__(
        self,
        name: str = "ApiKeyRecognizer",
        supported_language: str = "ru",
        supported_entity: str = "API_KEY",
    ):
        super().__init__(
            supported_entity=supported_entity,
            patterns=self.PATTERNS,
            context=self.CONTEXT,
            name=name,
            supported_language=supported_language,
        )

    def invalidate_result(self, pattern_text: str) -> bool:
        """Reject documentation placeholders and low-signal token examples."""
        value = _extract_key_value_value(pattern_text)
        return _is_placeholder_secret(value)
