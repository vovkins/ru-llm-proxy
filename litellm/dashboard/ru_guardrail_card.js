// Provider-specific presentation for LiteLLM 1.98.0's EvaluationCard.
// The installer supplies the chunk's React JSX runtime and GenericGuardrailResponse.
function ruGuardrailCard(entry, jsx, RawResponse) {
  const report = entry.ru_guardrail_report;
  const legacy = !report || report.version !== 1;
  const masked = Object.keys(entry.masked_entity_count || {}).length > 0;
  const counts = masked ? entry.masked_entity_count : report?.detected_entity_count || {};
  const rows = Object.entries(counts).filter(([, n]) => Number.isSafeInteger(n) && n > 0);
  const caption = rows.length
    ? (masked ? "Замаскировано" : entry.guardrail_status === "guardrail_intervened"
      ? "Обнаружено до блокировки" : "Обнаружено до сбоя")
    : legacy ? "Нет данных в этой записи"
    : report.analysis === "complete" ? "Сущности не обнаружены"
    : report.analysis === "partial" ? "Анализ не завершён"
    : "Проверка сущностей не выполнялась";
  const summary = { action: entry.guardrail_action || "Нет данных в этой записи" };
  if (legacy) {
    if (entry.violation_categories?.length) summary.categories = entry.violation_categories;
  } else {
    for (const key of ["rules", "rule_counts", "error_code", "failure_operation", "error_type"]) {
      if (report[key]) summary[key] = report[key];
    }
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
