// Check legacy and current cards without relying on a running browser.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");

const source = fs.readFileSync(process.argv[2], "utf8");
const context = vm.createContext({});
vm.runInContext(source, context);
const jsx = {
  jsx: (type, properties) => ({type, ...properties}),
  jsxs: (type, properties) => ({type, ...properties}),
};
const RawResponse = () => {};
const unsafe = "ui_private_value";
for (const entry of [
  {
    guardrail_action: "dictionary_substituted",
    violation_categories: [unsafe, "env_secret_assignment"],
    masked_entity_count: {[unsafe]: 1, PERSON: 1},
  },
  {
    guardrail_action: "dictionary_substituted",
    ru_guardrail_report: {
      version: 1, analysis: "complete",
      rules: [unsafe, "env_secret_assignment"],
      rule_counts: {[unsafe]: 2, env_secret_assignment: 1},
      dictionary_substitution_count: 2,
    },
    violation_categories: [unsafe],
  },
]) {
  const card = context.ruGuardrailCard(entry, jsx, RawResponse);
  const rendered = JSON.stringify(card);
  assert.ok(!rendered.includes(unsafe), rendered);
  assert.ok(rendered.includes("env_secret_assignment"));
}
const builtInRules = context.ruGuardrailCard({
  guardrail_action: unsafe,
  ru_guardrail_report: {
    version: 1, analysis: "complete",
    rules: [unsafe, "private_key_marker", "provider_key"],
    rule_counts: {[unsafe]: 2, private_key_marker: 1, provider_key: 1},
  },
}, jsx, RawResponse);
const builtInRendered = JSON.stringify(builtInRules);
assert.ok(!builtInRendered.includes(unsafe), builtInRendered);
for (const rule of ["private_key_marker", "provider_key"])
  assert.ok(builtInRendered.includes(rule), builtInRendered);

const previouslyOmittedActions = [
  "preprocessing_timeout", "attachment_dlp_unavailable",
  "env_mask_limit_exceeded", "env_mask_json_structure",
  "env_mask_assignment_structure", "env_mask_uncovered_value",
  "env_mask_alias_collision", "env_mask_mapping_required", "env_mask_requires_checks",
  "pii_history_unavailable", "pii_history_storage_unavailable",
  "pii_history_too_large", "pii_history_ambiguous", "pii_history_processing_failed",
];
for (const action of previouslyOmittedActions) {
  const card = context.ruGuardrailCard({
    guardrail_action: action,
    ru_guardrail_report: {version: 1, analysis: "complete"},
  }, jsx, RawResponse);
  const rendered = JSON.stringify(card);
  assert.ok(rendered.includes(`"action":"${action}"`), rendered);
}
console.log("PASS legacy/current filtering and built-in card rendering");
