// Provider-specific presentation for LiteLLM 1.98.0's EvaluationCard.
// The installer supplies the chunk's React JSX runtime and GenericGuardrailResponse.
function ruGuardrailCard(entry, jsx, RawResponse) {
  const publicRules = new Set([
    "env_secret_assignment", "kubeconfig_payload", "nginx_config_payload",
    "service_manifest_payload", "log_or_stacktrace_payload", "configured_canary",
    "bearer_token", "jwt_token", "private_key_marker", "provider_key",
    "aml_cft_internal_controls", "sanctions_watchlist_matching",
    "transaction_monitoring_thresholds", "suspicious_activity_playbook",
    "compliance_bypass_procedure"
  ]);
  const publicActions = new Set([
    "clean", "skipped", "history_inherited", "no_mapping", "no_placeholders",
    "restored", "unsupported_response", "masked", "dictionary_substituted",
    "masked_and_dictionary_substituted", "pii_blocked",
    "regulated_topic_policy_blocked", "pre_egress_policy_blocked",
    "final_payload_leak_check_blocked", "preprocessing_timeout",
    "attachment_invalid", "attachment_too_large", "attachment_reference_unsupported",
    "attachment_format_unsupported", "attachment_dlp_unavailable",
    "env_mask_limit_exceeded", "env_mask_json_structure",
    "env_mask_assignment_structure", "env_mask_uncovered_value",
    "env_mask_alias_collision", "env_mask_mapping_required", "env_mask_requires_checks",
    "pii_history_unavailable", "pii_history_storage_unavailable",
    "pii_history_too_large", "pii_history_ambiguous", "pii_history_processing_failed",
    "fail_open", "fail_closed", "analyzer_overloaded", "analyzer_unavailable",
    "client_disconnected", "cancelled", "incomplete", "upstream_failure",
    "post_call_failure", "guardrail_error"
  ]);
  const entityTypes = new Set(("PHONE_NUMBER EMAIL_ADDRESS RU_INN RU_KPP RU_OGRN RU_OGRNIP RU_BIK " +
    "RU_SETTLEMENT_ACCOUNT RU_CORRESPONDENT_ACCOUNT RU_SNILS RU_PASSPORT CREDIT_CARD RU_ADDRESS " +
    "INTERNAL_IP INTERNAL_DOMAIN HOSTNAME DB_URL JWT BEARER_TOKEN PRIVATE_KEY BASE64_DATA " +
    "API_KEY SECRET_KEY AUTH_TOKEN LOGIN PASSWORD PERSON LOCATION ORGANIZATION CONTRACT_NUMBER PII").split(" "));
  const safeCounts = value => Object.fromEntries(Object.entries(value || {})
    .filter(([key, n]) => publicRules.has(key) && Number.isSafeInteger(n) && n > 0));
  const report = entry.ru_guardrail_report;
  const legacy = !report || report.version !== 1;
  const masked = Object.keys(entry.masked_entity_count || {}).length > 0;
  const counts = masked ? entry.masked_entity_count : report?.detected_entity_count || {};
  const rows = Object.entries(counts).filter(([type, n]) =>
    entityTypes.has(type) && Number.isSafeInteger(n) && n > 0);
  const caption = rows.length
    ? (masked ? "Замаскировано" : entry.guardrail_status === "guardrail_intervened"
      ? "Обнаружено до блокировки" : "Обнаружено до сбоя")
    : legacy ? "Нет данных в этой записи"
    : report.analysis === "complete" ? "Сущности не обнаружены"
    : report.analysis === "partial" ? "Анализ не завершён"
    : "Проверка сущностей не выполнялась";
  const summary = { action: publicActions.has(entry.guardrail_action)
    ? entry.guardrail_action : "Нет данных в этой записи" };
  if (legacy) {
    const categories = (entry.violation_categories || []).filter(key => publicRules.has(key));
    if (categories.length) summary.categories = categories;
  } else {
    const rules = (report.rules || []).filter(key => publicRules.has(key));
    if (rules.length) summary.rules = rules;
    const ruleCounts = safeCounts(report.rule_counts);
    if (Object.keys(ruleCounts).length) summary.rule_counts = ruleCounts;
    if (Number.isSafeInteger(report.dictionary_substitution_count)
        && report.dictionary_substitution_count > 0)
      summary.dictionary_substitution_count = report.dictionary_substitution_count;
    for (const key of ["error_code", "failure_operation", "error_type"])
      if (typeof report[key] === "string" && /^[A-Za-z][A-Za-z0-9_]{0,79}$/.test(report[key]))
        summary[key] = report[key];
  }
  return jsx.jsxs("div", {children: [
    jsx.jsx("h5", {className: "text-sm font-medium text-gray-700", children: "Masked Entities"}),
    jsx.jsx("p", {className: "text-xs text-gray-500", children: caption}),
    ...rows.map(([type, count]) => jsx.jsx("span", {
      className: "mr-2 px-2 py-1 bg-blue-50 text-blue-700 text-xs",
      children: `${type}: ${count}`
    }, type)),
    jsx.jsx(RawResponse, {response: summary})
  ]});
}
