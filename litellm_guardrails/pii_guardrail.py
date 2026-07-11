"""Custom LiteLLM guardrail for Russian PII masking via Presidio."""

import asyncio
import inspect
import os
import uuid
import json
import logging
import re
import time
from typing import Any, Iterable, Optional, Union
from weakref import WeakKeyDictionary

import httpx
import litellm
from litellm.integrations.custom_guardrail import CustomGuardrail
from litellm.proxy._types import ProxyException, UserAPIKeyAuth
from litellm.caching.caching import DualCache

from litellm_guardrails.dictionary_policy import (
    DictionaryPolicyAmbiguousRequestError,
    DictionaryPolicyConfigError,
    DictionarySubstitutionPolicy,
    DictionarySubstitutionResult,
)

try:
    from fastapi import HTTPException
except ImportError:  # pragma: no cover - local lightweight test env without FastAPI.
    class HTTPException(Exception):
        def __init__(self, status_code: int, detail: Any):
            super().__init__(detail)
            self.status_code = status_code
            self.detail = detail

logger = logging.getLogger(__name__)

try:
    from prometheus_client import Counter, Histogram
except Exception:
    Counter = None
    Histogram = None


class _NoopMetric:
    """Fallback metric used when prometheus_client is unavailable."""

    def labels(self, *args, **kwargs):
        return self

    def inc(self, amount: float = 1):
        return None

    def observe(self, amount: float):
        return None


class _StreamingPlaceholderReplacer:
    """Chunk-safe placeholder replacement for independently streamed text fields."""

    def __init__(self, mapping: dict[str, str]):
        self._mapping = mapping
        self._placeholders = sorted(mapping, key=len, reverse=True)
        self._pending: dict[tuple[int, str], str] = {}

    def push(self, key: tuple[int, str], text: str) -> str:
        """Return text safe to emit now, keeping possible placeholder prefixes."""
        combined = self._pending.get(key, "") + text
        emit, pending = self._split_safe_prefix(combined)
        self._pending[key] = pending
        return self._replace(emit)

    def flush(self, key: tuple[int, str]) -> str:
        """Flush pending text for one stream field."""
        pending = self._pending.pop(key, "")
        return self._replace(pending)

    def flush_choice(self, choice_index: int) -> dict[str, str]:
        """Flush all pending fields for one choice."""
        flushed: dict[str, str] = {}
        for key in list(self._pending):
            key_choice_index, field = key
            if key_choice_index != choice_index:
                continue
            value = self.flush(key)
            if value:
                flushed[field] = flushed.get(field, "") + value
        return flushed

    def flush_all(self) -> dict[tuple[int, str], str]:
        """Flush all remaining pending fields."""
        flushed: dict[tuple[int, str], str] = {}
        for key in list(self._pending):
            value = self.flush(key)
            if value:
                flushed[key] = value
        return flushed

    def _split_safe_prefix(self, text: str) -> tuple[str, str]:
        keep = 0
        for length in range(1, len(text) + 1):
            suffix = text[-length:]
            if any(
                suffix != placeholder and placeholder.startswith(suffix)
                for placeholder in self._placeholders
            ):
                keep = length
        if keep == 0:
            return text, ""
        return text[:-keep], text[-keep:]

    def _replace(self, text: str) -> str:
        for placeholder in self._placeholders:
            text = text.replace(placeholder, self._mapping[placeholder])
        return text


def _build_metric(factory, *args, **kwargs):
    """Create a Prometheus metric or a no-op replacement."""
    if factory is None:
        return _NoopMetric()
    try:
        return factory(*args, **kwargs)
    except ValueError:
        logger.warning("Prometheus metric already registered, using no-op for %s", args[0])
        return _NoopMetric()


PII_PRE_CALLS = _build_metric(
    Counter,
    "ru_pii_guardrail_pre_calls",
    "PII guardrail pre-call requests.",
    ["result"],
)
PII_POST_CALLS = _build_metric(
    Counter,
    "ru_pii_guardrail_post_calls",
    "PII guardrail post-call requests.",
    ["result"],
)
PII_ENTITIES_DETECTED = _build_metric(
    Counter,
    "ru_pii_guardrail_entities_detected",
    "PII entities masked by entity type.",
    ["entity_type"],
)
PII_FAIL_OPEN = _build_metric(
    Counter,
    "ru_pii_guardrail_fail_open",
    "PII guardrail failures handled in fail-open mode.",
    ["operation"],
)
PII_FAIL_CLOSED = _build_metric(
    Counter,
    "ru_pii_guardrail_fail_closed",
    "PII guardrail failures handled in fail-closed mode.",
    ["operation"],
)
PII_ANALYZER_LATENCY = _build_metric(
    Histogram,
    "ru_pii_guardrail_analyzer_latency_seconds",
    "Latency of Presidio Analyzer calls made by the PII guardrail.",
    buckets=(0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30),
)
PII_REDIS_LATENCY = _build_metric(
    Histogram,
    "ru_pii_guardrail_redis_latency_seconds",
    "Latency of Redis operations made by the PII guardrail.",
    ["operation"],
    buckets=(0.001, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5),
)
PII_MAPPING_SIZE = _build_metric(
    Histogram,
    "ru_pii_guardrail_mapping_size",
    "Number of placeholder mappings saved for a masked request.",
    buckets=(1, 2, 5, 10, 25, 50, 100, 250),
)
PII_BLOCKED_ENTITIES = _build_metric(
    Counter,
    "ru_pii_guardrail_blocked",
    "PII entities blocked by entity type.",
    ["entity_type"],
)
PRE_EGRESS_POLICY_BLOCKED = _build_metric(
    Counter,
    "ru_pre_egress_policy_blocked",
    "Pre-egress policy blocks by category.",
    ["category"],
)
FINAL_PAYLOAD_LEAK_CHECK_BLOCKED = _build_metric(
    Counter,
    "ru_final_payload_leak_check_blocked",
    "Final provider-bound payload leak-check blocks by rule id.",
    ["rule_id"],
)
REGULATED_TOPIC_POLICY_BLOCKED = _build_metric(
    Counter,
    "ru_regulated_topic_policy_blocked",
    "Regulated-topic policy blocks by bounded category and rule id.",
    ["category", "rule_id"],
)
SYNTHETIC_PII_ALLOWLIST_HITS = _build_metric(
    Counter,
    "ru_synthetic_pii_allowlist_hits",
    "Synthetic/test PII allowlist hits by bounded rule id and entity type.",
    ["rule_id", "entity_type"],
)
DICTIONARY_SUBSTITUTIONS_APPLIED = _build_metric(
    Counter,
    "ru_dictionary_substitution_applied",
    "Dictionary substitutions applied by bounded rule id.",
    ["rule_id"],
)
DICTIONARY_SUBSTITUTION_MAPPING_SIZE = _build_metric(
    Histogram,
    "ru_dictionary_substitution_mapping_size",
    "Number of reversible dictionary mappings saved for a substituted request.",
    buckets=(1, 2, 5, 10, 25, 50, 100, 250),
)

# Presidio Analyzer service URL from environment
PRESIDIO_ANALYZER_URL = os.getenv("PRESIDIO_ANALYZER_URL", "http://presidio-analyzer:5001")
DEFAULT_DICTIONARY_SUBSTITUTIONS_FILE = os.getenv(
    "DICTIONARY_SUBSTITUTIONS_FILE",
    os.path.join(
        os.path.dirname(__file__),
        "dictionary-substitutions.default.json",
    ),
)
DICTIONARY_SUBSTITUTIONS_ENABLED = os.getenv(
    "DICTIONARY_SUBSTITUTIONS_ENABLED",
    "true",
)
DICTIONARY_SUBSTITUTIONS_FILE = DEFAULT_DICTIONARY_SUBSTITUTIONS_FILE
DICTIONARY_SUBSTITUTIONS_JSON = os.getenv("DICTIONARY_SUBSTITUTIONS_JSON", "")
DICTIONARY_SUBSTITUTIONS_FAILURE_MODE = os.getenv(
    "DICTIONARY_SUBSTITUTIONS_FAILURE_MODE",
    "fail_closed",
)

FAILURE_MODES = {"fail_open", "fail_closed"}
POLICY_MODES = {"mask", "block"}
PRE_EGRESS_POLICY_MODES = {"block", "off"}
FINAL_PAYLOAD_LEAK_CHECK_MODES = {"block", "off"}
REGULATED_TOPIC_POLICY_MODES = {"block", "off"}
REGULATED_TOPIC_POLICY_ACTIONS = {"block"}
SYNTHETIC_PII_ALLOWLIST_MODES = {"allow", "off"}
SYNTHETIC_PII_ALLOWLIST_POLICIES = {"pii"}
SYNTHETIC_PII_ALLOWLIST_SAFE_PATTERN_MARKERS = (
    "EXAMPLE\\.TEST",
    "EXAMPLE\\.COM",
    "EXAMPLE\\.ORG",
    "EXAMPLE\\.NET",
    "\\.TEST",
    "TEST_",
    "RU_PROXY_",
    "SYNTHETIC_",
    "CANARY_",
)
SYNTHETIC_PII_ALLOWLIST_MAX_PATTERN_LENGTH = 256
FINAL_PAYLOAD_LEAK_CHECK_PROVIDER_BOUND_FIELDS = (
    "tools",
    "tool_choice",
    "functions",
    "function_call",
    "prediction",
    "response_format",
    "text",
    "extra_body",
    "stop",
    "stop_sequences",
    "prompt_cache_key",
    "safety_identifier",
    "web_search_options",
    "user",
    "metadata",
)
FINAL_PAYLOAD_LEAK_CHECK_PROVIDER_BOUND_REQUEST_FIELDS = (
    "messages",
    "input",
    "instructions",
    "system",
)
DEFAULT_PII_MAPPING_TTL_SECONDS = 3600
PII_BLOCKED_MESSAGE = "Request contains personal data and was blocked by PII policy."
PRE_EGRESS_POLICY_BLOCKED_MESSAGE = (
    "Request contains configuration or log data and was blocked by pre-egress policy."
)
FINAL_PAYLOAD_LEAK_CHECK_BLOCKED_MESSAGE = (
    "Request contains a confirmed raw leak marker and was blocked before provider egress."
)
REGULATED_TOPIC_POLICY_BLOCKED_MESSAGE = (
    "Request contains regulated internal compliance content and was blocked by regulated-topic policy."
)
PII_REQUEST_ID_METADATA_KEY = "pii_request_id"
PII_STREAMING_RESTORATION_DONE_METADATA_KEY = "pii_streaming_restoration_done"
ANALYZER_OVERLOADED_MESSAGE = "PII guardrail analyzer overloaded"

_ENV_SECRET_KEY_PATTERN = (
    r"DATABASE_URL|REDIS_URL|AWS_ACCESS_KEY_ID|AWS_SECRET_ACCESS_KEY|"
    r"PGPASSWORD|MYSQL_PWD|DOCKER_AUTH_CONFIG|"
    r"SECRET_KEY|JWT_SECRET|"
    r"(?:[A-Z][A-Z0-9_]*_)?(?:"
    r"API_KEY|TOKEN|SECRET|PASSWORD|PRIVATE_KEY|MASTER_KEY|SALT_KEY|"
    r"PASS|PWD|DSN|URI|CONNECTION_STRING|AUTH_CONFIG"
    r")(?:_[0-9]+)?"
)
_ENV_SECRET_ASSIGNMENT_RE = re.compile(
    rf"(?m)(?:^[ \t]*(?:-[ \t]*)?(?:export[ \t]+)?[\"']?|[\[{{,][ \t]*[\"']?)"
    rf"(?:{_ENV_SECRET_KEY_PATTERN})[\"']?[ \t]*=[ \t]*[\"']?\S+"
)
_ENV_SECRET_YAML_MAPPING_RE = re.compile(
    rf"(?m)(?:^[ \t-]*[\"']?|[\[{{,][ \t]*[\"']?)"
    rf"(?:{_ENV_SECRET_KEY_PATTERN})[\"']?[ \t]*:[ \t]*[\"']?\S+"
)
_ENV_SECRET_KEY_RE = re.compile(rf"^(?:{_ENV_SECRET_KEY_PATTERN})$")
_ENV_CREDENTIAL_URL_RE = re.compile(
    r"(?m)(?:^[ \t]*(?:-[ \t]*)?(?:export[ \t]+)?[\"']?|[\[{,][ \t]*[\"']?)"
    r"[A-Z][A-Z0-9_]*"
    r"(?:_URL|_URI|_DSN|_CONNECTION_STRING)?[ \t]*=[ \t]*"
    r"[A-Za-z][A-Za-z0-9+.-]*://[^:\s/@]+:[^@\s]+@\S+"
)
_ENV_CREDENTIAL_URL_YAML_MAPPING_RE = re.compile(
    r"(?m)(?:^[ \t-]*[\"']?|[\[{,][ \t]*[\"']?)"
    r"[A-Z][A-Z0-9_]*"
    r"(?:_URL|_URI|_DSN|_CONNECTION_STRING)?[ \t]*:[ \t]*"
    r"[A-Za-z][A-Za-z0-9+.-]*://[^:\s/@]+:[^@\s]+@\S+"
)
_RAW_CREDENTIAL_URL_RE = re.compile(
    r"\b[A-Za-z][A-Za-z0-9+.-]*://[^:\s/@]+:[^@\s]+@\S+"
)
_ACCESS_LOG_RE = re.compile(
    r'(?m)^\S+\s+\S+\s+\S+\s+\[[^\]\n]+\]\s+"'
    r"(?:GET|POST|PUT|DELETE|PATCH|HEAD|OPTIONS)\s+\S+\s+HTTP/\d(?:\.\d)?"
    r'"\s+\d{3}\b'
)
_STACK_TRACE_RE = re.compile(
    r"(?m)^(?:Traceback \(most recent call last\):|"
    r"\s+at\s+(?:\S+\s+)?\(?[^)\n]+:\d+(?::\d+)?\)?|"
    r'\s+File "[^"\n]+", line \d+, in \w+)'
)
_RFC3164_SYSLOG_PREFIX_RE = (
    r"[A-Z][a-z]{2}\s+\d{1,2}\s+(?:\d{2}:\d{2}:\d{2}\s+)?\S+\s+"
)
_RFC3339_TIMESTAMP_RE = (
    r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}"
    r"(?:\.\d{1,9})?(?:Z|[+-]\d{2}:\d{2})"
)
_RFC3339_SYSLOG_PREFIX_RE = rf"{_RFC3339_TIMESTAMP_RE}\s+\S+\s+"
_SYSLOG_PREFIX_RE = (
    rf"(?:{_RFC3164_SYSLOG_PREFIX_RE}|{_RFC3339_SYSLOG_PREFIX_RE})"
)
_AUTH_LOG_RE = re.compile(
    r"(?im)^(?:"
    rf"{_SYSLOG_PREFIX_RE}(?:sshd\[\d+\]:\s+)?(?:failed|accepted) password\b"
    r".*\bfrom\s+\S+\s+port\s+\d+\b|"
    r"sshd\[\d+\]:\s+(?:failed|accepted) password\b"
    r".*\bfrom\s+\S+\s+port\s+\d+\b|"
    rf"{_SYSLOG_PREFIX_RE}.*\bpam_unix\b.*\bauthentication failure\b|"
    rf"{_SYSLOG_PREFIX_RE}sudo:\s+.*\bCOMMAND="
    r")"
)
_JSON_STACKTRACE_FIELD_RE = re.compile(
    r"(?i)\b(?:stack|stacktrace|traceback|exception|error)\b"
)
_PRIVATE_KEY_MARKER_RE = re.compile(
    r"(?i)-----BEGIN (?:[A-Z0-9]+[ \t]+)*PRIVATE KEY(?:[ \t]+BLOCK)?-----"
)
_BEARER_TOKEN_RE = re.compile(
    r"(?i)\bAuthorization\s*:\s*Bearer\s+[A-Za-z0-9._~+/=-]{20,}\b|"
    r"\bBearer\s+[A-Za-z0-9._~+/=-]{20,}\b"
)
_JWT_TOKEN_RE = re.compile(
    r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b"
)
_PROVIDER_KEY_RE = re.compile(
    r"\b(?:sk-ant-[A-Za-z0-9_-]{20,}|sk-[A-Za-z0-9_-]{20,})\b"
)
_AML_CFT_TOPIC_RE = re.compile(
    r"(?iu)(?:"
    r"\bAML\b|\bCFT\b|anti[-\s]?money[-\s]?laundering|"
    r"counter[-\s]?terror(?:ism)?[-\s]?financ(?:e|ing)|"
    r"ПОД\s*/\s*ФТ|ПОД[-\s]?ФТ|"
    r"противодейств\w*\s+легализац\w*|"
    r"отмыван\w*\s+доход\w*|"
    r"финансирован\w*\s+террор"
    r")"
)
_REGULATED_INTERNAL_CONTEXT_RE = re.compile(
    r"(?iu)\b(?:"
    r"internal|non[-\s]?public|confidential|restricted|"
    r"внутренн\w*|непубличн\w*|конфиденциальн\w*|служебн\w*|закрыт\w*"
    r")\b"
)
_REGULATED_CONTROL_CONTEXT_RE = re.compile(
    r"(?iu)\b(?:"
    r"controls?|procedures?|polic(?:y|ies)|rules?|logic|algorithm|"
    r"thresholds?|scenarios?|typolog(?:y|ies)|risk[-\s]?scor(?:e|ing)|playbooks?|"
    r"регламент\w*|методик\w*|процедур\w*|контрол\w*|правил\w*|"
    r"логик\w*|алгоритм\w*|порог\w*|сценари\w*|типолог\w*|"
    r"скоринг\w*|риск[-\s]?модел\w*"
    r")\b"
)
_SANCTIONS_TOPIC_RE = re.compile(
    r"(?iu)(?:"
    r"sanctions?\s+screening|\bsanctions?\b|"
    r"санкционн\w+\s+скрининг\w*|"
    r"провер\w+\s+по\s+санкционн\w+\s+списк\w*|"
    r"санкци\w+"
    r")"
)
_WATCHLIST_MATCHING_CONTEXT_RE = re.compile(
    r"(?iu)(?:"
    r"watch[-\s]?list|blacklist|deny[-\s]?list|stop[-\s]?list|\bSDN\b|\bOFAC\b|"
    r"санкционн\w+\s+списк\w*|стоп[-\s]?лист|черн\w+\s+список|"
    r"matching|fuzzy[-\s]?match|матчинг\w*|нечетк\w+\s+сравнен\w*"
    r")"
)
_TRANSACTION_MONITORING_TOPIC_RE = re.compile(
    r"(?iu)(?:"
    r"transaction\s+monitoring|мониторинг\s+операц\w*|"
    r"мониторинг\s+транзакц\w*|транзакционн\w+\s+мониторинг"
    r")"
)
_THRESHOLD_SCENARIO_CONTEXT_RE = re.compile(
    r"(?iu)\b(?:"
    r"thresholds?|triggers?|rules?|scenarios?|typolog(?:y|ies)|alerts?|red[-\s]?flags?|"
    r"порог\w*|триггер\w*|правил\w*|сценари\w*|типолог\w*|"
    r"алерт\w*|сигнал\w*|красн\w+\s+флаг\w*"
    r")\b"
)
_SUSPICIOUS_ACTIVITY_TOPIC_RE = re.compile(
    r"(?iu)(?:"
    r"suspicious\s+activity|\bSAR\b|\bSTR\b|"
    r"подозрительн\w+\s+операц\w*|сомнительн\w+\s+операц\w*|"
    r"сообщени\w+\s+о\s+подозрительн\w+"
    r")"
)
_INVESTIGATION_PLAYBOOK_CONTEXT_RE = re.compile(
    r"(?iu)(?:"
    r"investigation\s+playbook|case\s+workflow|escalation\s+matrix|"
    r"decision\s+tree|step[-\s]?by[-\s]?step|"
    r"плейбук|маршрут\s+эскалац\w*|матриц\w+\s+эскалац\w*|"
    r"дерев\w+\s+решен\w*|порядок\s+расследован\w*|шаг\w+\s+расследован\w*"
    r")"
)
_BYPASS_SENSITIVE_CONTEXT_RE = re.compile(
    r"(?iu)(?:"
    r"bypass|evad(?:e|ing)|avoid\s+(?:detection|triggering|alerts?)|work\s+around|"
    r"обойти|обход\w*|уклонит\w*|не\s+попасть|избежат\w+\s+срабатыван\w*|"
    r"скрыть\s+от|как\s+пройти\s+провер"
    r")"
)
REGULATED_TOPIC_POLICY_DEFAULT_RULES = (
    {
        "category": "aml_cft",
        "rule_id": "aml_cft_internal_controls",
        "action": "block",
    },
    {
        "category": "sanctions_screening",
        "rule_id": "sanctions_watchlist_matching",
        "action": "block",
    },
    {
        "category": "transaction_monitoring",
        "rule_id": "transaction_monitoring_thresholds",
        "action": "block",
    },
    {
        "category": "suspicious_activity_investigation",
        "rule_id": "suspicious_activity_playbook",
        "action": "block",
    },
    {
        "category": "bypass_sensitive_procedure",
        "rule_id": "compliance_bypass_procedure",
        "action": "block",
    },
)


class AnalyzerOverloadedError(RuntimeError):
    """Raised when the Presidio Analyzer rejects work due to capacity policy."""

    def __init__(self, reason: str = "unknown"):
        super().__init__(f"Presidio Analyzer overloaded: {reason}")
        self.reason = reason


def _get_int_env(name: str, default: int) -> int:
    """Read integer environment variable with a safe fallback."""
    raw_value = os.getenv(name)
    if raw_value is None:
        return default
    try:
        value = int(raw_value)
    except ValueError:
        logger.warning("Invalid %s=%r, falling back to %s", name, raw_value, default)
        return default
    return _normalize_positive_int(name, value, default)


def _normalize_positive_int(name: str, value: int, default: int) -> int:
    """Return value when positive, otherwise log and return default."""
    if value > 0:
        return value
    logger.warning("Invalid %s=%r, falling back to %s", name, value, default)
    return default


def _get_float_env(name: str, default: float) -> float:
    """Read float environment variable with a safe fallback."""
    raw_value = os.getenv(name)
    if raw_value is None:
        return default
    try:
        value = float(raw_value)
    except ValueError:
        logger.warning("Invalid %s=%r, falling back to %s", name, raw_value, default)
        return default
    return _normalize_positive_float(name, value, default)


def _normalize_positive_float(name: str, value: float, default: float) -> float:
    """Return value when positive, otherwise log and return default."""
    if value > 0:
        return value
    logger.warning("Invalid %s=%r, falling back to %s", name, value, default)
    return default


# Redis for storing PII mappings (for unmasking responses)
REDIS_URL = os.getenv("REDIS_URL", "redis://redis:6379")
PII_GUARDRAIL_REDIS_MAX_CONNECTIONS = _get_int_env(
    "PII_GUARDRAIL_REDIS_MAX_CONNECTIONS",
    20,
)
PII_GUARDRAIL_REDIS_SOCKET_CONNECT_TIMEOUT_SECONDS = _get_float_env(
    "PII_GUARDRAIL_REDIS_SOCKET_CONNECT_TIMEOUT_SECONDS",
    1.0,
)
PII_GUARDRAIL_REDIS_SOCKET_TIMEOUT_SECONDS = _get_float_env(
    "PII_GUARDRAIL_REDIS_SOCKET_TIMEOUT_SECONDS",
    2.0,
)
PII_GUARDRAIL_ANALYZER_TIMEOUT_SECONDS = _get_float_env(
    "PII_GUARDRAIL_ANALYZER_TIMEOUT_SECONDS",
    30.0,
)
PII_GUARDRAIL_ANALYZER_CONNECT_TIMEOUT_SECONDS = _get_float_env(
    "PII_GUARDRAIL_ANALYZER_CONNECT_TIMEOUT_SECONDS",
    5.0,
)
PII_GUARDRAIL_ANALYZER_MAX_CONNECTIONS = _get_int_env(
    "PII_GUARDRAIL_ANALYZER_MAX_CONNECTIONS",
    20,
)
PII_GUARDRAIL_ANALYZER_MAX_KEEPALIVE_CONNECTIONS = _get_int_env(
    "PII_GUARDRAIL_ANALYZER_MAX_KEEPALIVE_CONNECTIONS",
    10,
)
PII_MAPPING_TTL_SECONDS = _get_int_env(
    "PII_MAPPING_TTL_SECONDS",
    DEFAULT_PII_MAPPING_TTL_SECONDS,
)
PII_GUARDRAIL_FAILURE_MODE = os.getenv("PII_GUARDRAIL_FAILURE_MODE", "fail_open")
PII_GUARDRAIL_MODE = os.getenv("PII_GUARDRAIL_MODE", "mask")
PRE_EGRESS_POLICY_MODE = os.getenv("PRE_EGRESS_POLICY_MODE", "block")
FINAL_PAYLOAD_LEAK_CHECK_MODE = os.getenv("FINAL_PAYLOAD_LEAK_CHECK_MODE", "block")
REGULATED_TOPIC_POLICY_MODE = os.getenv("REGULATED_TOPIC_POLICY_MODE", "off")
REGULATED_TOPIC_POLICY_EXTRA_RULES_JSON = os.getenv(
    "REGULATED_TOPIC_POLICY_EXTRA_RULES_JSON",
    "",
)
SYNTHETIC_PII_ALLOWLIST_MODE = os.getenv("SYNTHETIC_PII_ALLOWLIST_MODE", "off")
SYNTHETIC_PII_ALLOWLIST_JSON = os.getenv("SYNTHETIC_PII_ALLOWLIST_JSON", "[]")
FINAL_PAYLOAD_LEAK_CHECK_CANARIES = tuple(
    token.strip()
    for token in re.split(r"[\n,]", os.getenv("FINAL_PAYLOAD_LEAK_CHECK_CANARIES", ""))
    if token.strip()
)
_REDIS_CLIENTS_BY_LOOP = WeakKeyDictionary()
_ANALYZER_HTTP_CLIENTS_BY_LOOP = WeakKeyDictionary()


def _unique_clients(clients: list[Any]) -> list[Any]:
    """Return clients once even if the same object appears in multiple loops."""
    unique = []
    seen = set()
    for client in clients:
        marker = id(client)
        if marker in seen:
            continue
        seen.add(marker)
        unique.append(client)
    return unique


def _get_shared_redis_client():
    """Return a per-event-loop Redis client shared by guardrail instances."""
    loop = asyncio.get_running_loop()
    client = _REDIS_CLIENTS_BY_LOOP.get(loop)
    if client is None:
        import redis.asyncio as aioredis

        client = aioredis.from_url(
            REDIS_URL,
            decode_responses=True,
            max_connections=PII_GUARDRAIL_REDIS_MAX_CONNECTIONS,
            socket_connect_timeout=(
                PII_GUARDRAIL_REDIS_SOCKET_CONNECT_TIMEOUT_SECONDS
            ),
            socket_timeout=PII_GUARDRAIL_REDIS_SOCKET_TIMEOUT_SECONDS,
        )
        _REDIS_CLIENTS_BY_LOOP[loop] = client
    return client


def _get_shared_analyzer_http_client() -> httpx.AsyncClient:
    """Return a per-event-loop HTTP client shared by guardrail instances."""
    loop = asyncio.get_running_loop()
    client = _ANALYZER_HTTP_CLIENTS_BY_LOOP.get(loop)
    if client is None:
        client = httpx.AsyncClient(
            timeout=httpx.Timeout(
                timeout=PII_GUARDRAIL_ANALYZER_TIMEOUT_SECONDS,
                connect=PII_GUARDRAIL_ANALYZER_CONNECT_TIMEOUT_SECONDS,
            ),
            limits=httpx.Limits(
                max_connections=PII_GUARDRAIL_ANALYZER_MAX_CONNECTIONS,
                max_keepalive_connections=(
                    PII_GUARDRAIL_ANALYZER_MAX_KEEPALIVE_CONNECTIONS
                ),
            ),
        )
        _ANALYZER_HTTP_CLIENTS_BY_LOOP[loop] = client
    return client


async def _maybe_await(value: Any) -> None:
    """Await async close results and ignore synchronous close results."""
    if inspect.isawaitable(value):
        await value


async def _close_client(
    client: Any,
    *,
    operation: str,
    close_connection_pool: bool = False,
) -> None:
    """Close a dependency client without leaking cleanup errors into shutdown."""
    close = getattr(client, "aclose", None)
    if callable(close):
        try:
            if close_connection_pool:
                await _maybe_await(close(close_connection_pool=True))
            else:
                await _maybe_await(close())
            return
        except TypeError:
            try:
                await _maybe_await(close())
            except Exception as exc:
                _safe_log(
                    logging.WARNING,
                    "pii_guardrail_dependency_client_close_failed",
                    operation=operation,
                    error_type=type(exc).__name__,
                )
            return
        except Exception as exc:
            _safe_log(
                logging.WARNING,
                "pii_guardrail_dependency_client_close_failed",
                operation=operation,
                error_type=type(exc).__name__,
            )
            return

    close = getattr(client, "close", None)
    if callable(close):
        try:
            await _maybe_await(close())
        except Exception as exc:
            _safe_log(
                logging.WARNING,
                "pii_guardrail_dependency_client_close_failed",
                operation=operation,
                error_type=type(exc).__name__,
            )


async def close_guardrail_dependency_clients() -> None:
    """Close shared guardrail dependency clients and clear process-local caches."""
    redis_clients = _unique_clients(list(_REDIS_CLIENTS_BY_LOOP.values()))
    analyzer_clients = _unique_clients(list(_ANALYZER_HTTP_CLIENTS_BY_LOOP.values()))
    _REDIS_CLIENTS_BY_LOOP.clear()
    _ANALYZER_HTTP_CLIENTS_BY_LOOP.clear()

    for client in analyzer_clients:
        await _close_client(client, operation="analyzer_http_client")
    for client in redis_clients:
        await _close_client(
            client,
            operation="redis_client",
            close_connection_pool=True,
        )

def _safe_log(level: int, event: str, **fields) -> None:
    """Write structured logs without prompt text or raw PII values."""
    logger.log(
        level,
        json.dumps(
            {"event": event, **fields},
            ensure_ascii=False,
            sort_keys=True,
        ),
    )


def _latency_ms(started_at: float) -> float:
    """Return elapsed milliseconds rounded for stable structured logs."""
    return round((time.perf_counter() - started_at) * 1000, 3)


class RuPIIGuardrail(CustomGuardrail):
    """LiteLLM custom guardrail that masks or blocks Russian PII using Presidio.

    Flow:
    1. async_pre_call_hook: block operational payloads, mask/block PII, then
       run final provider-bound leak checks before saving mappings
    2. async_post_call_success_hook: unmask PII in responses when mapping exists
    """

    def __init__(
        self,
        failure_mode: Optional[str] = None,
        pii_mode: Optional[str] = None,
        pre_egress_policy_mode: Optional[str] = None,
        final_payload_leak_check_mode: Optional[str] = None,
        final_payload_leak_check_canaries: Optional[tuple[str, ...]] = None,
        regulated_topic_policy_mode: Optional[str] = None,
        regulated_topic_policy_extra_rules_json: Optional[str] = None,
        synthetic_pii_allowlist_mode: Optional[str] = None,
        synthetic_pii_allowlist_json: Optional[str] = None,
        dictionary_substitutions_enabled: Optional[Union[str, bool]] = None,
        dictionary_substitutions_file: Optional[str] = None,
        dictionary_substitutions_json: Optional[str] = None,
        dictionary_substitutions_failure_mode: Optional[str] = None,
        mapping_ttl_seconds: Optional[int] = None,
        **kwargs,
    ):
        self._redis = None
        self.failure_mode = self._normalize_failure_mode(
            failure_mode or PII_GUARDRAIL_FAILURE_MODE
        )
        self.pii_mode = self._normalize_policy_mode(pii_mode or PII_GUARDRAIL_MODE)
        self.pre_egress_policy_mode = self._normalize_pre_egress_policy_mode(
            pre_egress_policy_mode or PRE_EGRESS_POLICY_MODE
        )
        self.final_payload_leak_check_mode = (
            self._normalize_final_payload_leak_check_mode(
                final_payload_leak_check_mode or FINAL_PAYLOAD_LEAK_CHECK_MODE
            )
        )
        self.final_payload_leak_check_canaries = tuple(
            token
            for token in (
                final_payload_leak_check_canaries
                if final_payload_leak_check_canaries is not None
                else FINAL_PAYLOAD_LEAK_CHECK_CANARIES
            )
            if token
        )
        self.regulated_topic_policy_mode = (
            self._normalize_regulated_topic_policy_mode(
                regulated_topic_policy_mode or REGULATED_TOPIC_POLICY_MODE
            )
        )
        self.regulated_topic_policy_extra_rules = (
            self._load_regulated_topic_policy_extra_rules(
                regulated_topic_policy_extra_rules_json
                if regulated_topic_policy_extra_rules_json is not None
                else REGULATED_TOPIC_POLICY_EXTRA_RULES_JSON
            )
        )
        self.synthetic_pii_allowlist_mode = (
            self._normalize_synthetic_pii_allowlist_mode(
                synthetic_pii_allowlist_mode or SYNTHETIC_PII_ALLOWLIST_MODE
            )
        )
        self.synthetic_pii_allowlist_rules = (
            self._load_synthetic_pii_allowlist_rules(
                synthetic_pii_allowlist_json
                if synthetic_pii_allowlist_json is not None
                else SYNTHETIC_PII_ALLOWLIST_JSON
            )
        )
        self.dictionary_substitutions_failure_mode = self._normalize_failure_mode(
            dictionary_substitutions_failure_mode
            or DICTIONARY_SUBSTITUTIONS_FAILURE_MODE
        )
        self.dictionary_substitutions_enabled = self._normalize_bool_setting(
            dictionary_substitutions_enabled
            if dictionary_substitutions_enabled is not None
            else DICTIONARY_SUBSTITUTIONS_ENABLED,
            default=True,
        )
        self.dictionary_substitution_policy = (
            self._load_dictionary_substitution_policy(
                dictionary_substitutions_file
                if dictionary_substitutions_file is not None
                else DICTIONARY_SUBSTITUTIONS_FILE,
                dictionary_substitutions_json
                if dictionary_substitutions_json is not None
                else DICTIONARY_SUBSTITUTIONS_JSON,
            )
            if self.dictionary_substitutions_enabled
            else DictionarySubstitutionPolicy([])
        )
        self.mapping_ttl_seconds = _normalize_positive_int(
            "PII_MAPPING_TTL_SECONDS",
            mapping_ttl_seconds
            if mapping_ttl_seconds is not None
            else PII_MAPPING_TTL_SECONDS,
            PII_MAPPING_TTL_SECONDS,
        )
        super().__init__(**kwargs)

    @staticmethod
    def _normalize_failure_mode(value: str) -> str:
        """Normalize and validate guardrail failure behavior."""
        mode = value.strip().lower().replace("-", "_")
        if mode not in FAILURE_MODES:
            logger.warning(
                "Unknown PII_GUARDRAIL_FAILURE_MODE=%r, falling back to fail_open",
                value,
            )
            return "fail_open"
        return mode

    @staticmethod
    def _normalize_policy_mode(value: str) -> str:
        """Normalize and validate normal PII policy behavior."""
        mode = value.strip().lower().replace("-", "_")
        if mode not in POLICY_MODES:
            logger.warning(
                "Unknown PII_GUARDRAIL_MODE=%r, falling back to mask",
                value,
            )
            return "mask"
        return mode

    @staticmethod
    def _normalize_pre_egress_policy_mode(value: str) -> str:
        """Normalize and validate config/log pre-egress policy behavior."""
        mode = value.strip().lower().replace("-", "_")
        if mode in {"disabled", "disable", "false", "0"}:
            mode = "off"
        if mode in {"enabled", "enable", "true", "1"}:
            mode = "block"
        if mode not in PRE_EGRESS_POLICY_MODES:
            logger.warning(
                "Unknown PRE_EGRESS_POLICY_MODE=%r, falling back to block",
                value,
            )
            return "block"
        return mode

    @staticmethod
    def _normalize_final_payload_leak_check_mode(value: str) -> str:
        """Normalize and validate final provider-bound leak-check behavior."""
        mode = value.strip().lower().replace("-", "_")
        if mode in {"disabled", "disable", "false", "0"}:
            mode = "off"
        if mode in {"enabled", "enable", "true", "1"}:
            mode = "block"
        if mode not in FINAL_PAYLOAD_LEAK_CHECK_MODES:
            logger.warning(
                "Unknown FINAL_PAYLOAD_LEAK_CHECK_MODE=%r, falling back to block",
                value,
            )
            return "block"
        return mode

    @staticmethod
    def _normalize_regulated_topic_policy_mode(value: str) -> str:
        """Normalize and validate regulated-topic policy behavior."""
        mode = value.strip().lower().replace("-", "_")
        if mode in {"disabled", "disable", "false", "0"}:
            mode = "off"
        if mode in {"enabled", "enable", "true", "1"}:
            mode = "block"
        if mode not in REGULATED_TOPIC_POLICY_MODES:
            logger.warning(
                "Unknown REGULATED_TOPIC_POLICY_MODE=%r, falling back to off",
                value,
            )
            return "off"
        return mode

    @staticmethod
    def _normalize_synthetic_pii_allowlist_mode(value: str) -> str:
        """Normalize and validate synthetic/test PII allowlist behavior."""
        mode = value.strip().lower().replace("-", "_")
        if mode in {"disabled", "disable", "false", "0"}:
            mode = "off"
        if mode in {"enabled", "enable", "true", "1"}:
            mode = "allow"
        if mode not in SYNTHETIC_PII_ALLOWLIST_MODES:
            logger.warning(
                "Unknown SYNTHETIC_PII_ALLOWLIST_MODE=%r, falling back to off",
                value,
            )
            return "off"
        return mode

    @staticmethod
    def _normalize_bool_setting(value: Union[str, bool], default: bool) -> bool:
        """Normalize common env-style boolean values."""
        if isinstance(value, bool):
            return value
        normalized = str(value or "").strip().lower()
        if normalized in {"1", "true", "yes", "on", "enabled", "enable"}:
            return True
        if normalized in {"0", "false", "no", "off", "disabled", "disable"}:
            return False
        logger.warning("Unknown boolean setting value=%r, falling back", value)
        return default

    @staticmethod
    def _normalize_policy_label(value: Any, fallback: str) -> str:
        """Return a bounded label safe for logs and metrics."""
        normalized = re.sub(
            r"[^a-zA-Z0-9_:-]+",
            "_",
            str(value or "").strip().lower(),
        ).strip("_:-")
        return (normalized[:80] or fallback)

    def _load_dictionary_substitution_policy(
        self,
        file_path: str,
        raw_json: str,
    ) -> DictionarySubstitutionPolicy:
        """Load reversible dictionary substitutions from JSON env or file."""
        try:
            if raw_json and raw_json.strip():
                return DictionarySubstitutionPolicy.from_json_text(raw_json)
            if file_path and file_path.strip():
                return DictionarySubstitutionPolicy.from_file(file_path)
            return DictionarySubstitutionPolicy([])
        except DictionaryPolicyConfigError as exc:
            _safe_log(
                logging.ERROR,
                "dictionary_substitution_config_invalid",
                failure_mode=self.dictionary_substitutions_failure_mode,
                error_type=type(exc).__name__,
            )
            if self.dictionary_substitutions_failure_mode == "fail_closed":
                raise
            return DictionarySubstitutionPolicy([])

    @classmethod
    def _load_regulated_topic_policy_extra_rules(
        cls,
        raw_config: str,
    ) -> list[dict[str, Any]]:
        """Load operator-defined block-only regulated-topic regex rules."""
        if not raw_config.strip():
            return []

        try:
            parsed = json.loads(raw_config)
        except (TypeError, ValueError):
            logger.warning(
                "Invalid REGULATED_TOPIC_POLICY_EXTRA_RULES_JSON, ignoring extra rules"
            )
            return []

        if not isinstance(parsed, list):
            logger.warning(
                "REGULATED_TOPIC_POLICY_EXTRA_RULES_JSON must be a JSON array"
            )
            return []

        rules: list[dict[str, Any]] = []
        for index, entry in enumerate(parsed):
            if not isinstance(entry, dict):
                logger.warning(
                    "Ignoring regulated-topic extra rule %s: expected object",
                    index,
                )
                continue

            action = str(entry.get("action") or "block").strip().lower()
            action = action.replace("-", "_")
            if action not in REGULATED_TOPIC_POLICY_ACTIONS:
                logger.warning(
                    "Ignoring regulated-topic extra rule %s: unsupported action",
                    index,
                )
                continue

            pattern = entry.get("pattern")
            if not isinstance(pattern, str) or not pattern.strip():
                logger.warning(
                    "Ignoring regulated-topic extra rule %s: missing pattern",
                    index,
                )
                continue

            flags = re.IGNORECASE | re.UNICODE
            raw_flags = str(entry.get("flags") or "")
            if "m" in raw_flags.lower():
                flags |= re.MULTILINE
            if "s" in raw_flags.lower():
                flags |= re.DOTALL

            try:
                compiled = re.compile(pattern, flags)
            except re.error:
                logger.warning(
                    "Ignoring regulated-topic extra rule %s: invalid regex",
                    index,
                )
                continue

            rules.append(
                {
                    "category": cls._normalize_policy_label(
                        entry.get("category"),
                        "custom_regulated_topic",
                    ),
                    "rule_id": cls._normalize_policy_label(
                        entry.get("rule_id"),
                        f"custom_regulated_topic_{index + 1}",
                    ),
                    "action": "block",
                    "pattern": compiled,
                }
            )

        return rules

    @staticmethod
    def _normalize_policy_list(value: Any, default: tuple[str, ...]) -> set[str]:
        """Normalize a JSON policy list to lowercase bounded names."""
        if value is None:
            items = list(default)
        elif isinstance(value, str):
            items = [value]
        elif isinstance(value, list):
            items = value
        else:
            return set()

        policies = set()
        for item in items:
            normalized = re.sub(
                r"[^a-z0-9_:-]+",
                "_",
                str(item or "").strip().lower(),
            ).strip("_:-")
            if normalized:
                policies.add(normalized)
        return policies

    @staticmethod
    def _safe_string_list(value: Any) -> list[str]:
        """Return non-empty strings without logging their raw contents."""
        if value is None:
            return []
        if isinstance(value, str):
            value = [value]
        if not isinstance(value, list):
            return []
        return [item for item in value if isinstance(item, str) and item]

    @staticmethod
    def _synthetic_pattern_has_required_anchors(pattern: str) -> bool:
        """Return whether a regex is anchored to the complete synthetic value."""
        stripped = pattern.strip()
        starts = stripped.startswith("^") or stripped.startswith(r"\A")
        ends = stripped.endswith("$") or stripped.endswith(r"\Z")
        return starts and ends

    @staticmethod
    def _synthetic_pattern_has_safe_namespace(pattern: str) -> bool:
        """Return whether a regex references an approved synthetic namespace."""
        upper_pattern = pattern.upper()
        return any(
            marker in upper_pattern
            for marker in SYNTHETIC_PII_ALLOWLIST_SAFE_PATTERN_MARKERS
        )

    @classmethod
    def _is_safe_synthetic_allowlist_pattern(cls, pattern: str) -> bool:
        """Reject broad regexes; allow only anchored controlled namespaces."""
        stripped = pattern.strip()
        if not stripped or len(stripped) > SYNTHETIC_PII_ALLOWLIST_MAX_PATTERN_LENGTH:
            return False
        if stripped in {".*", ".+", "^.*$", "^.+$", r"\A.*\Z", r"\A.+\Z"}:
            return False
        return (
            cls._synthetic_pattern_has_required_anchors(stripped)
            and cls._synthetic_pattern_has_safe_namespace(stripped)
        )

    @classmethod
    def _load_synthetic_pii_allowlist_rules(
        cls,
        raw_config: str,
    ) -> list[dict[str, Any]]:
        """Load exact-value and safe-pattern synthetic/test PII allowlist rules."""
        if not raw_config.strip():
            return []

        try:
            parsed = json.loads(raw_config)
        except (TypeError, ValueError):
            logger.warning("Invalid SYNTHETIC_PII_ALLOWLIST_JSON, ignoring rules")
            return []

        if not isinstance(parsed, list):
            logger.warning("SYNTHETIC_PII_ALLOWLIST_JSON must be a JSON array")
            return []

        rules: list[dict[str, Any]] = []
        for index, entry in enumerate(parsed):
            if not isinstance(entry, dict):
                logger.warning(
                    "Ignoring synthetic PII allowlist rule %s: expected object",
                    index,
                )
                continue

            policies = cls._normalize_policy_list(
                entry.get("policies"),
                tuple(SYNTHETIC_PII_ALLOWLIST_POLICIES),
            )
            if not policies.intersection(SYNTHETIC_PII_ALLOWLIST_POLICIES):
                continue

            raw_entity_types = cls._safe_string_list(entry.get("entity_types"))
            entity_types = {
                cls._normalize_entity_type(entity_type)
                for entity_type in raw_entity_types
            }
            if not entity_types:
                logger.warning(
                    "Ignoring synthetic PII allowlist rule %s: missing entity_types",
                    index,
                )
                continue

            values = frozenset(cls._safe_string_list(entry.get("values")))

            compiled_patterns = []
            for pattern in cls._safe_string_list(entry.get("patterns")):
                if not cls._is_safe_synthetic_allowlist_pattern(pattern):
                    logger.warning(
                        "Ignoring synthetic PII allowlist pattern in rule %s: unsafe pattern",
                        index,
                    )
                    continue
                try:
                    compiled_patterns.append(re.compile(pattern, re.UNICODE))
                except re.error:
                    logger.warning(
                        "Ignoring synthetic PII allowlist pattern in rule %s: invalid regex",
                        index,
                    )

            if not values and not compiled_patterns:
                logger.warning(
                    "Ignoring synthetic PII allowlist rule %s: no exact values or safe patterns",
                    index,
                )
                continue

            rule_id = cls._normalize_policy_label(
                entry.get("rule_id"),
                f"synthetic_pii_allowlist_{index + 1}",
            )
            rules.append(
                {
                    "rule_id": rule_id,
                    "entity_types": frozenset(entity_types),
                    "values": values,
                    "patterns": tuple(compiled_patterns),
                    "policies": frozenset(SYNTHETIC_PII_ALLOWLIST_POLICIES),
                }
            )

        return rules

    def _handle_failure(self, operation: str, error: Exception, data: dict) -> dict:
        """Apply configured fail-open/fail-closed behavior."""
        operation_label = operation.replace(" ", "_")
        if self.failure_mode == "fail_closed":
            PII_FAIL_CLOSED.labels(operation=operation_label).inc()
        else:
            PII_FAIL_OPEN.labels(operation=operation_label).inc()
        _safe_log(
            logging.ERROR,
            "pii_guardrail_failed_open"
            if self.failure_mode == "fail_open"
            else "pii_guardrail_failed_closed",
            operation=operation_label,
            failure_mode=self.failure_mode,
            error_type=type(error).__name__,
        )
        if self.failure_mode == "fail_closed":
            raise RuntimeError(f"PII guardrail {operation} failed") from error
        return data

    def _handle_dictionary_failure(
        self,
        operation: str,
        error: Exception,
        data: dict,
    ) -> dict:
        """Apply dictionary substitution failure behavior."""
        operation_label = operation.replace(" ", "_")
        if self.dictionary_substitutions_failure_mode == "fail_closed":
            PII_FAIL_CLOSED.labels(operation=f"dictionary_{operation_label}").inc()
        else:
            PII_FAIL_OPEN.labels(operation=f"dictionary_{operation_label}").inc()
        _safe_log(
            logging.ERROR,
            "dictionary_substitution_failed_open"
            if self.dictionary_substitutions_failure_mode == "fail_open"
            else "dictionary_substitution_failed_closed",
            operation=operation_label,
            failure_mode=self.dictionary_substitutions_failure_mode,
            error_type=type(error).__name__,
        )
        if self.dictionary_substitutions_failure_mode == "fail_closed":
            raise RuntimeError(
                f"Dictionary substitution {operation} failed"
            ) from error
        return data

    def _log_gateway_audit(
        self,
        *,
        request_id: str,
        data: dict,
        started_at: float,
        status: str,
        policy_result: str,
        call_type: Optional[str] = None,
        redaction_count: int = 0,
        entity_counts: Optional[dict[str, int]] = None,
        block_reason: Optional[str] = None,
        error_code: Optional[str] = None,
        categories: Optional[list[str]] = None,
        rules: Optional[list[str]] = None,
        actions: Optional[list[str]] = None,
        category_counts: Optional[dict[str, int]] = None,
        rule_counts: Optional[dict[str, int]] = None,
        finding_count: Optional[int] = None,
        synthetic_allowlist_rules: Optional[list[str]] = None,
        synthetic_allowlist_entity_counts: Optional[dict[str, int]] = None,
        synthetic_allowlist_rule_counts: Optional[dict[str, int]] = None,
        synthetic_allowlist_hit_count: Optional[int] = None,
        dictionary_substitution_rules: Optional[list[str]] = None,
        dictionary_substitution_rule_counts: Optional[dict[str, int]] = None,
        dictionary_substitution_count: Optional[int] = None,
        failure_operation: Optional[str] = None,
        error_type: Optional[str] = None,
    ) -> None:
        """Emit one safe gateway-level audit event for a pre-call decision."""
        fields: dict[str, Any] = {
            "request_id": request_id,
            "model": str(data.get("model") or "unknown"),
            "status": status,
            "latency_ms": _latency_ms(started_at),
            "guardrail_name": str(getattr(self, "guardrail_name", None) or "unknown"),
            "guardrail_mode": "pre_call",
            "call_type": str(call_type or "unknown"),
            "policy_mode": self.pii_mode,
            "pre_egress_policy_mode": self.pre_egress_policy_mode,
            "final_payload_leak_check_mode": self.final_payload_leak_check_mode,
            "regulated_topic_policy_mode": self.regulated_topic_policy_mode,
            "dictionary_substitutions_enabled": self.dictionary_substitutions_enabled,
            "failure_mode": self.failure_mode,
            "dictionary_substitutions_failure_mode": (
                self.dictionary_substitutions_failure_mode
            ),
            "policy_result": policy_result,
            "redaction_count": int(redaction_count),
            "entity_counts": entity_counts or {},
        }

        optional_fields = {
            "block_reason": block_reason,
            "error_code": error_code,
            "categories": categories,
            "rules": rules,
            "actions": actions,
            "category_counts": category_counts,
            "rule_counts": rule_counts,
            "finding_count": finding_count,
            "synthetic_allowlist_rules": synthetic_allowlist_rules,
            "synthetic_allowlist_entity_counts": synthetic_allowlist_entity_counts,
            "synthetic_allowlist_rule_counts": synthetic_allowlist_rule_counts,
            "synthetic_allowlist_hit_count": synthetic_allowlist_hit_count,
            "dictionary_substitution_rules": dictionary_substitution_rules,
            "dictionary_substitution_rule_counts": dictionary_substitution_rule_counts,
            "dictionary_substitution_count": dictionary_substitution_count,
            "failure_operation": failure_operation,
            "error_type": error_type,
        }
        fields.update(
            {
                key: value
                for key, value in optional_fields.items()
                if value is not None
            }
        )

        _safe_log(logging.INFO, "gateway_guardrail_audit", **fields)

    def _raise_analyzer_overloaded(self, error: AnalyzerOverloadedError) -> None:
        """Fail closed on capacity overload even when other failures fail open."""
        PII_FAIL_CLOSED.labels(operation="analyzer_overloaded").inc()
        _safe_log(
            logging.ERROR,
            "pii_guardrail_analyzer_overloaded",
            failure_mode="fail_closed",
            reason=error.reason,
        )
        raise RuntimeError(ANALYZER_OVERLOADED_MESSAGE) from error

    async def _get_redis(self):
        """Lazy Redis connection."""
        if self._redis is not None:
            return self._redis
        return _get_shared_redis_client()

    @classmethod
    def _iter_text_content_block_targets(cls, blocks: list) -> list[tuple[dict, str]]:
        """Return mutable text fields from provider content block lists."""
        targets: list[tuple[dict, str]] = []
        for block in blocks:
            if not isinstance(block, dict):
                continue
            block_type = block.get("type")
            if block_type in (
                None,
                "text",
                "input_text",
                "output_text",
            ) and isinstance(
                block.get("text"),
                str,
            ):
                targets.append((block, "text"))
            elif block_type == "tool_result":
                content = block.get("content")
                if isinstance(content, str):
                    targets.append((block, "content"))
                elif isinstance(content, list):
                    targets.extend(cls._iter_text_content_block_targets(content))
        return targets

    @classmethod
    def _iter_message_text_targets(cls, message: dict) -> list[tuple[dict, str]]:
        """Return mutable text fields that are safe to send through analyzer."""
        if not isinstance(message, dict):
            return []

        targets: list[tuple[dict, str]] = []
        content = message.get("content")
        if isinstance(content, str):
            targets.append((message, "content"))
        elif isinstance(content, list):
            targets.extend(cls._iter_text_content_block_targets(content))

        tool_calls = message.get("tool_calls")
        if isinstance(tool_calls, list):
            for tool_call in tool_calls:
                if not isinstance(tool_call, dict):
                    continue
                function = tool_call.get("function")
                if isinstance(function, dict) and isinstance(
                    function.get("arguments"),
                    str,
                ):
                    targets.append((function, "arguments"))

        function_call = message.get("function_call")
        if isinstance(function_call, dict) and isinstance(
            function_call.get("arguments"),
            str,
        ):
            targets.append((function_call, "arguments"))

        return targets

    @classmethod
    def _iter_responses_input_item_text_targets(
        cls,
        items: list,
    ) -> list[tuple[dict, str]]:
        """Return mutable text fields from Responses API input item lists."""
        targets: list[tuple[dict, str]] = []
        for item in items:
            if not isinstance(item, dict):
                continue

            item_type = item.get("type")
            if item_type in ("text", "input_text", "output_text") and isinstance(
                item.get("text"),
                str,
            ):
                targets.append((item, "text"))

            targets.extend(cls._iter_message_text_targets(item))

            if isinstance(item.get("arguments"), str):
                targets.append((item, "arguments"))

            output = item.get("output")
            if isinstance(output, str):
                targets.append((item, "output"))
            elif isinstance(output, list):
                targets.extend(cls._iter_text_content_block_targets(output))

        return targets

    @classmethod
    def _iter_responses_field_text_targets(
        cls,
        data: dict,
        field: str,
    ) -> list[tuple[dict, str]]:
        """Return mutable Responses API text fields for a top-level field."""
        if not isinstance(data, dict) or field not in data:
            return []

        value = data.get(field)
        if isinstance(value, str):
            return [(data, field)]

        if isinstance(value, list):
            return cls._iter_responses_input_item_text_targets(value)

        return []

    @classmethod
    def _iter_request_text_targets(cls, data: dict) -> list[tuple[dict, str]]:
        """Return request text fields covered by the guardrail."""
        targets: list[tuple[dict, str]] = []

        messages = data.get("messages")
        if isinstance(messages, list):
            for message in messages:
                targets.extend(cls._iter_message_text_targets(message))

        targets.extend(cls._iter_responses_field_text_targets(data, "instructions"))
        targets.extend(cls._iter_responses_field_text_targets(data, "input"))

        system = data.get("system")
        if isinstance(system, str):
            targets.append((data, "system"))
        elif isinstance(system, list):
            targets.extend(cls._iter_text_content_block_targets(system))

        return targets

    @staticmethod
    def _add_policy_finding(
        findings: list[dict[str, str]],
        seen_rules: set[str],
        category: str,
        rule_id: str,
    ) -> None:
        """Add one bounded policy finding without source text or offsets."""
        if rule_id in seen_rules:
            return
        seen_rules.add(rule_id)
        findings.append({"category": category, "rule_id": rule_id})

    @staticmethod
    def _add_regulated_topic_finding(
        findings: list[dict[str, str]],
        seen_rules: set[str],
        category: str,
        rule_id: str,
        action: str = "block",
    ) -> None:
        """Add one bounded regulated-topic finding without source text or offsets."""
        if rule_id in seen_rules:
            return
        seen_rules.add(rule_id)
        findings.append(
            {
                "category": category,
                "rule_id": rule_id,
                "action": action,
            }
        )

    @staticmethod
    def _has_any_regulated_topic(text: str) -> bool:
        """Return whether text mentions one of the regulated-topic families."""
        return any(
            pattern.search(text)
            for pattern in (
                _AML_CFT_TOPIC_RE,
                _SANCTIONS_TOPIC_RE,
                _TRANSACTION_MONITORING_TOPIC_RE,
                _SUSPICIOUS_ACTIVITY_TOPIC_RE,
            )
        )

    def _classify_regulated_topic_policy_text(
        self,
        text: str,
    ) -> list[dict[str, str]]:
        """Classify high-risk regulated AML/CFT topics before provider egress."""
        if not text.strip():
            return []

        findings: list[dict[str, str]] = []
        seen_rules: set[str] = set()

        if (
            self._has_any_regulated_topic(text)
            and _BYPASS_SENSITIVE_CONTEXT_RE.search(text)
        ):
            self._add_regulated_topic_finding(
                findings,
                seen_rules,
                "bypass_sensitive_procedure",
                "compliance_bypass_procedure",
            )

        if (
            _AML_CFT_TOPIC_RE.search(text)
            and _REGULATED_INTERNAL_CONTEXT_RE.search(text)
            and _REGULATED_CONTROL_CONTEXT_RE.search(text)
        ):
            self._add_regulated_topic_finding(
                findings,
                seen_rules,
                "aml_cft",
                "aml_cft_internal_controls",
            )

        if (
            _SANCTIONS_TOPIC_RE.search(text)
            and _WATCHLIST_MATCHING_CONTEXT_RE.search(text)
            and (
                _REGULATED_INTERNAL_CONTEXT_RE.search(text)
                or _REGULATED_CONTROL_CONTEXT_RE.search(text)
                or _THRESHOLD_SCENARIO_CONTEXT_RE.search(text)
            )
        ):
            self._add_regulated_topic_finding(
                findings,
                seen_rules,
                "sanctions_screening",
                "sanctions_watchlist_matching",
            )

        if (
            _TRANSACTION_MONITORING_TOPIC_RE.search(text)
            and _THRESHOLD_SCENARIO_CONTEXT_RE.search(text)
        ):
            self._add_regulated_topic_finding(
                findings,
                seen_rules,
                "transaction_monitoring",
                "transaction_monitoring_thresholds",
            )

        if (
            _SUSPICIOUS_ACTIVITY_TOPIC_RE.search(text)
            and (
                _INVESTIGATION_PLAYBOOK_CONTEXT_RE.search(text)
                or (
                    _REGULATED_INTERNAL_CONTEXT_RE.search(text)
                    and _REGULATED_CONTROL_CONTEXT_RE.search(text)
                )
            )
        ):
            self._add_regulated_topic_finding(
                findings,
                seen_rules,
                "suspicious_activity_investigation",
                "suspicious_activity_playbook",
            )

        for rule in self.regulated_topic_policy_extra_rules:
            pattern = rule["pattern"]
            if pattern.search(text):
                self._add_regulated_topic_finding(
                    findings,
                    seen_rules,
                    rule["category"],
                    rule["rule_id"],
                    rule["action"],
                )

        return findings

    def _classify_regulated_topic_policy_targets(
        self,
        request_targets: list[tuple[dict, str]],
    ) -> list[dict[str, str]]:
        """Classify all request text targets and return bounded findings."""
        findings: list[dict[str, str]] = []
        seen_rules: set[str] = set()
        for target, field in request_targets:
            content = target[field]
            if not isinstance(content, str) or not content.strip():
                continue
            for finding in self._classify_regulated_topic_policy_text(content):
                self._add_regulated_topic_finding(
                    findings,
                    seen_rules,
                    finding["category"],
                    finding["rule_id"],
                    finding["action"],
                )
        return findings

    @staticmethod
    def _has_yaml_key(text: str, key: str) -> bool:
        """Return whether text contains a top-level or nested YAML-like key."""
        return re.search(rf"(?im)^[ \t-]*{re.escape(key)}[ \t]*:", text) is not None

    @staticmethod
    def _yaml_scalar_value(text: str, key: str) -> Optional[str]:
        """Return a simple YAML scalar value for a key without parsing snippets."""
        match = re.search(
            rf"(?im)^[ \t-]*{re.escape(key)}[ \t]*:[ \t]*[\"']?([A-Za-z][\w.-]*)[\"']?[ \t]*$",
            text,
        )
        if match is None:
            return None
        return match.group(1)

    @staticmethod
    def _iter_yaml_documents(text: str) -> Iterable[str]:
        """Return non-empty YAML-ish documents split on standard doc separators."""
        for document in re.split(r"(?m)^[ \t]*---[ \t]*(?:#.*)?$", text):
            document = document.strip()
            if document:
                yield document

    @staticmethod
    def _iter_json_stacktrace_field_values(value: Any) -> Iterable[str]:
        """Return likely stack/exception string values from a parsed JSON payload."""
        pending = [value]
        visited = 0
        while pending and visited < 512:
            visited += 1
            current = pending.pop()
            if isinstance(current, dict):
                for key, child in current.items():
                    if (
                        isinstance(key, str)
                        and isinstance(child, str)
                        and _JSON_STACKTRACE_FIELD_RE.search(key)
                    ):
                        yield child
                    elif isinstance(child, (dict, list)):
                        pending.append(child)
            elif isinstance(current, list):
                pending.extend(current)

    @classmethod
    def _looks_like_json_stacktrace_payload(cls, text: str) -> bool:
        """Detect stack traces carried as JSON string values."""
        stripped = text.strip()
        if not stripped or stripped[0] not in "{[":
            return False

        try:
            payload = json.loads(stripped)
        except (TypeError, ValueError):
            return False

        return any(
            _STACK_TRACE_RE.search(value)
            for value in cls._iter_json_stacktrace_field_values(payload)
        )

    @classmethod
    def _looks_like_kubeconfig_payload(cls, text: str) -> bool:
        """Detect high-confidence kubeconfig payloads."""
        return (
            cls._has_yaml_key(text, "apiVersion")
            and cls._yaml_scalar_value(text, "kind") == "Config"
            and cls._has_yaml_key(text, "clusters")
            and cls._has_yaml_key(text, "contexts")
            and cls._has_yaml_key(text, "users")
        )

    @staticmethod
    def _looks_like_nginx_config_payload(text: str) -> bool:
        """Detect high-confidence nginx virtual host snippets."""
        if re.search(r"(?is)\bserver\s*\{", text) is None:
            return False

        directives = re.findall(
            r"(?im)(?:^|[;{]\s*)(listen|server_name|root|location|proxy_pass|"
            r"ssl_certificate|try_files|access_log|error_log|return)\b",
            text,
        )
        return len(set(directives)) >= 2

    @classmethod
    def _looks_like_service_manifest_payload(cls, text: str) -> bool:
        """Detect high-confidence Kubernetes service/application manifests."""
        for document in cls._iter_yaml_documents(text):
            if not cls._has_yaml_key(document, "apiVersion") or not cls._has_yaml_key(
                document,
                "metadata",
            ):
                continue

            kind = cls._yaml_scalar_value(document, "kind")
            if not kind:
                continue

            if any(
                cls._has_yaml_key(document, key)
                for key in ("data", "stringData", "binaryData", "spec")
            ):
                return True
        return False

    @classmethod
    def _classify_pre_egress_policy_text(cls, text: str) -> list[dict[str, str]]:
        """Classify high-risk config/log payloads before Analyzer/provider egress."""
        if not text.strip():
            return []

        findings: list[dict[str, str]] = []
        seen_rules: set[str] = set()

        if (
            _ENV_SECRET_ASSIGNMENT_RE.search(text)
            or _ENV_SECRET_YAML_MAPPING_RE.search(text)
            or _ENV_CREDENTIAL_URL_RE.search(text)
            or _ENV_CREDENTIAL_URL_YAML_MAPPING_RE.search(text)
        ):
            cls._add_policy_finding(
                findings,
                seen_rules,
                "config",
                "env_secret_assignment",
            )

        if cls._looks_like_kubeconfig_payload(text):
            cls._add_policy_finding(
                findings,
                seen_rules,
                "config",
                "kubeconfig_payload",
            )

        if cls._looks_like_nginx_config_payload(text):
            cls._add_policy_finding(
                findings,
                seen_rules,
                "config",
                "nginx_config_payload",
            )

        if cls._looks_like_service_manifest_payload(text):
            cls._add_policy_finding(
                findings,
                seen_rules,
                "config",
                "service_manifest_payload",
            )

        if (
            _ACCESS_LOG_RE.search(text)
            or _STACK_TRACE_RE.search(text)
            or cls._looks_like_json_stacktrace_payload(text)
            or _AUTH_LOG_RE.search(text)
        ):
            cls._add_policy_finding(
                findings,
                seen_rules,
                "log",
                "log_or_stacktrace_payload",
            )

        return findings

    @classmethod
    def _classify_pre_egress_policy_targets(
        cls,
        request_targets: list[tuple[dict, str]],
    ) -> list[dict[str, str]]:
        """Classify all request text targets and return bounded findings."""
        findings: list[dict[str, str]] = []
        seen_rules: set[str] = set()
        for target, field in request_targets:
            content = target[field]
            if not isinstance(content, str) or not content.strip():
                continue
            for finding in cls._classify_pre_egress_policy_text(content):
                cls._add_policy_finding(
                    findings,
                    seen_rules,
                    finding["category"],
                    finding["rule_id"],
                )
        return findings

    @staticmethod
    def _add_final_leak_finding(
        findings: list[dict[str, str]],
        seen_rules: set[str],
        rule_id: str,
    ) -> None:
        """Add one bounded final leak-check finding without raw values."""
        if rule_id in seen_rules:
            return
        seen_rules.add(rule_id)
        findings.append({"rule_id": rule_id})

    def _classify_final_payload_leak_check_text(
        self,
        text: str,
    ) -> list[dict[str, str]]:
        """Classify deterministic raw leak markers in provider-bound text."""
        if not text.strip():
            return []

        findings: list[dict[str, str]] = []
        seen_rules: set[str] = set()

        for canary in self.final_payload_leak_check_canaries:
            if canary and canary in text:
                self._add_final_leak_finding(
                    findings,
                    seen_rules,
                    "configured_canary",
                )
                break

        if _PRIVATE_KEY_MARKER_RE.search(text):
            self._add_final_leak_finding(
                findings,
                seen_rules,
                "private_key_marker",
            )

        if _BEARER_TOKEN_RE.search(text):
            self._add_final_leak_finding(
                findings,
                seen_rules,
                "bearer_token",
            )

        if _JWT_TOKEN_RE.search(text):
            self._add_final_leak_finding(
                findings,
                seen_rules,
                "jwt_token",
            )

        if _PROVIDER_KEY_RE.search(text):
            self._add_final_leak_finding(
                findings,
                seen_rules,
                "provider_key",
            )

        if (
            _ENV_SECRET_ASSIGNMENT_RE.search(text)
            or _ENV_SECRET_YAML_MAPPING_RE.search(text)
            or _ENV_CREDENTIAL_URL_RE.search(text)
            or _ENV_CREDENTIAL_URL_YAML_MAPPING_RE.search(text)
            or _RAW_CREDENTIAL_URL_RE.search(text)
        ):
            self._add_final_leak_finding(
                findings,
                seen_rules,
                "env_secret_assignment",
            )

        return findings

    def _classify_final_payload_leak_check_texts(
        self,
        texts: list[str],
    ) -> list[dict[str, str]]:
        """Classify final provider-bound texts and return bounded findings."""
        findings: list[dict[str, str]] = []
        seen_rules: set[str] = set()
        for text in texts:
            for finding in self._classify_final_payload_leak_check_text(text):
                self._add_final_leak_finding(
                    findings,
                    seen_rules,
                    finding["rule_id"],
                )
        return findings

    @classmethod
    def _iter_nested_string_values(cls, value: Any) -> list[str]:
        """Return string keys/values from provider-bound structured request fields."""
        if isinstance(value, str):
            return [value]
        if isinstance(value, list):
            texts: list[str] = []
            for item in value:
                texts.extend(cls._iter_nested_string_values(item))
            return texts
        if isinstance(value, dict):
            texts = []
            for key, item in value.items():
                if isinstance(key, str):
                    if key in {
                        PII_REQUEST_ID_METADATA_KEY,
                        PII_STREAMING_RESTORATION_DONE_METADATA_KEY,
                    }:
                        continue
                    texts.append(key)
                    if _ENV_SECRET_KEY_RE.fullmatch(key) and cls._has_value(item):
                        if isinstance(item, str):
                            texts.append(f"{key}={item}")
                            texts.append(f"{key}: {item}")
                        else:
                            texts.append(f"{key}=structured")
                texts.extend(cls._iter_nested_string_values(item))
            return texts
        return []

    @staticmethod
    def _has_value(value: Any) -> bool:
        """Return whether a structured provider value is meaningfully non-empty."""
        if value is None:
            return False
        if isinstance(value, str):
            return bool(value.strip())
        if isinstance(value, (list, tuple, set, dict)):
            return bool(value)
        return True

    @classmethod
    def _iter_provider_bound_final_payload_texts(cls, data: dict) -> list[str]:
        """Return strings from provider-bound payload fields for scan-only checks."""
        texts: list[str] = []
        for field in (
            *FINAL_PAYLOAD_LEAK_CHECK_PROVIDER_BOUND_REQUEST_FIELDS,
            *FINAL_PAYLOAD_LEAK_CHECK_PROVIDER_BOUND_FIELDS,
        ):
            if field in data:
                texts.extend(cls._iter_nested_string_values(data[field]))
        return texts

    def _run_final_payload_leak_check(
        self,
        data: dict,
        request_targets: list[tuple[dict, str]],
        request_id: str,
        *,
        audit_started_at: Optional[float] = None,
        call_type: Optional[str] = None,
        redaction_count: int = 0,
        entity_counts: Optional[dict[str, int]] = None,
        synthetic_allowlist_findings: Optional[list[dict[str, str]]] = None,
        dictionary_rule_counts: Optional[dict[str, int]] = None,
    ) -> None:
        """Block confirmed leaks in actual provider-bound request text."""
        if self.final_payload_leak_check_mode != "block":
            return

        final_texts = self._iter_provider_bound_final_payload_texts(data)
        final_leak_findings = self._classify_final_payload_leak_check_texts(
            final_texts
        )
        if final_leak_findings:
            self._raise_final_payload_leak_check_blocked(
                data,
                request_id,
                final_leak_findings,
                audit_started_at=audit_started_at,
                call_type=call_type,
                redaction_count=redaction_count,
                entity_counts=entity_counts,
                synthetic_allowlist_findings=synthetic_allowlist_findings,
                dictionary_rule_counts=dictionary_rule_counts,
            )

    @staticmethod
    def _get_container_field(container: Any, field: str) -> Any:
        """Read a field from dict-like or object-like LiteLLM containers."""
        if isinstance(container, dict):
            return container.get(field)
        return getattr(container, field, None)

    @staticmethod
    def _set_container_field(container: Any, field: str, value: Any) -> None:
        """Write a field to dict-like or object-like LiteLLM containers."""
        if isinstance(container, dict):
            container[field] = value
        else:
            setattr(container, field, value)

    @classmethod
    def _iter_response_text_targets(cls, message: Any) -> list[tuple[Any, str]]:
        """Return mutable response fields that may contain PII placeholders."""
        if message is None:
            return []

        targets: list[tuple[Any, str]] = []
        content = cls._get_container_field(message, "content")
        if isinstance(content, str):
            targets.append((message, "content"))
        elif isinstance(content, list):
            for block in content:
                block_text = cls._get_container_field(block, "text")
                if isinstance(block_text, str):
                    targets.append((block, "text"))

        reasoning_content = cls._get_container_field(message, "reasoning_content")
        if isinstance(reasoning_content, str):
            targets.append((message, "reasoning_content"))

        tool_calls = cls._get_container_field(message, "tool_calls")
        if isinstance(tool_calls, list):
            for tool_call in tool_calls:
                function = cls._get_container_field(tool_call, "function")
                arguments = cls._get_container_field(function, "arguments")
                if isinstance(arguments, str):
                    targets.append((function, "arguments"))

        function_call = cls._get_container_field(message, "function_call")
        arguments = cls._get_container_field(function_call, "arguments")
        if isinstance(arguments, str):
            targets.append((function_call, "arguments"))

        return targets

    async def _analyze_text(self, text: str) -> list[dict]:
        """Send text to Presidio Analyzer for PII detection."""
        started_at = time.perf_counter()
        try:
            client = _get_shared_analyzer_http_client()
            response = await client.post(
                f"{PRESIDIO_ANALYZER_URL}/api/v1/analyze",
                json={"text": text, "language": "ru", "score_threshold": 0.35},
            )
            if response.status_code == 503:
                try:
                    detail = response.json().get("detail", {})
                except ValueError:
                    detail = {}
                if isinstance(detail, dict) and (
                    detail.get("code") == "analyzer_overloaded"
                ):
                    raise AnalyzerOverloadedError(
                        reason=str(detail.get("reason") or "unknown")
                    )
            response.raise_for_status()
            data = response.json()
            return data.get("entities", [])
        finally:
            PII_ANALYZER_LATENCY.observe(time.perf_counter() - started_at)

    def _mask_text(
        self,
        text: str,
        entities: list[dict],
        entity_counts: Optional[dict[str, int]] = None,
        exclusion_spans: Optional[tuple[tuple[int, int], ...]] = None,
    ) -> tuple[str, dict[str, str]]:
        """Mask text with unique placeholders and return placeholder mapping."""
        if not entities:
            return text, {}

        if entity_counts is None:
            entity_counts = {}

        normalized_entities = self._normalize_entities(
            text,
            entities,
            exclusion_spans=exclusion_spans,
        )
        if not normalized_entities:
            return text, {}

        parts = []
        mapping = {}
        last_end = 0
        for start, end, entity_type in normalized_entities:
            placeholder = self._next_placeholder(entity_type, entity_counts)
            parts.append(text[last_end:start])
            parts.append(placeholder)
            mapping[placeholder] = text[start:end]
            last_end = end

        parts.append(text[last_end:])
        return "".join(parts), mapping

    def _normalize_entities(
        self,
        text: str,
        entities: list[dict],
        exclusion_spans: Optional[tuple[tuple[int, int], ...]] = None,
    ) -> list[tuple[int, int, str]]:
        """Return valid non-overlapping entity spans with normalized types."""
        normalized_entities = []
        for entity in entities:
            try:
                start = int(entity["start"])
                end = int(entity["end"])
            except (KeyError, TypeError, ValueError):
                continue

            if start < 0 or end > len(text) or start >= end:
                continue
            if self._span_overlaps_any(start, end, exclusion_spans or ()):
                continue

            entity_type = self._normalize_entity_type(
                str(entity.get("entity_type") or "PII")
            )
            normalized_entities.append((start, end, entity_type))

        if not normalized_entities:
            return []

        normalized_entities.sort(key=lambda item: (item[0], -(item[1] - item[0])))

        non_overlapping_entities = []
        last_end = 0
        for start, end, entity_type in normalized_entities:
            if start < last_end:
                continue
            non_overlapping_entities.append((start, end, entity_type))
            last_end = end

        return non_overlapping_entities

    @staticmethod
    def _span_overlaps_any(
        start: int,
        end: int,
        spans: Iterable[tuple[int, int]],
    ) -> bool:
        """Return whether a span overlaps any excluded provider-bound range."""
        return any(
            start < span_end and end > span_start
            for span_start, span_end in spans
        )

    @staticmethod
    def _synthetic_pii_rule_matches(
        rule: dict[str, Any],
        entity_type: str,
        value: str,
    ) -> bool:
        """Return whether one configured allowlist rule matches one PII span."""
        if entity_type not in rule["entity_types"]:
            return False
        if value in rule["values"]:
            return True
        return any(pattern.fullmatch(value) for pattern in rule["patterns"])

    def _filter_synthetic_pii_allowlisted_entities(
        self,
        text: str,
        entities: list[dict],
    ) -> tuple[list[dict], list[dict[str, str]]]:
        """Remove explicitly allowlisted synthetic/test PII spans."""
        if (
            self.synthetic_pii_allowlist_mode != "allow"
            or not self.synthetic_pii_allowlist_rules
            or not entities
        ):
            return entities, []

        filtered_entities: list[dict] = []
        findings: list[dict[str, str]] = []
        for entity in entities:
            try:
                start = int(entity["start"])
                end = int(entity["end"])
            except (KeyError, TypeError, ValueError):
                filtered_entities.append(entity)
                continue

            if start < 0 or end > len(text) or start >= end:
                filtered_entities.append(entity)
                continue

            entity_type = self._normalize_entity_type(
                str(entity.get("entity_type") or "PII")
            )
            value = text[start:end]
            matching_rule_id = None
            for rule in self.synthetic_pii_allowlist_rules:
                if self._synthetic_pii_rule_matches(rule, entity_type, value):
                    matching_rule_id = rule["rule_id"]
                    break

            if matching_rule_id is None:
                filtered_entities.append(entity)
                continue

            findings.append(
                {
                    "rule_id": matching_rule_id,
                    "entity_type": entity_type,
                }
            )

        return filtered_entities, findings

    @staticmethod
    def _synthetic_allowlist_entity_counts(
        findings: list[dict[str, str]],
    ) -> dict[str, int]:
        """Return allowlist hit counts by normalized entity type."""
        counts: dict[str, int] = {}
        for finding in findings:
            entity_type = finding["entity_type"]
            counts[entity_type] = counts.get(entity_type, 0) + 1
        return counts

    @staticmethod
    def _synthetic_allowlist_rule_counts(
        findings: list[dict[str, str]],
    ) -> dict[str, int]:
        """Return allowlist hit counts by bounded rule id."""
        counts: dict[str, int] = {}
        for finding in findings:
            rule_id = finding["rule_id"]
            counts[rule_id] = counts.get(rule_id, 0) + 1
        return counts

    @classmethod
    def _synthetic_pii_allowlist_audit_fields(
        cls,
        findings: list[dict[str, str]],
    ) -> dict[str, Any]:
        """Build safe audit fields for synthetic/test PII allowlist hits."""
        if not findings:
            return {}
        return {
            "synthetic_allowlist_rules": sorted(
                {finding["rule_id"] for finding in findings}
            ),
            "synthetic_allowlist_entity_counts": (
                cls._synthetic_allowlist_entity_counts(findings)
            ),
            "synthetic_allowlist_rule_counts": (
                cls._synthetic_allowlist_rule_counts(findings)
            ),
            "synthetic_allowlist_hit_count": len(findings),
        }

    @classmethod
    def _record_synthetic_pii_allowlist_hits(
        cls,
        findings: list[dict[str, str]],
    ) -> None:
        """Record synthetic/test PII allowlist hits by bounded labels."""
        for finding in findings:
            SYNTHETIC_PII_ALLOWLIST_HITS.labels(
                rule_id=finding["rule_id"],
                entity_type=finding["entity_type"],
            ).inc()

    @classmethod
    def _emit_synthetic_pii_allowlist_applied(
        cls,
        request_id: str,
        findings: list[dict[str, str]],
    ) -> None:
        """Emit safe logs/metrics when synthetic/test PII was allowlisted."""
        if not findings:
            return
        cls._record_synthetic_pii_allowlist_hits(findings)
        audit_fields = cls._synthetic_pii_allowlist_audit_fields(findings)
        _safe_log(
            logging.INFO,
            "synthetic_pii_allowlist_applied",
            request_id=request_id,
            rules=audit_fields["synthetic_allowlist_rules"],
            entity_counts=audit_fields["synthetic_allowlist_entity_counts"],
            rule_counts=audit_fields["synthetic_allowlist_rule_counts"],
            hit_count=audit_fields["synthetic_allowlist_hit_count"],
        )

    def _apply_dictionary_substitutions(
        self,
        text: str,
    ) -> DictionarySubstitutionResult:
        """Apply reversible dictionary policy to one provider-bound text field."""
        if (
            not self.dictionary_substitutions_enabled
            or not self.dictionary_substitution_policy.rules
        ):
            return DictionarySubstitutionResult(text, {}, (), {})
        return self.dictionary_substitution_policy.apply(text)

    @staticmethod
    def _merge_dictionary_rule_counts(
        current: dict[str, int],
        update: dict[str, int],
    ) -> None:
        """Merge bounded dictionary rule hit counts in-place."""
        for rule_id, count in update.items():
            current[rule_id] = current.get(rule_id, 0) + count

    @staticmethod
    def _dictionary_substitution_audit_fields(
        rule_counts: dict[str, int],
    ) -> dict[str, Any]:
        """Build safe audit fields for dictionary substitutions."""
        if not rule_counts:
            return {}
        return {
            "dictionary_substitution_rules": sorted(rule_counts),
            "dictionary_substitution_rule_counts": dict(sorted(rule_counts.items())),
            "dictionary_substitution_count": sum(rule_counts.values()),
        }

    @staticmethod
    def _record_dictionary_substitutions(rule_counts: dict[str, int]) -> None:
        """Record dictionary substitutions by bounded rule id."""
        for rule_id, count in rule_counts.items():
            DICTIONARY_SUBSTITUTIONS_APPLIED.labels(rule_id=rule_id).inc(count)

    @classmethod
    def _emit_dictionary_substitution_applied(
        cls,
        request_id: str,
        rule_counts: dict[str, int],
        mapping_size: int,
        mapping_ttl_seconds: int,
    ) -> None:
        """Emit safe logs/metrics when dictionary substitutions were applied."""
        if not rule_counts:
            return
        cls._record_dictionary_substitutions(rule_counts)
        DICTIONARY_SUBSTITUTION_MAPPING_SIZE.observe(mapping_size)
        _safe_log(
            logging.INFO,
            "dictionary_substitution_applied",
            request_id=request_id,
            rules=sorted(rule_counts),
            rule_counts=dict(sorted(rule_counts.items())),
            substitution_count=sum(rule_counts.values()),
            mapping_size=mapping_size,
            mapping_ttl_seconds=mapping_ttl_seconds,
        )

    @staticmethod
    def _normalize_entity_type(entity_type: str) -> str:
        """Keep placeholders predictable even for custom entity names."""
        normalized = re.sub(r"[^A-Za-z0-9_]", "_", entity_type).strip("_").upper()
        return normalized or "PII"

    @staticmethod
    def _next_placeholder(entity_type: str, entity_counts: dict[str, int]) -> str:
        """Return the next request-scoped placeholder for an entity type."""
        next_index = entity_counts.get(entity_type, 0) + 1
        entity_counts[entity_type] = next_index
        return f"<{entity_type}_{next_index}>"

    async def _save_mapping(self, request_id: str, mapping: dict):
        """Save PII mapping to Redis with TTL."""
        if not mapping:
            return
        r = await self._get_redis()
        started_at = time.perf_counter()
        try:
            await r.setex(
                f"pii_mapping:{request_id}",
                self.mapping_ttl_seconds,
                json.dumps(mapping, ensure_ascii=False),
            )
        finally:
            PII_REDIS_LATENCY.labels(operation="save").observe(
                time.perf_counter() - started_at
            )

    async def _load_mapping(self, request_id: str) -> dict:
        """Load PII mapping from Redis."""
        r = await self._get_redis()
        started_at = time.perf_counter()
        try:
            data = await r.get(f"pii_mapping:{request_id}")
        finally:
            PII_REDIS_LATENCY.labels(operation="load").observe(
                time.perf_counter() - started_at
            )
        if data:
            return json.loads(data)
        return {}

    async def _delete_mapping(self, request_id: str) -> None:
        """Delete PII mapping from Redis after post-call processing."""
        r = await self._get_redis()
        started_at = time.perf_counter()
        try:
            await r.delete(f"pii_mapping:{request_id}")
        finally:
            PII_REDIS_LATENCY.labels(operation="delete").observe(
                time.perf_counter() - started_at
            )

    def _get_request_id(self, data: dict) -> str:
        """Generate a server-side mapping ID for request-scoped PII."""
        return str(uuid.uuid4())

    @staticmethod
    def _get_response_request_id(data: dict) -> Optional[str]:
        """Return request ID used for post-call response restoration."""
        metadata = data.get("metadata", {})
        if not isinstance(metadata, dict):
            return None
        request_id = metadata.get(PII_REQUEST_ID_METADATA_KEY)
        if not request_id:
            return None
        return str(request_id)

    @staticmethod
    def _mark_streaming_restoration_done(data: dict) -> None:
        """Mark that streaming iterator hook already handled post-call restoration."""
        if not isinstance(data.get("metadata"), dict):
            data["metadata"] = {}
        data["metadata"][PII_STREAMING_RESTORATION_DONE_METADATA_KEY] = True

    @staticmethod
    def _streaming_restoration_done(data: dict) -> bool:
        """Return whether streaming iterator hook already handled this response."""
        metadata = data.get("metadata", {})
        return (
            isinstance(metadata, dict)
            and metadata.get(PII_STREAMING_RESTORATION_DONE_METADATA_KEY) is True
        )

    @staticmethod
    def _clear_inbound_pii_metadata(data: dict) -> None:
        """Remove caller-supplied guardrail-internal metadata from a new request."""
        metadata = data.get("metadata")
        if not isinstance(metadata, dict):
            return
        metadata.pop(PII_REQUEST_ID_METADATA_KEY, None)
        metadata.pop(PII_STREAMING_RESTORATION_DONE_METADATA_KEY, None)

    @staticmethod
    def _entity_counts_from_mapping(mapping: dict[str, str]) -> dict[str, int]:
        """Return entity counts derived from generated placeholders."""
        counts: dict[str, int] = {}
        for placeholder in mapping:
            entity_type = placeholder.strip("<>").rsplit("_", 1)[0] or "PII"
            counts[entity_type] = counts.get(entity_type, 0) + 1
        return counts

    @staticmethod
    def _record_entities(entity_counts: dict[str, int]) -> None:
        """Increment entity metrics with bounded entity_type labels."""
        for entity_type, count in entity_counts.items():
            PII_ENTITIES_DETECTED.labels(entity_type=entity_type).inc(count)

    @staticmethod
    def _record_blocked_entities(entity_counts: dict[str, int]) -> None:
        """Increment blocked entity metrics with bounded entity_type labels."""
        for entity_type, count in entity_counts.items():
            PII_BLOCKED_ENTITIES.labels(entity_type=entity_type).inc(count)

    @staticmethod
    def _entity_counts_from_normalized_entities(
        normalized_entities: list[tuple[int, int, str]],
    ) -> dict[str, int]:
        """Return entity counts without exposing source text or offsets."""
        counts: dict[str, int] = {}
        for _, _, entity_type in normalized_entities:
            counts[entity_type] = counts.get(entity_type, 0) + 1
        return counts

    @staticmethod
    def _category_counts_from_policy_findings(
        findings: list[dict[str, str]],
    ) -> dict[str, int]:
        """Return policy category counts without exposing matched text."""
        counts: dict[str, int] = {}
        for finding in findings:
            category = finding["category"]
            counts[category] = counts.get(category, 0) + 1
        return counts

    @staticmethod
    def _rule_counts_from_policy_findings(
        findings: list[dict[str, str]],
    ) -> dict[str, int]:
        """Return policy rule counts without exposing matched text."""
        counts: dict[str, int] = {}
        for finding in findings:
            rule_id = finding["rule_id"]
            counts[rule_id] = counts.get(rule_id, 0) + 1
        return counts

    @staticmethod
    def _record_pre_egress_policy_blocks(category_counts: dict[str, int]) -> None:
        """Increment pre-egress policy metrics with bounded category labels."""
        for category in category_counts:
            PRE_EGRESS_POLICY_BLOCKED.labels(category=category).inc()

    @staticmethod
    def _record_regulated_topic_policy_blocks(
        findings: list[dict[str, str]],
    ) -> None:
        """Increment regulated-topic metrics with bounded labels."""
        for finding in findings:
            REGULATED_TOPIC_POLICY_BLOCKED.labels(
                category=finding["category"],
                rule_id=finding["rule_id"],
            ).inc()

    @staticmethod
    def _rule_counts_from_final_leak_findings(
        findings: list[dict[str, str]],
    ) -> dict[str, int]:
        """Return final leak-check rule counts without exposing matched text."""
        counts: dict[str, int] = {}
        for finding in findings:
            rule_id = finding["rule_id"]
            counts[rule_id] = counts.get(rule_id, 0) + 1
        return counts

    @staticmethod
    def _record_final_payload_leak_check_blocks(rule_counts: dict[str, int]) -> None:
        """Increment final leak-check metrics with bounded rule labels."""
        for rule_id, count in rule_counts.items():
            FINAL_PAYLOAD_LEAK_CHECK_BLOCKED.labels(rule_id=rule_id).inc(count)

    def _raise_regulated_topic_policy_blocked(
        self,
        data: dict,
        request_id: str,
        findings: list[dict[str, str]],
        *,
        audit_started_at: Optional[float] = None,
        call_type: Optional[str] = None,
    ) -> None:
        """Raise a safe client error for blocked regulated-topic content."""
        categories = sorted({finding["category"] for finding in findings})
        rules = sorted({finding["rule_id"] for finding in findings})
        actions = sorted({finding["action"] for finding in findings})
        category_counts = self._category_counts_from_policy_findings(findings)
        rule_counts = self._rule_counts_from_policy_findings(findings)
        self._record_regulated_topic_policy_blocks(findings)
        PII_PRE_CALLS.labels(result="regulated_topic_policy_blocked").inc()
        _safe_log(
            logging.INFO,
            "regulated_topic_policy_blocked",
            request_id=request_id,
            categories=categories,
            rules=rules,
            actions=actions,
            category_counts=category_counts,
            rule_counts=rule_counts,
            finding_count=len(findings),
        )
        if audit_started_at is not None:
            self._log_gateway_audit(
                request_id=request_id,
                data=data,
                started_at=audit_started_at,
                status="blocked",
                policy_result="regulated_topic_policy_blocked",
                call_type=call_type,
                block_reason="regulated_topic_policy_violation",
                error_code="regulated_topic_policy_blocked",
                categories=categories,
                rules=rules,
                actions=actions,
                category_counts=category_counts,
                rule_counts=rule_counts,
                finding_count=len(findings),
            )

        error = {
            "message": REGULATED_TOPIC_POLICY_BLOCKED_MESSAGE,
            "type": "regulated_topic_policy_violation",
            "code": "regulated_topic_policy_blocked",
            "details": {
                "categories": categories,
                "rules": rules,
                "actions": actions,
            },
        }
        policy_param = {
            "regulated_topic_policy": {
                "code": error["code"],
                "details": error["details"],
            }
        }
        provider_specific_fields = {
            "error": error,
            "guardrail_name": self.guardrail_name,
            "guardrail_mode": "pre_call",
        }
        exc = ProxyException(
            message=REGULATED_TOPIC_POLICY_BLOCKED_MESSAGE,
            type="regulated_topic_policy_violation",
            param=policy_param,
            code=422,
            provider_specific_fields=provider_specific_fields,
        )
        exc.status_code = 422
        raise exc

    def _raise_pre_egress_policy_blocked(
        self,
        data: dict,
        request_id: str,
        findings: list[dict[str, str]],
        *,
        audit_started_at: Optional[float] = None,
        call_type: Optional[str] = None,
    ) -> None:
        """Raise a safe client error for blocked config/log payloads."""
        categories = sorted({finding["category"] for finding in findings})
        rules = sorted({finding["rule_id"] for finding in findings})
        category_counts = self._category_counts_from_policy_findings(findings)
        self._record_pre_egress_policy_blocks(category_counts)
        PII_PRE_CALLS.labels(result="pre_egress_policy_blocked").inc()
        _safe_log(
            logging.INFO,
            "pre_egress_policy_blocked",
            request_id=request_id,
            categories=categories,
            rules=rules,
            category_counts=category_counts,
            finding_count=len(findings),
        )
        if audit_started_at is not None:
            self._log_gateway_audit(
                request_id=request_id,
                data=data,
                started_at=audit_started_at,
                status="blocked",
                policy_result="pre_egress_policy_blocked",
                call_type=call_type,
                block_reason="pre_egress_policy_violation",
                error_code="pre_egress_policy_blocked",
                categories=categories,
                rules=rules,
                category_counts=category_counts,
                finding_count=len(findings),
            )

        error = {
            "message": PRE_EGRESS_POLICY_BLOCKED_MESSAGE,
            "type": "pre_egress_policy_violation",
            "code": "pre_egress_policy_blocked",
            "details": {
                "categories": categories,
                "rules": rules,
            },
        }
        policy_param = {
            "pre_egress_policy": {
                "code": error["code"],
                "details": error["details"],
            }
        }
        provider_specific_fields = {
            "error": error,
            "guardrail_name": self.guardrail_name,
            "guardrail_mode": "pre_call",
        }
        exc = ProxyException(
            message=PRE_EGRESS_POLICY_BLOCKED_MESSAGE,
            type="pre_egress_policy_violation",
            param=policy_param,
            code=422,
            provider_specific_fields=provider_specific_fields,
        )
        exc.status_code = 422
        raise exc

    def _raise_final_payload_leak_check_blocked(
        self,
        data: dict,
        request_id: str,
        findings: list[dict[str, str]],
        *,
        audit_started_at: Optional[float] = None,
        call_type: Optional[str] = None,
        redaction_count: int = 0,
        entity_counts: Optional[dict[str, int]] = None,
        synthetic_allowlist_findings: Optional[list[dict[str, str]]] = None,
        dictionary_rule_counts: Optional[dict[str, int]] = None,
    ) -> None:
        """Raise a safe client error for confirmed final payload leaks."""
        rules = sorted({finding["rule_id"] for finding in findings})
        rule_counts = self._rule_counts_from_final_leak_findings(findings)
        self._record_final_payload_leak_check_blocks(rule_counts)
        PII_PRE_CALLS.labels(result="final_payload_leak_check_blocked").inc()
        _safe_log(
            logging.INFO,
            "final_payload_leak_check_blocked",
            request_id=request_id,
            rules=rules,
            rule_counts=rule_counts,
            finding_count=len(findings),
        )
        if audit_started_at is not None:
            self._log_gateway_audit(
                request_id=request_id,
                data=data,
                started_at=audit_started_at,
                status="blocked",
                policy_result="final_payload_leak_check_blocked",
                call_type=call_type,
                redaction_count=redaction_count,
                entity_counts=entity_counts,
                block_reason="final_payload_leak_check_violation",
                error_code="final_payload_leak_check_blocked",
                rules=rules,
                rule_counts=rule_counts,
                finding_count=len(findings),
                **self._synthetic_pii_allowlist_audit_fields(
                    synthetic_allowlist_findings or []
                ),
                **self._dictionary_substitution_audit_fields(
                    dictionary_rule_counts or {}
                ),
            )

        body = {
            "error": {
                "message": FINAL_PAYLOAD_LEAK_CHECK_BLOCKED_MESSAGE,
                "type": "final_payload_leak_check_violation",
                "code": "final_payload_leak_check_blocked",
                "details": {
                    "rules": rules,
                },
            }
        }
        response = httpx.Response(
            status_code=422,
            json=body,
            request=httpx.Request(
                "POST",
                "http://ru-llm-proxy.local/final-payload-leak-check",
            ),
        )
        raise litellm.UnprocessableEntityError(
            message=FINAL_PAYLOAD_LEAK_CHECK_BLOCKED_MESSAGE,
            model=str(data.get("model") or "unknown"),
            llm_provider="ru-llm-proxy",
            response=response,
        )

    def _raise_blocked_request(
        self,
        data: dict,
        request_id: str,
        entity_counts: dict[str, int],
        *,
        audit_started_at: Optional[float] = None,
        call_type: Optional[str] = None,
        synthetic_allowlist_findings: Optional[list[dict[str, str]]] = None,
        dictionary_rule_counts: Optional[dict[str, int]] = None,
    ) -> None:
        """Raise a LiteLLM-compatible client error without raw PII."""
        entity_types = sorted(entity_counts)
        self._record_blocked_entities(entity_counts)
        PII_PRE_CALLS.labels(result="blocked").inc()
        _safe_log(
            logging.INFO,
            "pii_guardrail_blocked",
            request_id=request_id,
            entity_types=entity_types,
            entity_counts=entity_counts,
        )
        if audit_started_at is not None:
            self._log_gateway_audit(
                request_id=request_id,
                data=data,
                started_at=audit_started_at,
                status="blocked",
                policy_result="pii_blocked",
                call_type=call_type,
                entity_counts=entity_counts,
                block_reason="pii_detected",
                error_code="pii_blocked",
                **self._synthetic_pii_allowlist_audit_fields(
                    synthetic_allowlist_findings or []
                ),
                **self._dictionary_substitution_audit_fields(
                    dictionary_rule_counts or {}
                ),
            )

        body = {
            "error": {
                "message": PII_BLOCKED_MESSAGE,
                "type": "pii_detected",
                "code": "pii_blocked",
                "details": {"entities": entity_types},
            }
        }
        response = httpx.Response(
            status_code=422,
            json=body,
            request=httpx.Request("POST", "http://ru-llm-proxy.local/pii-policy"),
        )
        raise litellm.UnprocessableEntityError(
            message=PII_BLOCKED_MESSAGE,
            model=str(data.get("model") or "unknown"),
            llm_provider="ru-llm-proxy",
            response=response,
        )

    async def async_pre_call_hook(
        self,
        user_api_key_dict: UserAPIKeyAuth,
        cache: DualCache,
        data: dict,
        call_type: Optional[str] = None,
    ) -> Optional[Union[Exception, str, dict]]:
        """Apply request-time PII policy before sending to LLM."""
        started_at = time.perf_counter()
        self._clear_inbound_pii_metadata(data)
        request_targets = self._iter_request_text_targets(data)
        request_id = self._get_request_id(data)
        if not request_targets:
            self._run_final_payload_leak_check(
                data,
                request_targets,
                request_id,
                audit_started_at=started_at,
                call_type=call_type,
            )
            PII_PRE_CALLS.labels(result="skipped").inc()
            self._log_gateway_audit(
                request_id=request_id,
                data=data,
                started_at=started_at,
                status="allowed",
                policy_result="skipped",
                call_type=call_type,
            )
            return data

        if self.regulated_topic_policy_mode == "block":
            regulated_topic_findings = self._classify_regulated_topic_policy_targets(
                request_targets
            )
            if regulated_topic_findings:
                self._raise_regulated_topic_policy_blocked(
                    data,
                    request_id,
                    regulated_topic_findings,
                    audit_started_at=started_at,
                    call_type=call_type,
                )

        if self.pre_egress_policy_mode == "block":
            policy_findings = self._classify_pre_egress_policy_targets(request_targets)
            if policy_findings:
                self._raise_pre_egress_policy_blocked(
                    data,
                    request_id,
                    policy_findings,
                    audit_started_at=started_at,
                    call_type=call_type,
                )

        full_mapping: dict[str, str] = {}
        pii_mapping: dict[str, str] = {}
        dictionary_mapping: dict[str, str] = {}
        dictionary_rule_counts: dict[str, int] = {}
        entity_counts: dict[str, int] = {}
        blocked_entity_counts: dict[str, int] = {}
        synthetic_allowlist_findings: list[dict[str, str]] = []
        pending_updates = []

        for target, field in request_targets:
            content = target[field]
            if not content.strip():
                continue

            try:
                dictionary_result = self._apply_dictionary_substitutions(content)
                provider_content = dictionary_result.text
                if dictionary_result.rule_counts:
                    self._merge_dictionary_rule_counts(
                        dictionary_rule_counts,
                        dictionary_result.rule_counts,
                    )
                if dictionary_result.mapping:
                    dictionary_mapping.update(dictionary_result.mapping)

                entities = await self._analyze_text(provider_content)
                if not entities:
                    if provider_content != content:
                        pending_updates.append((target, field, content, provider_content))
                    continue

                (
                    entities,
                    current_allowlist_findings,
                ) = self._filter_synthetic_pii_allowlisted_entities(
                    provider_content,
                    entities,
                )
                synthetic_allowlist_findings.extend(current_allowlist_findings)
                if not entities:
                    if provider_content != content:
                        pending_updates.append((target, field, content, provider_content))
                    continue

                if self.pii_mode == "block":
                    normalized_entities = self._normalize_entities(
                        provider_content,
                        entities,
                        exclusion_spans=dictionary_result.replacement_spans,
                    )
                    entity_counts_for_block = (
                        self._entity_counts_from_normalized_entities(
                            normalized_entities
                        )
                    )
                    for entity_type, count in entity_counts_for_block.items():
                        blocked_entity_counts[entity_type] = (
                            blocked_entity_counts.get(entity_type, 0) + count
                        )
                    if provider_content != content:
                        pending_updates.append((target, field, content, provider_content))
                    continue

                masked_text, mapping = self._mask_text(
                    provider_content,
                    entities,
                    entity_counts,
                    exclusion_spans=dictionary_result.replacement_spans,
                )
                if mapping:
                    pii_mapping.update(mapping)
                    pending_updates.append((target, field, content, masked_text))
                elif provider_content != content:
                    pending_updates.append((target, field, content, provider_content))

            except DictionaryPolicyAmbiguousRequestError as e:
                PII_PRE_CALLS.labels(result="error").inc()
                self._log_gateway_audit(
                    request_id=request_id,
                    data=data,
                    started_at=started_at,
                    status="blocked"
                    if self.dictionary_substitutions_failure_mode == "fail_closed"
                    else "allowed",
                    policy_result=self.dictionary_substitutions_failure_mode,
                    call_type=call_type,
                    block_reason="dictionary_substitution_ambiguous"
                    if self.dictionary_substitutions_failure_mode == "fail_closed"
                    else None,
                    error_code="dictionary_substitution_ambiguous",
                    failure_operation="dictionary_substitution",
                    error_type=type(e).__name__,
                )
                return self._handle_dictionary_failure(
                    "ambiguous request",
                    e,
                    data,
                )
            except AnalyzerOverloadedError as e:
                PII_PRE_CALLS.labels(result="error").inc()
                self._log_gateway_audit(
                    request_id=request_id,
                    data=data,
                    started_at=started_at,
                    status="blocked",
                    policy_result="analyzer_overloaded",
                    call_type=call_type,
                    block_reason="analyzer_overloaded",
                    error_code="analyzer_overloaded",
                    failure_operation="analyzer",
                    error_type=type(e).__name__,
                )
                self._raise_analyzer_overloaded(e)
            except Exception as e:
                if self.pii_mode == "block" and blocked_entity_counts:
                    self._raise_blocked_request(
                        data,
                        request_id,
                        blocked_entity_counts,
                        audit_started_at=started_at,
                        call_type=call_type,
                        synthetic_allowlist_findings=synthetic_allowlist_findings,
                        dictionary_rule_counts=dictionary_rule_counts,
                    )
                self._run_final_payload_leak_check(
                    data,
                    request_targets,
                    request_id,
                    audit_started_at=started_at,
                    call_type=call_type,
                    synthetic_allowlist_findings=synthetic_allowlist_findings,
                    dictionary_rule_counts=dictionary_rule_counts,
                )
                PII_PRE_CALLS.labels(result="error").inc()
                self._log_gateway_audit(
                    request_id=request_id,
                    data=data,
                    started_at=started_at,
                    status="allowed"
                    if self.failure_mode == "fail_open"
                    else "blocked",
                    policy_result=self.failure_mode,
                    call_type=call_type,
                    block_reason="guardrail_failure"
                    if self.failure_mode == "fail_closed"
                    else None,
                    error_code=f"guardrail_{self.failure_mode}",
                    failure_operation="masking",
                    error_type=type(e).__name__,
                    **self._dictionary_substitution_audit_fields(
                        dictionary_rule_counts
                    ),
                    **self._synthetic_pii_allowlist_audit_fields(
                        synthetic_allowlist_findings
                    ),
                )
                return self._handle_failure("masking", e, data)

        full_mapping.update(dictionary_mapping)
        full_mapping.update(pii_mapping)

        if synthetic_allowlist_findings:
            self._emit_synthetic_pii_allowlist_applied(
                request_id,
                synthetic_allowlist_findings,
            )

        if blocked_entity_counts:
            self._raise_blocked_request(
                data,
                request_id,
                blocked_entity_counts,
                audit_started_at=started_at,
                call_type=call_type,
                synthetic_allowlist_findings=synthetic_allowlist_findings,
                dictionary_rule_counts=dictionary_rule_counts,
            )

        for target, field, _, masked_text in pending_updates:
            target[field] = masked_text

        entity_counts_for_audit = self._entity_counts_from_mapping(pii_mapping)
        try:
            self._run_final_payload_leak_check(
                data,
                request_targets,
                request_id,
                audit_started_at=started_at,
                call_type=call_type,
                redaction_count=len(pii_mapping),
                entity_counts=entity_counts_for_audit,
                synthetic_allowlist_findings=synthetic_allowlist_findings,
                dictionary_rule_counts=dictionary_rule_counts,
            )
        except litellm.UnprocessableEntityError:
            for target, field, original_text, _ in pending_updates:
                target[field] = original_text
            raise

        # Save mapping for post-call unmasking
        if full_mapping:
            try:
                await self._save_mapping(request_id, full_mapping)
            except Exception as e:
                for target, field, original_text, _ in pending_updates:
                    target[field] = original_text
                self._run_final_payload_leak_check(
                    data,
                    request_targets,
                    request_id,
                    audit_started_at=started_at,
                    call_type=call_type,
                    synthetic_allowlist_findings=synthetic_allowlist_findings,
                    dictionary_rule_counts=dictionary_rule_counts,
                )
                PII_PRE_CALLS.labels(result="error").inc()
                mapping_failure_mode = (
                    self.dictionary_substitutions_failure_mode
                    if dictionary_mapping
                    else self.failure_mode
                )
                self._log_gateway_audit(
                    request_id=request_id,
                    data=data,
                    started_at=started_at,
                    status="allowed"
                    if mapping_failure_mode == "fail_open"
                    else "blocked",
                    policy_result=mapping_failure_mode,
                    call_type=call_type,
                    redaction_count=len(pii_mapping),
                    entity_counts=entity_counts_for_audit,
                    block_reason="guardrail_failure"
                    if mapping_failure_mode == "fail_closed"
                    else None,
                    error_code=f"guardrail_{mapping_failure_mode}",
                    failure_operation="mapping_save",
                    error_type=type(e).__name__,
                    **self._dictionary_substitution_audit_fields(
                        dictionary_rule_counts
                    ),
                    **self._synthetic_pii_allowlist_audit_fields(
                        synthetic_allowlist_findings
                    ),
                )
                if dictionary_mapping:
                    return self._handle_dictionary_failure("mapping save", e, data)
                return self._handle_failure("mapping save", e, data)

            # Store internal mapping ID in data for post-call hooks.
            if not isinstance(data.get("metadata"), dict):
                data["metadata"] = {}
            data["metadata"][PII_REQUEST_ID_METADATA_KEY] = request_id
            entity_counts_for_log = entity_counts_for_audit
            if pii_mapping:
                self._record_entities(entity_counts_for_log)
                PII_MAPPING_SIZE.observe(len(pii_mapping))
                _safe_log(
                    logging.INFO,
                    "pii_guardrail_masked",
                    request_id=request_id,
                    masked_count=len(pii_mapping),
                    entity_counts=entity_counts_for_log,
                    mapping_ttl_seconds=self.mapping_ttl_seconds,
                )
            if dictionary_rule_counts:
                self._emit_dictionary_substitution_applied(
                    request_id,
                    dictionary_rule_counts,
                    len(dictionary_mapping),
                    self.mapping_ttl_seconds,
                )
            policy_result = (
                "masked_and_dictionary_substituted"
                if pii_mapping and dictionary_mapping
                else "masked"
                if pii_mapping
                else "dictionary_substituted"
            )
            PII_PRE_CALLS.labels(result=policy_result).inc()
            self._log_gateway_audit(
                request_id=request_id,
                data=data,
                started_at=started_at,
                status="allowed",
                policy_result=policy_result,
                call_type=call_type,
                redaction_count=len(pii_mapping),
                entity_counts=entity_counts_for_log,
                **self._dictionary_substitution_audit_fields(
                    dictionary_rule_counts
                ),
                **self._synthetic_pii_allowlist_audit_fields(
                    synthetic_allowlist_findings
                ),
            )
        else:
            PII_PRE_CALLS.labels(result="clean").inc()
            self._log_gateway_audit(
                request_id=request_id,
                data=data,
                started_at=started_at,
                status="allowed",
                policy_result="clean",
                call_type=call_type,
                **self._dictionary_substitution_audit_fields(
                    dictionary_rule_counts
                ),
                **self._synthetic_pii_allowlist_audit_fields(
                    synthetic_allowlist_findings
                ),
            )

        return data

    async def async_post_call_success_hook(
        self,
        data: dict,
        user_api_key_dict: UserAPIKeyAuth,
        response: Any,
    ) -> None:
        """Unmask PII in response after receiving from LLM."""
        if data.get("stream") is True and (
            self._streaming_restoration_done(data)
            or not isinstance(response, litellm.ModelResponse)
        ):
            PII_POST_CALLS.labels(result="skipped").inc()
            return

        request_id = self._get_response_request_id(data)
        if not request_id:
            PII_POST_CALLS.labels(result="skipped").inc()
            return

        # Load mapping
        try:
            mapping = await self._load_mapping(request_id)
        except Exception as e:
            PII_POST_CALLS.labels(result="error").inc()
            self._handle_failure("mapping load", e, data)
            return

        if not mapping:
            PII_POST_CALLS.labels(result="no_mapping").inc()
            _safe_log(
                logging.INFO,
                "pii_guardrail_no_mapping",
                request_id=request_id,
            )
            return

        # Unmask in response
        restored_fields = 0
        if isinstance(response, litellm.ModelResponse):
            for choice in response.choices:
                message = getattr(choice, "message", None)
                for target, field in self._iter_response_text_targets(message):
                    original_value = self._get_container_field(target, field)
                    restored_value = self._replace_placeholders(
                        original_value,
                        mapping,
                    )
                    if restored_value != original_value:
                        restored_fields += 1
                        self._set_container_field(target, field, restored_value)
        else:
            PII_POST_CALLS.labels(result="unsupported_response").inc()
            _safe_log(
                logging.WARNING,
                "pii_guardrail_unsupported_response",
                request_id=request_id,
                response_type=type(response).__name__,
                mapping_size=len(mapping),
            )
            return

        # Clean up Redis key
        try:
            await self._delete_mapping(request_id)
        except Exception as e:
            PII_FAIL_OPEN.labels(operation="mapping_delete").inc()
            _safe_log(
                logging.WARNING,
                "pii_guardrail_cleanup_failed",
                request_id=request_id,
                error_type=type(e).__name__,
            )

        PII_POST_CALLS.labels(
            result="restored" if restored_fields else "no_placeholders"
        ).inc()
        _safe_log(
            logging.INFO,
            "pii_guardrail_restored",
            request_id=request_id,
            mapping_size=len(mapping),
            restored_fields=restored_fields,
        )

    @classmethod
    def _iter_stream_delta_text_targets(
        cls,
        chunk: Any,
    ) -> list[tuple[int, Any, str]]:
        """Return stream delta text fields that can contain placeholders."""
        targets: list[tuple[int, Any, str]] = []
        choices = getattr(chunk, "choices", None)
        if not isinstance(choices, list):
            return targets

        for choice in choices:
            try:
                choice_index = int(cls._get_container_field(choice, "index") or 0)
            except (TypeError, ValueError):
                choice_index = 0

            delta = cls._get_container_field(choice, "delta")
            if delta is None:
                continue

            for field in ("content", "reasoning_content"):
                value = cls._get_container_field(delta, field)
                if isinstance(value, str):
                    targets.append((choice_index, delta, field))

        return targets

    @classmethod
    def _append_stream_delta_text(cls, delta: Any, field: str, text: str) -> None:
        """Append restored trailing text to a stream delta field."""
        current_value = cls._get_container_field(delta, field)
        if isinstance(current_value, str):
            cls._set_container_field(delta, field, current_value + text)
        else:
            cls._set_container_field(delta, field, text)

    def _flush_stream_choice(
        self,
        choice: Any,
        replacer: _StreamingPlaceholderReplacer,
    ) -> None:
        """Flush pending text for a finished streaming choice onto its delta."""
        try:
            choice_index = int(self._get_container_field(choice, "index") or 0)
        except (TypeError, ValueError):
            choice_index = 0

        delta = self._get_container_field(choice, "delta")
        if delta is None:
            return

        for field, text in replacer.flush_choice(choice_index).items():
            self._append_stream_delta_text(delta, field, text)

    @staticmethod
    def _build_stream_flush_chunk(
        choice_index: int,
        field: str,
        text: str,
    ) -> Any:
        """Build a final stream chunk for pending text when no finish chunk exists."""
        return litellm.ModelResponseStream(
            choices=[
                litellm.StreamingChoices(
                    index=choice_index,
                    delta={field: text},
                )
            ]
        )

    async def async_post_call_streaming_iterator_hook(
        self,
        user_api_key_dict: UserAPIKeyAuth,
        response: Any,
        request_data: dict,
    ):
        """Unmask placeholders in streaming response chunks."""
        request_id = self._get_response_request_id(request_data)
        if not request_id:
            PII_POST_CALLS.labels(result="skipped").inc()
            async for item in response:
                yield item
            return

        try:
            mapping = await self._load_mapping(request_id)
        except Exception as e:
            PII_POST_CALLS.labels(result="error").inc()
            self._handle_failure("stream mapping load", e, request_data)
            async for item in response:
                yield item
            return

        if not mapping:
            PII_POST_CALLS.labels(result="no_mapping").inc()
            _safe_log(
                logging.INFO,
                "pii_guardrail_no_mapping",
                request_id=request_id,
            )
            self._mark_streaming_restoration_done(request_data)
            async for item in response:
                yield item
            return

        replacer = _StreamingPlaceholderReplacer(mapping)
        restored_fields = 0
        try:
            async for item in response:
                for choice_index, target, field in self._iter_stream_delta_text_targets(
                    item
                ):
                    original_value = self._get_container_field(target, field)
                    restored_value = replacer.push(
                        (choice_index, field),
                        original_value,
                    )
                    if restored_value != original_value:
                        restored_fields += 1
                    self._set_container_field(target, field, restored_value)

                choices = getattr(item, "choices", None)
                if isinstance(choices, list):
                    for choice in choices:
                        if self._get_container_field(choice, "finish_reason") is not None:
                            self._flush_stream_choice(choice, replacer)

                yield item

            for (choice_index, field), text in replacer.flush_all().items():
                restored_fields += 1
                yield self._build_stream_flush_chunk(choice_index, field, text)
        finally:
            self._mark_streaming_restoration_done(request_data)
            try:
                await self._delete_mapping(request_id)
            except Exception as e:
                PII_FAIL_OPEN.labels(operation="mapping_delete").inc()
                _safe_log(
                    logging.WARNING,
                    "pii_guardrail_cleanup_failed",
                    request_id=request_id,
                    error_type=type(e).__name__,
                )

        PII_POST_CALLS.labels(
            result="restored" if restored_fields else "no_placeholders"
        ).inc()
        _safe_log(
            logging.INFO,
            "pii_guardrail_stream_restored",
            request_id=request_id,
            mapping_size=len(mapping),
            restored_fields=restored_fields,
        )

    @staticmethod
    def _replace_placeholders(text: str, mapping: dict[str, str]) -> str:
        """Replace placeholders with originals, longest keys first for stability."""
        for placeholder, original in sorted(
            mapping.items(), key=lambda item: len(item[0]), reverse=True
        ):
            text = text.replace(placeholder, original)
        return text
