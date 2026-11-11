"""Opt-in guardrail: reversible scalar masking for 54 reviewed env names.

The regular RuPIIGuardrail and service configuration are unchanged. Select this
class explicitly for local validation. Unsupported assignments keep the existing
pre-egress rejection. Values are never declared harmless because they look like
paths. No prompt values are written to logs.
"""

import copy
import json
import logging
import re
from contextvars import ContextVar
from dataclasses import dataclass, field

from litellm_guardrails import pii_guardrail as base
from litellm_guardrails.dictionary_policy import DictionarySubstitutionResult


MASKED_ENV_NAMES = frozenset({
    "PASSWORD", "PASS", "PWD", "PGPASSWORD", "MYSQL_PWD", "DB_PASSWORD",
    "DB_PASS", "DB_PWD", "DATABASE_PASSWORD", "LDAP_PASSWORD", "SSH_PASSWORD",
    "TOKEN", "ACCESS_TOKEN", "REFRESH_TOKEN", "AUTH_TOKEN", "API_TOKEN",
    "JIRA_TOKEN", "JIRA_API_TOKEN", "GITHUB_TOKEN", "CI_JOB_TOKEN",
    "SESSION_TOKEN", "BEARER_TOKEN", "ID_TOKEN", "API_KEY", "CLIENT_SECRET",
    "SECRET", "SECRET_KEY", "PRIVATE_KEY", "MASTER_KEY", "SALT_KEY",
    "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN",
    "JWT_SECRET", "DOCKER_AUTH_CONFIG", "SSH_PRIVATE_KEY", "DATABASE_URL",
    "REDIS_URL", "URI", "DSN", "CONNECTION_STRING", "SERVICE_CONNECTION_STRING",
    "AUTH_CONFIG", "REQUEST_URI", "REDIRECT_URI", "CALLBACK_URI", "BASE_URI",
    "PUBLIC_API_KEY", "NOT_A_SECRET", "TOKEN_1", "TOKEN_22", "APP_TOKEN",
    "APP_TOKEN_2", "DB_PASS_1",
})

# Preserve lexical values, including JSON escapes, rather than reserializing the
# input. Only complete JSON documents and complete simple assignment lines are
# accepted. Expressions and compound shell commands are not evaluated.
_JSON_STRING = re.compile(
    r'"(?P<key>[A-Z][A-Z0-9_]*)"\s*:\s*'
    r'"(?P<value>(?:[^"\\\r\n]|\\[^\r\n])*)"'
)
_LINE = re.compile(
    r'''(?mx)^[ \t]*(?:-[ \t]*)?(?:export[ \t]+)?
    (?P<key>[A-Z][A-Z0-9_]*)[ \t]*[=:][ \t]*
    (?:"(?P<double>(?:[^"\\\r\n]|\\[^\r\n])*)"
      |'(?P<single>[^'\r\n]*)'
      |(?P<bare>[^\s'";&|<>\[\]{}()]+))
    (?:[ \t]+\#.*)?[ \t]*$
    '''
)
_INBOUND_ALIAS = re.compile(r"<[A-Z][A-Z0-9_]*_[0-9]+>|\bENV_MASK_V1_[0-9]+\b")


@dataclass
class _MaskState:
    mapping: dict[str, str]
    trusted: set[str] = field(default_factory=set)
    names: set[str] = field(default_factory=set)
    values_for_scan: set[str] = field(default_factory=set)
    json_shapes: dict[int, object] = field(default_factory=dict)


_STATE: ContextVar[_MaskState | None] = ContextVar("env_assignment_masks_v1", default=None)


def assignment_spans(text: str) -> list[tuple[int, int, str]]:
    """Return complete supported scalar values; never guess a secret's type."""
    stripped = text.lstrip()
    if stripped.startswith(("{", "[")):
        try:
            json.loads(text)
        except (ValueError, TypeError):
            return []
        expressions = (_JSON_STRING,)
    else:
        expressions = (_LINE,)
    spans = []
    for expression in expressions:
        for match in expression.finditer(text):
            name = match.group("key")
            if name not in MASKED_ENV_NAMES:
                continue
            group = "value" if expression is _JSON_STRING else next(
                key for key in ("double", "single", "bare")
                if match.group(key) is not None
            )
            start, end = match.span(group)
            value = text[start:end]
            if not value or "$" in value or "`" in value or value in {"|", "|-", "|+", ">", ">-", ">+"}:
                continue
            spans.append((start, end, name))
    return spans


def _json_shape(value):
    if isinstance(value, dict):
        return {key: _json_shape(child) for key, child in value.items()}
    if isinstance(value, list):
        return [_json_shape(child) for child in value]
    if isinstance(value, str):
        return ("string",)
    return (type(value).__name__, value)


def _scan_assignments(text: str) -> str:
    """Exclude only request-generated aliases from assignment leak detection.

    This view is never sent to the provider. Other detectors see the original
    provider-bound text. Incoming aliases are rejected before this scope exists.
    """
    state = _STATE.get()
    if state is None:
        return text
    for token in sorted(state.trusted, key=len, reverse=True):
        literal = re.escape(token)
        expression = re.compile(
            r'(?<![\w])(?:"[A-Z][A-Z0-9_]*"|[A-Z][A-Z0-9_]*)'
            r"[ \t]*[=:][ \t]*(?:\"" + literal + r"\"|'" + literal
            + r"'|" + literal + r")(?=$|[\s,;}\]])"
        )
        text = expression.sub("", text)
    return text


class RuEnvAssignmentGuardrail(base.RuPIIGuardrail):
    """Local opt-in alternative; retains the standard post/streaming hooks."""

    @classmethod
    def _classify_pre_egress_policy_text(cls, text):
        original = base.RuPIIGuardrail._classify_pre_egress_policy_text(text)
        scanned = base.RuPIIGuardrail._classify_pre_egress_policy_text(
            _scan_assignments(text)
        )
        env_remains = any(f["rule_id"] == "env_secret_assignment" for f in scanned)
        return [f for f in original if f["rule_id"] != "env_secret_assignment" or env_remains]

    def _classify_final_payload_leak_check_text(self, text):
        original = super()._classify_final_payload_leak_check_text(text)
        scanned = super()._classify_final_payload_leak_check_text(_scan_assignments(text))
        env_remains = any(f["rule_id"] == "env_secret_assignment" for f in scanned)
        return [f for f in original if f["rule_id"] != "env_secret_assignment" or env_remains]

    def _run_final_payload_leak_check(self, data, *args, **kwargs):
        super()._run_final_payload_leak_check(data, *args, **kwargs)
        state = _STATE.get()
        if state is None:
            return
        targets = self._iter_request_text_targets(data)
        for index, expected in state.json_shapes.items():
            try:
                shape = _json_shape(json.loads(targets[index][0][targets[index][1]]))
            except (ValueError, TypeError, IndexError):
                raise base.HTTPException(status_code=422, detail={"code": "env_mask_json_structure"})
            if shape != expected:
                raise base.HTTPException(status_code=422, detail={"code": "env_mask_json_structure"})
        # A known value repeated in a comment, another field or tool schema must
        # not escape solely because the first assignment was successfully masked.
        for text in self._iter_provider_bound_final_payload_texts(data):
            for alias in sorted(state.trusted, key=len, reverse=True):
                text = re.sub(r"(?<!\w)" + re.escape(alias) + r"(?!\w)", "", text)
            if any(value and value in text for value in state.values_for_scan):
                raise base.HTTPException(
                    status_code=422, detail={"code": "env_mask_uncovered_value"}
                )

    def _mask_text(self, *args, **kwargs):
        state = _STATE.get()
        if state is not None and len(args) >= 2:
            source, entities = args[:2]
            protected = [
                (match.start(), match.end())
                for token in state.mapping
                for match in re.finditer(re.escape(token), source)
            ]
            # A finding wholly within an already protected generated alias has
            # no raw value to hide. Cross-boundary evidence is preserved.
            entities = [entity for entity in entities if not any(
                start <= int(entity.get("start", -1))
                < int(entity.get("end", -1)) <= end
                for start, end in protected
            )]
            args = (source, entities, *args[2:])
        text, mapping = super()._mask_text(*args, **kwargs)
        if state is not None:
            state.trusted.update(mapping)
        return text, mapping

    def _apply_dictionary_substitutions(self, text, existing_mapping=None):
        result = super()._apply_dictionary_substitutions(text, existing_mapping)
        state = _STATE.get()
        if state is None:
            return result
        local = {key: value for key, value in state.mapping.items() if key in result.text}
        ranges = list(result.replacement_spans)
        for token in local:
            ranges.extend((m.start(), m.end()) for m in re.finditer(re.escape(token), result.text))
        # Reuse atomic alias expansion and request-scoped Redis restoration.
        return DictionarySubstitutionResult(
            result.text, {**result.mapping, **local}, tuple(sorted(ranges)), result.rule_counts
        )

    async def _prepare_responses_state(self, data, auth, call_type):
        inherited = await super()._prepare_responses_state(data, auth, call_type)
        state = _STATE.get()
        if state is None or not inherited:
            return inherited
        # Continuations inherit immutable mappings. Allocate a new namespace
        # range rather than overwriting ENV_MASK_V1_1 from a previous turn.
        state.values_for_scan.update(value for key, value in inherited.items()
            if re.fullmatch(r"ENV_MASK_V1_[0-9]+", key))
        largest = max((int(match.group(1)) for key in inherited
            if (match := re.fullmatch(r"ENV_MASK_V1_([0-9]+)", key))), default=0)
        if largest:
            renamed = {old: f"ENV_MASK_V1_{largest + index}" for index, old
                in enumerate(state.mapping, 1)}
            pattern = re.compile(r"\b(?:" + "|".join(map(re.escape, renamed)) + r")\b")
            for target, field in self._iter_request_text_targets(data):
                target[field] = pattern.sub(lambda match: renamed[match.group()], target[field])
            state.mapping = {renamed[key]: value for key, value in state.mapping.items()}
            state.trusted = set(state.mapping)
        return inherited

    async def async_pre_call_hook(self, user_api_key_dict, cache, data, call_type=None):
        targets = self._iter_request_text_targets(data)
        original_findings = base.RuPIIGuardrail._classify_pre_egress_policy_targets(targets)
        # Inspect original content before hiding values: operational/topic rules
        # must not lose evidence located within a proposed secret span.
        other_blocks = any(f["rule_id"] != "env_secret_assignment" for f in original_findings)
        topic_blocks = self.regulated_topic_policy_mode == "block" and bool(
            self._classify_regulated_topic_policy_targets(targets)
        )
        planned = [assignment_spans(target[field]) for target, field in targets]
        if other_blocks or topic_blocks or self.pii_mode != "mask" or not any(planned):
            return await super().async_pre_call_hook(user_api_key_dict, cache, data, call_type)

        if self.pre_egress_policy_mode != "block" or self.final_payload_leak_check_mode != "block":
            raise base.HTTPException(status_code=503, detail={"code": "env_mask_requires_checks"})
        texts = self._iter_provider_bound_final_payload_texts(data)
        if any(_INBOUND_ALIAS.search(text) for text in texts):
            raise base.HTTPException(status_code=422, detail={"code": "env_mask_alias_collision"})

        staged = copy.deepcopy(data)
        state = _MaskState(mapping={})
        for index, (target, field) in enumerate(targets):
            if target[field].lstrip().startswith(("{", "[")):
                try:
                    state.json_shapes[index] = _json_shape(json.loads(target[field]))
                except (ValueError, TypeError):
                    pass
        for (target, field), spans in zip(self._iter_request_text_targets(staged), planned):
            text = target[field]
            for start, end, name in reversed(spans):
                alias = f"ENV_MASK_V1_{len(state.mapping) + 1}"
                value = text[start:end]
                state.mapping[alias] = value
                state.values_for_scan.add(value)
                if text.lstrip().startswith(("{", "[")):
                    state.values_for_scan.add(json.loads('"' + value + '"'))
                else:
                    # JSON-style decoding is inappropriate for Windows paths;
                    # shell double quotes only unescape quote/backslash here.
                    state.values_for_scan.add(re.sub(r'\\(["\\])', r'\1', value))
                state.names.add(name)
                text = text[:start] + alias + text[end:]
            target[field] = text
        state.trusted.update(state.mapping)
        context_token = _STATE.set(state)
        try:
            processed = await super().async_pre_call_hook(
                user_api_key_dict, cache, staged, call_type
            )
            if not isinstance(processed, dict) or not self._get_response_request_id(processed):
                # fail_open in the underlying guardrail cannot discard an ENV
                # mapping after a dependency failure. Do not return such a body.
                raise base.HTTPException(status_code=503, detail={"code": "env_mask_mapping_required"})
            base._safe_log(
                logging.INFO, "env_assignment_masked",
                request_id=self._get_response_request_id(processed),
                name_count=len(state.names), value_count=len(state.mapping),
                policy_version="env54-v1",
            )
            return processed
        finally:
            _STATE.reset(context_token)
