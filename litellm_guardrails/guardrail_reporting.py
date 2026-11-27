"""Per-execution decisions and their content-free LiteLLM spend-log projection."""
import asyncio
from contextvars import ContextVar
import json
import logging
import re
import time
from typing import Literal, TypedDict


class Decision(TypedDict, total=False):
    policy_result: str
    status: str
    rules: list[str]
    rule_counts: dict[str, int]
    dictionary_substitution_count: int
    entity_counts: dict[str, int]  # committed masks, never blocked occurrences
    detected_entity_count: dict[str, int]
    analysis: Literal["not_started", "partial", "complete"]
    error_code: str | None
    failure_operation: str | None
    error_type: str | None


current: ContextVar[Decision | None] = ContextVar("ru_guardrail_decision", default=None)
LABEL = re.compile(r"[a-z][a-z0-9_:-]{0,79}|[A-Z][A-Z0-9_]{0,63}")
ERROR_TYPE = re.compile(r"[A-Za-z][A-Za-z0-9_]{0,79}")
SUCCESS = {
    "clean", "skipped", "history_inherited", "no_mapping", "no_placeholders",
    "restored", "unsupported_response",
}
INTERVENTION = {
    "masked", "dictionary_substituted", "masked_and_dictionary_substituted",
    "pii_blocked", "regulated_topic_policy_blocked", "pre_egress_policy_blocked",
    "final_payload_leak_check_blocked", "attachment_invalid", "attachment_too_large",
    "attachment_reference_unsupported", "attachment_format_unsupported",
}
FAILED = "guardrail_failed_to_respond"
# Public Analyzer types; custom recognizers still contribute to the PII total.
ENTITY_TYPES = frozenset("""
    PHONE_NUMBER EMAIL_ADDRESS RU_INN RU_KPP RU_OGRN RU_OGRNIP RU_BIK RU_SETTLEMENT_ACCOUNT
    RU_CORRESPONDENT_ACCOUNT RU_SNILS RU_PASSPORT CREDIT_CARD RU_ADDRESS INTERNAL_IP
    INTERNAL_DOMAIN HOSTNAME DB_URL JWT BEARER_TOKEN PRIVATE_KEY BASE64_DATA API_KEY
    SECRET_KEY AUTH_TOKEN LOGIN PASSWORD PERSON LOCATION ORGANIZATION CONTRACT_NUMBER
""".split())
# Only rules compiled into the proxy can be shown to an operator. IDs supplied
# by dictionary / regulated-topic configuration can themselves be secret data.
PUBLIC_RULES = frozenset({
    "env_secret_assignment", "kubeconfig_payload", "nginx_config_payload",
    "service_manifest_payload", "log_or_stacktrace_payload",
    "configured_canary", "bearer_token", "jwt_token",
    "private_key_marker", "provider_key",
    "aml_cft_internal_controls", "sanctions_watchlist_matching",
    "transaction_monitoring_thresholds", "suspicious_activity_playbook",
    "compliance_bypass_procedure",
})


def _label(value, pattern=LABEL):
    return value if isinstance(value, str) and pattern.fullmatch(value) else None


def _counts(values):
    return {k: v for k, v in (values or {}).items() if _label(k) and type(v) is int and v > 0}


def _safe_entity_counts(values):
    safe = {}
    for entity, count in (values or {}).items():
        if type(count) is int and count > 0:
            key = entity if entity in ENTITY_TYPES else "PII"
            safe[key] = safe.get(key, 0) + count
    return safe


def note_analysis(state):
    decision = current.get()
    if decision is not None:
        decision["analysis"] = state


def note_detections(counts):
    decision = current.get()
    if decision is not None:
        detected = decision.setdefault("detected_entity_count", {})
        for entity, count in _safe_entity_counts(counts).items():
            detected[entity] = detected.get(entity, 0) + count


def set_decision(policy_result, *, decision=None, status=None, rules=None, rule_counts=None,
                 entity_counts=None, error_code=None, failure_operation=None, error_type=None,
                 dictionary_substitution_count=None):
    if decision is None:
        decision = current.get()
    if decision is None:
        return
    if status is None:
        status = ("success" if policy_result in SUCCESS else
                  "guardrail_intervened" if policy_result in INTERVENTION else FAILED)
    decision.update(
        policy_result=policy_result, status=status,
        rules=sorted({r for r in rules or () if r in PUBLIC_RULES})
        if policy_result != "regulated_topic_policy_blocked" else [],
        rule_counts={k: v for k, v in _counts(rule_counts).items() if k in PUBLIC_RULES}
        if policy_result != "regulated_topic_policy_blocked" else {},
        entity_counts=_safe_entity_counts(entity_counts),
        error_code=_label(error_code), failure_operation=_label(failure_operation),
        error_type=_label(error_type, ERROR_TYPE),
    )
    if type(dictionary_substitution_count) is int and dictionary_substitution_count > 0:
        decision["dictionary_substitution_count"] = dictionary_substitution_count


def stream_failure(decision, result, operation, error=None):
    """Cleanup and exception translation must preserve the first diagnosed failure."""
    if decision is not None and decision.get("status") != FAILED:
        set_decision(result, decision=decision, status=FAILED, failure_operation=operation,
                     error_type=type(error).__name__ if error is not None else None)


_INFORMATION_KEY = "standard_logging_guardrail_information"


def _dict_cards(value):
    """Return only structurally valid entries from a guardrail-card sequence."""
    if not isinstance(value, (list, tuple)):
        return []
    return [entry for entry in value if isinstance(entry, dict)]


def _metadata_cards(bucket):
    """Normalize a metadata bucket without letting malformed input suppress a card."""
    value = bucket.get(_INFORMATION_KEY)
    if isinstance(value, list):
        cards = _dict_cards(value)
        if len(cards) != len(value):
            value[:] = cards
        return value
    cards = _dict_cards(value)
    bucket[_INFORMATION_KEY] = cards
    return cards


def _request_cards(data):
    bucket = data.get("litellm_metadata") if "litellm_metadata" in data else data.get("metadata")
    return _dict_cards(bucket.get(_INFORMATION_KEY)) if isinstance(bucket, dict) else []


_CREATED_CARDS = "_ru_guardrail_created_cards"


def _created_cards(logging_obj):
    attributes = getattr(logging_obj, "__dict__", None)
    if not isinstance(attributes, dict):
        return None
    created = attributes.get(_CREATED_CARDS)
    if not isinstance(created, list):
        # Keep references, not integer IDs: deepcopy of the request (ENV
        # masking) must preserve the association with its server-created cards.
        created = []
        attributes[_CREATED_CARDS] = created
    return created


def clear_inbound_information(data):
    """Discard caller-supplied cards, retaining earlier executions in this request."""
    created = _created_cards(data.get("litellm_logging_obj"))
    trusted_ids = {id(card) for card in created or ()}
    for field in ("metadata", "litellm_metadata"):
        bucket = data.get(field)
        if not isinstance(bucket, dict) or _INFORMATION_KEY not in bucket:
            continue
        cards = _metadata_cards(bucket)
        cards[:] = [card for card in cards if id(card) in trusted_ids]
        if not cards:
            bucket.pop(_INFORMATION_KEY, None)


def write_information(guardrail, data, mode, decision: Decision, started_wall, started_perf):
    if not guardrail.guardrail_name:
        return
    try:
        from litellm.litellm_core_utils.core_helpers import get_or_create_metadata_bucket

        _, bucket = get_or_create_metadata_bucket(data)
        cards = _metadata_cards(bucket)
        logging_obj = data.get("litellm_logging_obj")
        created = _created_cards(logging_obj)

        def matches(entry):
            return (entry.get("guardrail_name") == guardrail.guardrail_name
                    and entry.get("guardrail_mode") == mode)

        existing = [entry for entry in cards if matches(entry)]
        should_write = True
        if existing:
            if created is None:
                return
            created_ids = {id(card) for card in created}
            trusted = [entry for entry in existing if id(entry) in created_ids]
            if trusted:
                # Keep the one server-created result, but discard any duplicate
                # entries that arrived from an untrusted metadata snapshot.
                keep = id(trusted[0])
                cards[:] = [entry for entry in cards if not matches(entry) or id(entry) == keep]
                should_write = False
            else:
                cards[:] = [entry for entry in cards if not matches(entry)]

        if should_write:
            result = decision.get("policy_result", "guardrail_error")
            details = {k: decision[k] for k in (
                "rules", "rule_counts", "dictionary_substitution_count",
                "error_code", "failure_operation", "error_type"
            ) if decision.get(k)}
            masked = decision.get("entity_counts", {})
            detected = decision.get("detected_entity_count", {})
            guardrail.add_standard_logging_guardrail_information_to_request_data(
                guardrail_json_response={"policy_result": result, **details},
                request_data=data, guardrail_provider="ru-llm-proxy", event_type=mode,
                guardrail_status=decision.get("status", FAILED),
                start_time=started_wall, end_time=time.time(),
                duration=max(0., time.perf_counter() - started_perf),
                masked_entity_count=masked,
                tracing_detail={
                    "guardrail_action": result,
                    "violation_categories": sorted(set(decision.get("rules", ())) | masked.keys() | detected.keys()),
                    # 1.98.0 copies tracing_detail into the spend entry before independently
                    # redacting guardrail_response. Never copy the request/exception here.
                    "ru_guardrail_report": {
                        "version": 1, "analysis": decision.get("analysis", "not_started"),
                        "detected_entity_count": detected, **details,
                    },
                },
            )
            cards = _metadata_cards(bucket)
            if created is not None:
                created.extend(entry for entry in cards if matches(entry))
        # A pre-call exception bypasses LiteLLM's normal model-call setup. On
        # Responses SSE its logging object may likewise retain an earlier
        # metadata snapshot. Keep the native logging object's metadata in sync
        # before its ordinary success/failure callback snapshots the spend row.
        if logging_obj is not None:
            params = getattr(logging_obj, "litellm_params", None)
            if isinstance(params, dict):
                for field in ("metadata", "litellm_metadata"):
                    internal = params.setdefault(field, {})
                    if not isinstance(internal, dict):
                        continue
                    existing = _metadata_cards(internal)
                    if existing is cards:
                        continue
                    positions = {
                        (entry.get("guardrail_name"), entry.get("guardrail_mode")): index
                        for index, entry in enumerate(existing)
                    }
                    for entry in cards:
                        identity = (entry.get("guardrail_name"), entry.get("guardrail_mode"))
                        if identity in positions:
                            # The request bucket is authoritative. In particular,
                            # replace a caller-supplied card retained in an early
                            # LiteLLM metadata snapshot instead of treating it as
                            # a legitimate duplicate of the server decision.
                            existing[positions[identity]] = entry
                        else:
                            positions[identity] = len(existing)
                            existing.append(entry)
                details_obj = getattr(logging_obj, "model_call_details", None)
                if isinstance(details_obj, dict):
                    details_obj["litellm_params"] = params
                    # Responses streaming can assemble this standard payload at
                    # the provider's terminal event, before our iterator's
                    # finally block records the restoration result.
                    payload = details_obj.get("standard_logging_object")
                    if isinstance(payload, dict):
                        payload["guardrail_information"] = list(cards)
    except Exception as error:
        logging.getLogger(__name__).warning("pii_guardrail_information_failed: %s", type(error).__name__)


# LiteLLM 1.98.0 can publish an early-refusal or post-call-failure spend row
# before copying the guardrail's metadata. Responses SSE can publish its success
# row when the provider's terminal event arrives, before the restoration iterator
# closes. Reconcile ONLY those paths against their existing native spend row;
# no new spend entry or second callback is emitted.
_RECONCILE_SPEND_SQL = """
UPDATE "LiteLLM_SpendLogs" AS spend
SET metadata = jsonb_set(
    COALESCE(spend.metadata, '{}'::jsonb), '{guardrail_information}',
    (CASE WHEN jsonb_typeof(spend.metadata->'guardrail_information') = 'array'
          THEN spend.metadata->'guardrail_information' ELSE '[]'::jsonb END) ||
    COALESCE((
        SELECT jsonb_agg(incoming)
        FROM jsonb_array_elements($1::jsonb) AS incoming
        WHERE NOT EXISTS (
            SELECT 1
            FROM jsonb_array_elements(
                CASE WHEN jsonb_typeof(spend.metadata->'guardrail_information') = 'array'
                     THEN spend.metadata->'guardrail_information' ELSE '[]'::jsonb END
            ) AS present
            WHERE present->>'guardrail_name' = incoming->>'guardrail_name'
              AND present->>'guardrail_mode' = incoming->>'guardrail_mode'
        )
    ), '[]'::jsonb), true
)
WHERE spend.request_id IN ($2::text, $3::text)
  AND spend.metadata->>'litellm_call_id' = $3::text
"""


_RECONCILIATION_STATE = "_ru_guardrail_spend_reconciliation_state"


def _reconciliation_state(logging_obj):
    """Keep scheduling state on LiteLLM's per-request object, never in request JSON."""
    attributes = getattr(logging_obj, "__dict__", None)
    if not isinstance(attributes, dict):
        return None
    state = attributes.get(_RECONCILIATION_STATE)
    if not isinstance(state, dict):
        state = {"cards": {}, "synced": set(), "task": None}
        attributes[_RECONCILIATION_STATE] = state
    return state


def _schedule_spend_reconciliation(data, *, error=False):
    """Coalesce best-effort repairs of LiteLLM's existing asynchronous row."""
    logging_obj = data.get("litellm_logging_obj")
    call_id = getattr(logging_obj, "litellm_call_id", None)
    cards = _request_cards(data)
    if not isinstance(call_id, str) or not cards:
        return
    state = _reconciliation_state(logging_obj)
    if state is None:
        return
    for card in cards:
        name = card.get("guardrail_name")
        mode = card.get("guardrail_mode")
        if isinstance(name, str) and isinstance(mode, str):
            state["cards"][name, mode] = card
    if not set(state["cards"]).difference(state["synced"]):
        return

    from litellm.proxy import proxy_server
    from litellm.proxy.spend_tracking import spend_tracking_utils

    if getattr(proxy_server, "disable_spend_logs", False) is True:
        return
    settings = getattr(proxy_server, "general_settings", {})
    if error and isinstance(settings, dict) and settings.get("disable_error_logs") is True:
        return
    task = state.get("task")
    if task is not None and not task.done():
        return

    async def update_native_row():
        try:
            if proxy_server.prisma_client is None:
                return
            for attempt in range(40):
                current = list(state["cards"].items())
                safe_cards = spend_tracking_utils._sanitize_guardrail_information_for_spend_logs(
                    [card for _, card in current]
                )
                payload = json.dumps(safe_cards)
                details = getattr(logging_obj, "model_call_details", None)
                cached = details.get("standard_logging_object") if isinstance(details, dict) else None
                row_id = cached.get("id") if isinstance(cached, dict) else call_id
                if not isinstance(row_id, str):
                    row_id = call_id
                count = await proxy_server.prisma_client.db.execute_raw(
                    _RECONCILE_SPEND_SQL, payload, row_id, call_id,
                )
                if count:
                    state["synced"].update(identity for identity, _ in current)
                    if not set(state["cards"]).difference(state["synced"]):
                        return
                    # Another post-call guardrail finished during the SQL await.
                    # Merge its card into the same native row without a second task.
                    continue
                await asyncio.sleep(min(.1 + .1 * attempt, 1.))
            logging.getLogger(__name__).warning("pii_guardrail_spend_reconciliation_timed_out")
        except Exception as update_error:
            logging.getLogger(__name__).warning(
                "pii_guardrail_spend_reconciliation_failed: %s", type(update_error).__name__,
            )
        finally:
            state["task"] = None

    state["task"] = asyncio.create_task(update_native_row())


def reconcile_spend_information(data, *, error=False):
    """Never let logging or database diagnostics change the proxy response."""
    try:
        _schedule_spend_reconciliation(data, error=error)
    except Exception as schedule_error:
        logging.getLogger(__name__).warning(
            "pii_guardrail_spend_reconciliation_failed: %s", type(schedule_error).__name__,
        )
