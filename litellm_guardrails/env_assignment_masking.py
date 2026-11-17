"""Opt-in reversible masking of complete scalar values for reviewed ENV names."""

import copy
import json
import logging
import re
import time
from contextvars import ContextVar
from dataclasses import dataclass, field
from itertools import pairwise

from litellm_guardrails import pii_guardrail as base
from litellm_guardrails.dictionary_policy import DictionarySubstitutionResult
from litellm_guardrails.restoration_mapping import RestorationMapping, json_atom_spans


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
MAX_ENV_ASSIGNMENTS = 256
MAX_ENV_VALUE_BYTES = 64 * 1024

_LINE = re.compile(
    r'''(?mx)^[ \t]*(?P<list>-[ \t]+)?(?P<export>export[ \t]+)?
    (?P<key>[A-Z][A-Z0-9_]*)[ \t]*(?P<op>[=:])[ \t]*
    (?:"(?P<double>(?:[^"\\\r\n]|\\[^\r\n])*)"
      |'(?P<single>[^'\r\n]*)'
      |(?P<bare>[^\s'";&|<>\[\]{}()]+))
    (?:[ \t]+\#.*)?[ \t]*\r?$
    '''
)
_JSON_MEMBER = re.compile(r"[ \t\r\n]*:[ \t\r\n]*")
_YAML_IGNORABLE_LINE = re.compile(r"[ \t]*(?:#[^\r\n]*)?\r?\n")
_ENV_ALIAS_BODY = r"ENV_MASK_V1_[1-9][0-9]*"
_ENV_ALIAS = re.compile(r"(?<!\w)(?:<" + _ENV_ALIAS_BODY + r">|" + _ENV_ALIAS_BODY + r")(?!\w)")
_INBOUND_ALIAS = re.compile(r"<[A-Z][A-Z0-9_]*_[0-9]+>|\bENV_MASK_V1_[0-9]+\b")


@dataclass(frozen=True)
class _Assignment:
    name: str
    key_start: int
    key_end: int
    start: int
    end: int
    syntax: str


@dataclass
class _MaskState:
    plan: list
    count: int
    size: int
    mapping: RestorationMapping = field(default_factory=RestorationMapping)
    fields: dict[tuple[int, str], dict[str, str]] = field(default_factory=dict)
    json_shapes: dict[tuple[int, str], object] = field(default_factory=dict)
    views: dict[str, str] = field(default_factory=dict)
    known: re.Pattern | None = None
    saved: bool = False


_STATE: ContextVar[_MaskState | None] = ContextVar("env_assignment_masks_v1", default=None)


def _check_budget():
    deadline = base._ANALYSIS_DEADLINE.get()
    if deadline is not None and time.monotonic() >= deadline:
        raise TimeoutError


def _check_limit(count, size):
    if count > MAX_ENV_ASSIGNMENTS or size > MAX_ENV_VALUE_BYTES:
        raise base.HTTPException(status_code=413, detail={"code": "env_mask_limit_exceeded"})


def _byte_length(text):
    return len(text.encode("utf-8", errors="surrogatepass"))


def _is_json_container(text):
    return text.lstrip().startswith(("{", "["))


def _following_indentation(text, line_end):
    """Indent of the next content line; a deeper line may continue YAML."""
    pos = line_end + 1
    while line := _YAML_IGNORABLE_LINE.match(text, pos):
        pos = line.end()
    end = pos
    while end < len(text) and text[end] in " \t":
        end += 1
    return end - pos if end < len(text) else 0


def _previous_shell_continuation(text, start):
    if not start:
        return False
    before = text.rfind("\n", 0, start - 1) + 1
    return text[before:start - 1].rstrip("\r").endswith("\\")


def _assignments(text):
    """Yield only complete, supported scalar spans in source coordinates."""
    if _is_json_container(text):
        _, strings = json_atom_spans(text)
        for (left, right), (start, end) in pairwise(strings):
            member = _JSON_MEMBER.match(text, right + 1)
            if not member or member.end() != start - 1 or start == end:
                continue
            name = json.loads(text[left - 1:right + 1])
            if name in MASKED_ENV_NAMES:
                yield _Assignment(name, left, right, start, end, "json")
        return

    for match in _LINE.finditer(text):
        name = match.group("key")
        if name not in MASKED_ENV_NAMES or _previous_shell_continuation(text, match.start()):
            continue
        syntax = next(key for key in ("double", "single", "bare") if match.group(key) is not None)
        value = match.group(syntax)
        if not value or "$" in value or "`" in value:
            continue
        if syntax == "bare" and value.endswith("\\"):
            continue
        yaml_mapping = match.group("op") == ":"
        yaml_list = match.group("list") is not None
        if yaml_mapping and match.group("export"):
            continue
        if yaml_mapping and syntax == "bare" and value.startswith(("*", "!", "@")):
            continue
        if (yaml_mapping and syntax == "bare") or (yaml_list and not yaml_mapping):
            column = match.start("key" if yaml_mapping else "list") - match.start()
            if _following_indentation(text, match.end()) > column:
                continue
        start, end = match.span(syntax)
        yield _Assignment(name, *match.span("key"), start, end, syntax)


def assignment_spans(text):
    return [(item.start, item.end, item.name) for item in _assignments(text)]


def _json_shape(text):
    def shape(value):
        if isinstance(value, tuple):
            return ("object", tuple((key, shape(child)) for key, child in value))
        if isinstance(value, list):
            return ("array", tuple(map(shape, value)))
        if isinstance(value, str):
            return ("string",)
        return (type(value).__name__, value)

    # The default dict decoder would discard repeated member names.
    return shape(json.loads(text, object_pairs_hook=tuple))


def _replace_spans(text, replacements):
    parts = []
    cursor = 0
    for start, end, replacement in replacements:
        parts.extend((text[cursor:start], replacement))
        cursor = end
    parts.append(text[cursor:])
    return "".join(parts)


def _decoded_json_strings(text):
    if "\\" in text and text.lstrip().startswith(("{", "[", '"')):
        _, strings = json_atom_spans(text)
        for start, end in strings:
            yield json.loads(text[start - 1:end + 1])


def _verified_views(targets, state):
    """Grant a scan exemption only to a complete alias in its recorded field."""
    views = {}
    for target, field in targets:
        key = (id(target), field)
        aliases = state.fields.get(key)
        if not aliases and key not in state.json_shapes:
            continue
        _check_budget()
        text = target[field]
        if key in state.json_shapes:
            try:
                shape = _json_shape(text)
            except (ValueError, RecursionError):
                raise base.HTTPException(status_code=422, detail={"code": "env_mask_json_structure"}) from None
            if shape != state.json_shapes[key]:
                raise base.HTTPException(status_code=422, detail={"code": "env_mask_json_structure"})
        if not aliases:
            continue
        expected = set(aliases)
        replacements = []
        for item in _assignments(text):
            alias = text[item.start:item.end]
            if alias not in expected or aliases[alias] != item.name:
                continue
            expected.remove(alias)
            # Neutralize the key only, leaving comments and all other content visible.
            replacements.append((item.key_start, item.key_end, "RU_MASKED_VALUE"))
        if expected:
            raise base.HTTPException(status_code=422, detail={"code": "env_mask_assignment_structure"})
        views[text] = _replace_spans(text, replacements)
    return views


def _scan_env_rule(text, classify):
    original = classify(text)
    state = _STATE.get()
    view = state.views.get(text) if state is not None else None
    if view is None or not any(f["rule_id"] == "env_secret_assignment" for f in original):
        return original
    if any(f["rule_id"] == "env_secret_assignment" for f in classify(view)):
        return original
    return [f for f in original if f["rule_id"] != "env_secret_assignment"]


def _known_value_visible(text, state):
    cursor = 0
    for match in _ENV_ALIAS.finditer(text):
        if match.group() not in state.mapping:
            continue
        if state.known.search(text, cursor, match.start()):
            return True
        cursor = match.end()
    return state.known.search(text, cursor) is not None


class RuEnvAssignmentGuardrail(base.RuPIIGuardrail):
    """Opt-in alternative retaining the ordinary post-call and streaming hooks."""

    # LiteLLM discovers these capabilities in the registered leaf class's
    # __dict__, so ordinary Python inheritance alone silently skips them.
    # Bind the original methods directly to preserve their cleanup lifecycle.
    async_pre_call_hook = base.RuPIIGuardrail.async_pre_call_hook
    async_post_call_streaming_iterator_hook = base.RuPIIGuardrail.async_post_call_streaming_iterator_hook

    @classmethod
    def _classify_pre_egress_policy_text(cls, text):
        return _scan_env_rule(text, super()._classify_pre_egress_policy_text)

    def _classify_final_payload_leak_check_text(self, text):
        return _scan_env_rule(text, super()._classify_final_payload_leak_check_text)

    def _run_final_payload_leak_check(self, data, request_targets, *args, **kwargs):
        state = _STATE.get()
        if state is not None and state.fields:
            state.views = _verified_views(request_targets, state)
        super()._run_final_payload_leak_check(data, request_targets, *args, **kwargs)
        if state is None or state.known is None:
            return
        for text in self._iter_provider_bound_final_payload_texts(data):
            _check_budget()
            if (_known_value_visible(text, state) or any(
                _known_value_visible(value, state) for value in _decoded_json_strings(text)
            )):
                raise base.HTTPException(status_code=422, detail={"code": "env_mask_uncovered_value"})

    def _apply_dictionary_substitutions(self, text, existing_mapping=None):
        result = super()._apply_dictionary_substitutions(text, existing_mapping)
        state = _STATE.get()
        if state is None or not state.mapping:
            return result
        before = [match.group() for match in _ENV_ALIAS.finditer(text)]
        after = list(_ENV_ALIAS.finditer(result.text))
        if before != [match.group() for match in after]:
            raise base.HTTPException(status_code=422, detail={"code": "env_mask_alias_collision"})
        combined = RestorationMapping(result.mapping)
        ranges = list(result.replacement_spans)
        substitutions = iter(result.replacement_spans)
        current = next(substitutions, None)
        for match in after:
            alias = match.group()
            if alias in state.mapping:
                while current is not None and current[1] <= match.start():
                    current = next(substitutions, None)
                if alias in combined or (current is not None and current[0] < match.end()):
                    raise base.HTTPException(status_code=422, detail={"code": "env_mask_alias_collision"})
                if alias in state.mapping.json_strings:
                    combined.add_string(alias, state.mapping[alias])
                else:
                    combined[alias] = state.mapping[alias]
                ranges.append(match.span())
        return DictionarySubstitutionResult(
            result.text, combined, tuple(sorted(ranges)), result.rule_counts,
        )

    async def _save_mapping(self, request_id, mapping):
        state = _STATE.get()
        if state is not None and state.mapping:
            if (any(mapping.get(key) != value for key, value in state.mapping.items())
                    or not state.mapping.json_strings <= getattr(mapping, "json_strings", set())):
                raise base.HTTPException(status_code=503, detail={"code": "env_mask_mapping_required"})
        await super()._save_mapping(request_id, mapping)
        if state is not None:
            state.saved = True

    async def _prepare_responses_state(self, data, auth, call_type):
        inherited = await super()._prepare_responses_state(data, auth, call_type)
        state = _STATE.get()
        if state is None:
            return inherited
        previous = {key: value for key, value in inherited.items() if _ENV_ALIAS.fullmatch(key)}
        if not state.count and not previous:
            return inherited
        _check_limit(state.count + len(previous), state.size + sum(map(_byte_length, previous.values())))
        collision = _INBOUND_ALIAS if state.count else _ENV_ALIAS
        for text in self._iter_provider_bound_final_payload_texts(data):
            _check_budget()
            if collision.search(text) or any(collision.search(value) for value in _decoded_json_strings(text)):
                raise base.HTTPException(status_code=422, detail={"code": "env_mask_alias_collision"})
        known = set(previous.values())
        typed = getattr(inherited, "json_strings", ())
        for alias, value in previous.items():
            if alias in typed:
                known.add(json.loads('"' + value + '"'))
            else:
                # Bare mappings keep the source spelling, not the original quote style.
                known.add(re.sub(r"\\(.)", r"\1", value))
        index = max((int(key.rstrip(">").rsplit("_", 1)[1]) for key in previous), default=0)
        targets = self._iter_request_text_targets(data)
        if state.plan:
            for target, field in targets:
                _check_budget()
                if _is_json_container(target[field]):
                    try:
                        state.json_shapes[id(target), field] = _json_shape(target[field])
                    except (ValueError, RecursionError):
                        pass
        for target, field, assignments in state.plan:
            _check_budget()
            text = target[field]
            field_key = (id(target), field)
            aliases = state.fields.setdefault(field_key, {})
            if _is_json_container(text) and field_key not in state.json_shapes:
                raise base.HTTPException(status_code=422, detail={"code": "env_mask_json_structure"})
            replacements = []
            for item in assignments:
                index += 1
                value = text[item.start:item.end]
                alias = f"ENV_MASK_V1_{index}"
                known.add(value)
                if item.syntax == "json":
                    alias = f"<{alias}>"
                    state.mapping.add_string(alias, value)
                    known.add(json.loads('"' + value + '"'))
                else:
                    state.mapping[alias] = value
                    if item.syntax == "double":
                        known.add(re.sub(r'\\(["\\])', r'\1', value))
                    elif item.syntax == "bare":
                        known.add(re.sub(r"\\(.)", r"\1", value))
                aliases[alias] = item.name
                replacements.append((item.start, item.end, alias))
            target[field] = _replace_spans(text, replacements)
        known.discard("")
        if known:
            state.known = re.compile("|".join(re.escape(value) for value in sorted(known)))
        if state.fields:
            state.views = _verified_views(targets, state)
        return inherited

    async def _pre_call_with_budget(
        self, user_api_key_dict, cache, data, call_type, started_at, request_id, request_targets,
    ):
        run_base = super()._pre_call_with_budget
        planned = []
        count = size = 0
        if self.pii_mode == "mask":
            for index, (target, field) in enumerate(request_targets):
                _check_budget()
                assignments = []
                text = target[field]
                for item in _assignments(text):
                    count += 1
                    size += _byte_length(text[item.start:item.end])
                    _check_limit(count, size)
                    assignments.append(item)
                if assignments:
                    planned.append((index, assignments))
        if not planned and not data.get("previous_response_id"):
            return await run_base(user_api_key_dict, cache, data, call_type, started_at, request_id, request_targets)
        if planned:
            original = base.RuPIIGuardrail._classify_pre_egress_policy_targets(request_targets)
            other_blocks = any(f["rule_id"] != "env_secret_assignment" for f in original)
            topic_blocks = self.regulated_topic_policy_mode == "block" and bool(
                self._classify_regulated_topic_policy_targets(request_targets)
            )
            if other_blocks or topic_blocks:
                return await run_base(user_api_key_dict, cache, data, call_type, started_at, request_id, request_targets)
            if self.pre_egress_policy_mode != "block" or self.final_payload_leak_check_mode != "block":
                raise base.HTTPException(status_code=503, detail={"code": "env_mask_requires_checks"})
            data = copy.deepcopy(data)
            request_targets = self._iter_request_text_targets(data)
        state = _MaskState([(*request_targets[index], spans) for index, spans in planned], count, size)
        token = _STATE.set(state)
        try:
            result = await run_base(user_api_key_dict, cache, data, call_type, started_at, request_id, request_targets)
            if planned:
                if not state.saved or not isinstance(result, dict) or not self._get_response_request_id(result):
                    raise base.HTTPException(status_code=503, detail={"code": "env_mask_mapping_required"})
                base._safe_log(logging.INFO, "env_assignment_masked", request_id=request_id,
                               name_count=len({name for aliases in state.fields.values() for name in aliases.values()}),
                               value_count=len(state.mapping), policy_version="env54-v1")
            return result
        finally:
            _STATE.reset(token)
