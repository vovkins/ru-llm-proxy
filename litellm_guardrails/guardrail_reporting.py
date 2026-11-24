"""Per-execution decisions and their content-free LiteLLM spend-log projection."""
from contextvars import ContextVar
import logging
import re
import time
from typing import Literal, TypedDict


class Decision(TypedDict, total=False):
    policy_result: str
    status: str
    rules: list[str]
    rule_counts: dict[str, int]
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
                 entity_counts=None, error_code=None, failure_operation=None, error_type=None):
    if decision is None:
        decision = current.get()
    if decision is None:
        return
    if status is None:
        status = ("success" if policy_result in SUCCESS else
                  "guardrail_intervened" if policy_result in INTERVENTION else FAILED)
    decision.update(
        policy_result=policy_result, status=status,
        rules=sorted({r for r in rules or () if _label(r)}),
        rule_counts=_counts(rule_counts), entity_counts=_safe_entity_counts(entity_counts),
        error_code=_label(error_code), failure_operation=_label(failure_operation),
        error_type=_label(error_type, ERROR_TYPE),
    )


def stream_failure(decision, result, operation, error=None):
    """Cleanup and exception translation must preserve the first diagnosed failure."""
    if decision is not None and decision.get("status") != FAILED:
        set_decision(result, decision=decision, status=FAILED, failure_operation=operation,
                     error_type=type(error).__name__ if error is not None else None)


def write_information(guardrail, data, mode, decision: Decision, started_wall, started_perf):
    if not guardrail.guardrail_name:
        return
    try:
        result = decision.get("policy_result", "guardrail_error")
        details = {k: decision[k] for k in (
            "rules", "rule_counts", "error_code", "failure_operation", "error_type"
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
    except Exception as error:
        logging.getLogger(__name__).warning("pii_guardrail_information_failed: %s", type(error).__name__)
