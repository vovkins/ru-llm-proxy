"""Static checks for the live guardrails smoke target."""

from pathlib import Path
import re
import unittest


class GuardrailsSmokeMakefileTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.makefile = Path("Makefile").read_text(encoding="utf-8")
        cls.readme = Path("README.md").read_text(encoding="utf-8")
        cls.monitoring = Path("docs/monitoring.md").read_text(encoding="utf-8")
        cls.architecture = Path("docs/architecture.md").read_text(encoding="utf-8")
        match = re.search(
            r"^guardrails-smoke:\n(?P<body>(?:\t.*\n)+)",
            cls.makefile,
            re.MULTILINE,
        )
        if match is None:
            raise AssertionError("guardrails-smoke target not found")
        cls.recipe = match.group("body")
        cls.script_path = Path("tests/e2e/test_guardrails_smoke.sh")
        cls.script = cls.script_path.read_text(encoding="utf-8")

    def test_target_delegates_to_guardrails_smoke_script(self):
        self.assertIn("bash tests/e2e/test_guardrails_smoke.sh", self.recipe)

    def test_script_exercises_streaming_chat_completions(self):
        self.assertIn('BASE_URL="${LITELLM_URL:-http://localhost:4000}"', self.script)
        self.assertIn('"$BASE_URL/v1/chat/completions"', self.script)
        self.assertIn('"stream":true', self.script.replace(" ", ""))
        self.assertIn("Accept: text/event-stream", self.script)
        self.assertIn("--no-buffer", self.script)
        self.assertIn("CURL_EXIT=$curl_exit", self.script)
        self.assertIn('if [ "${CURL_EXIT:-1}" -ne 0 ]; then', self.script)
        self.assertIn("if expect_http_success", self.script)
        self.assertNotIn("|| true", self.script)

    def test_script_uses_bounded_curl_timeouts(self):
        self.assertIn(
            'CURL_CONNECT_TIMEOUT="${CURL_CONNECT_TIMEOUT:-10}"',
            self.script,
        )
        self.assertIn('CURL_MAX_TIME="${CURL_MAX_TIME:-180}"', self.script)
        self.assertIn('--connect-timeout "$CURL_CONNECT_TIMEOUT"', self.script)
        self.assertIn('--max-time "$CURL_MAX_TIME"', self.script)

    def test_script_checks_guardrail_header_and_redis_cleanup(self):
        self.assertIn("x-litellm-applied-guardrails", self.script)
        self.assertIn("ru-pii-mask-pre", self.script)
        self.assertIn("ru-pii-mask-post", self.script)
        self.assertIn("redis-cli --scan --pattern 'pii_mapping:*'", self.script)
        self.assertIn("docker compose exec -T redis", self.script)
        self.assertIn("comm -13", self.script)
        self.assertIn("SMOKE_PII_MARKER=", self.script)
        self.assertIn("mapping_value_contains", self.script)
        self.assertIn("smoke_leaked_mappings", self.script)
        self.assertIn("Redis has no smoke-owned leaked PII mappings", self.script)
        self.assertNotIn("Redis PII mapping set did not grow", self.script)
        self.assertNotIn('"$after_mappings" -le "$before_mappings"', self.script)

    def test_script_rejects_remote_urls_for_local_redis_check(self):
        self.assertIn("require_local_base_url", self.script)
        self.assertIn("docker compose Redis cleanup verification", self.script)
        self.assertIn("localhost", self.script)
        self.assertIn("127.0.0.1", self.script)
        self.assertIn("[::1]", self.script)

    def test_script_preflights_jq_dependency(self):
        self.assertIn("command -v jq", self.script)
        self.assertIn("jq is required", self.script)

    def test_script_requires_stream_completion_marker(self):
        self.assertIn('^data:[[:space:]]*\\[DONE\\]', self.script)
        self.assertIn('^event:[[:space:]]*error', self.script)

    def test_script_does_not_print_proxy_token(self):
        self.assertIn("Authorization: Bearer $RU_LLM_PROXY_TOKEN", self.script)
        self.assertNotIn("echo \"$RU_LLM_PROXY_TOKEN", self.script)
        self.assertNotIn("printf '%s' \"$RU_LLM_PROXY_TOKEN", self.script)

    def test_makefile_and_readme_describe_broader_static_target(self):
        self.assertIn(
            "make test-routing-diagnostics — static tests для routing-smoke и guardrails-smoke Makefile targets",
            self.makefile,
        )
        self.assertIn("🧪 Makefile diagnostics static tests", self.makefile)
        self.assertIn(
            "`make test-routing-diagnostics` | Static regression tests для `routing-smoke` и `guardrails-smoke` Makefile targets",
            self.readme,
        )

    def test_docs_capture_local_smoke_and_update_checklist(self):
        self.assertIn("локального docker-compose", self.readme)
        self.assertIn("CURL_CONNECT_TIMEOUT", self.readme)
        self.assertIn("CURL_MAX_TIME", self.readme)
        self.assertIn("smoke-owned", self.architecture)
        self.assertIn("docker compose exec -T redis", self.monitoring)
        self.assertIn("CURL_CONNECT_TIMEOUT", self.monitoring)
        checklist = self.monitoring.split("Минимальный update checklist:", 1)[1]
        checklist = checklist.split("## References", 1)[0]
        self.assertIn("локальном docker-compose окружении", checklist)
        self.assertIn("`make guardrails-smoke`", checklist)
        self.assertLess(
            checklist.index("`make guardrails-list`"),
            checklist.index("`make guardrails-smoke`"),
        )
        self.assertLess(
            checklist.index("`make guardrails-smoke`"),
            checklist.index("`make routing-smoke`"),
        )


if __name__ == "__main__":
    unittest.main()
