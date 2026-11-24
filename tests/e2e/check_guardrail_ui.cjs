// Run against the disposable PostgreSQL/mock-provider stack from test_guardrail_ui.sh.
const assert = require("node:assert/strict");
const { setTimeout: delay } = require("node:timers/promises");
const { chromium } = require("playwright");

async function check(base) {
  const headers = {Authorization: "Bearer sk-test-master", "Content-Type": "application/json"};
  const text = "UI_FIXTURE_PERSON_VALUE UI_FIXTURE_PASSWORD_VALUE UI_FIXTURE_ORGANIZATION_VALUE";
  const cases = [
    ["test-mask", text, 200, "masked", "Замаскировано"],
    ["test-block", text, 422, "pii_blocked", "Обнаружено до блокировки"],
    ["test-block", "TOKEN=ui_private_value\nTraceback (most recent call last):", 422,
      "pre_egress_policy_blocked", "Проверка сущностей не выполнялась"],
    ["test-mask", "hello", 200, "clean", "Сущности не обнаружены"],
  ];
  for (const [name, content, status] of cases) {
    const response = await fetch(`${base}/v1/chat/completions`, {
      method: "POST", headers, body: JSON.stringify({
        model: "mock-chat", guardrails: [name], messages: [{role: "user", content}],
      }),
    });
    assert.equal(response.status, status);
    await response.text();
  }
  const date = offset => new Date(Date.now() + offset * 86400000).toISOString().slice(0, 19).replace("T", " ");
  const query = new URLSearchParams({start_date: date(-1), end_date: date(1)});
  const found = new Map();
  const deadline = Date.now() + 60000;
  // Proxy spending events are flushed asynchronously; use the actual Logs API.
  do {
    const res = await fetch(`${base}/spend/logs/ui?${query}`, {headers});
    if (!res.ok) throw new Error(`Logs API returned ${res.status}: ${await res.text()}`);
    for (const row of (await res.json()).data) {
      const cards = row.metadata.guardrail_information || [];
      const pre = cards.filter(e => e.guardrail_mode === "pre_call");
      assert.equal(pre.length, 1);
      const [entry] = pre;
      assert.equal(entry.guardrail_response, "REDACTED_BY_LITELM");
      assert.doesNotMatch(JSON.stringify(cards), /UI_FIXTURE_|ui_private_value/);
      found.set(entry.guardrail_action, row.request_id);
    }
    if (cases.every(([, , , action]) => found.has(action))) break;
    await delay(500);
  } while (Date.now() < deadline);

  const browser = await chromium.launch(process.env.CHROME_CHANNEL ? {channel: process.env.CHROME_CHANNEL} : {});
  try {
    const page = await browser.newPage();
    await page.goto(`${base}/ui/logs/`);
    await page.getByPlaceholder("Enter your username").fill("smoke");
    await page.getByPlaceholder("Enter your password").fill("smoke-test-only");
    await page.getByRole("button", {name: "Login", exact: true}).click();
    await page.waitForURL("**/ui/logs/**");
    for (const [name, , , action, caption] of cases) {
      assert.ok(found.has(action), `${action} absent from spend logs`);
      await page.goto(`${base}/ui/logs/?log_id=${encodeURIComponent(found.get(action))}`);
      const evaluations = page.getByText("Evaluation Details", {exact: true}).locator("..");
      await evaluations.getByText(name, {exact: true}).click();
      await evaluations.getByText("Masked Entities", {exact: true}).waitFor();
      await evaluations.getByText(caption, {exact: true}).waitFor();
      for (const type of ["PERSON", "PASSWORD", "ORGANIZATION"]) {
        assert.equal(await evaluations.getByText(`${type}: 1`, {exact: true}).count(),
          Number(action === "masked" || action === "pii_blocked"));
      }
      await evaluations.getByText("Raw Guardrail Response", {exact: true}).click();
      const raw = JSON.parse(await evaluations.locator("pre").innerText());
      assert.equal(raw.action, action);
      if (action === "pre_egress_policy_blocked") {
        assert.deepEqual(raw.rules, ["env_secret_assignment", "log_or_stacktrace_payload"]);
        assert.deepEqual(raw.rule_counts, {env_secret_assignment: 1, log_or_stacktrace_payload: 1});
      }
      console.log(`PASS Logs: ${action}`);
    }
    await page.goto(`${base}/ui/guardrails-monitor/`);
    for (const name of ["test-block", "test-mask", "test-post"]) {
      await page.getByRole("row").filter({hasText: name}).first().waitFor();
    }
    console.log("PASS Guardrails Monitor");
  } finally {
    await browser.close();
  }
}

check(process.argv[2]).catch(error => { console.error(error); process.exitCode = 1; });
