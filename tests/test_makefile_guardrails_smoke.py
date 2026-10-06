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
            r"^guardrails-smoke:[^\n]*\n(?P<body>(?:\t.*\n)+)",
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
        self.assertIn(". + {stream: true}", self.script)
        self.assertIn("Accept: text/event-stream", self.script)
        self.assertIn("--no-buffer", self.script)
        self.assertIn("CURL_EXIT=$curl_exit", self.script)
        self.assertIn('if [ "${CURL_EXIT:-1}" -ne 0 ]; then', self.script)
        self.assertIn("if expect_http_success", self.script)
        self.assertNotIn("|| true", self.script)

    def test_script_can_explicitly_exercise_responses_api(self):
        self.assertIn('RESPONSES_MODEL="${RESPONSES_MODEL:-}"', self.script)
        self.assertIn('if [ -n "$RESPONSES_MODEL" ]; then', self.script)
        self.assertIn('"$BASE_URL/v1/responses"', self.script)
        self.assertIn("expect_responses_restoration", self.script)
        self.assertIn("<EMAIL_ADDRESS_[0-9]+>", self.script)

    def test_script_has_optional_codex_lb_protocol_coverage(self):
        self.assertIn(
            'PROTOCOL_SMOKE_ENABLED="${PROTOCOL_SMOKE_ENABLED:-false}"',
            self.script,
        )
        self.assertIn("expect_responses_stream_events", self.script)
        self.assertIn("expect_chat_tool_restoration", self.script)
        self.assertIn("expect_responses_tool_restoration", self.script)
        self.assertIn("previous_response_id", self.script)
        self.assertIn("prompt_cache_key", self.script)

    def test_script_checks_restoration_in_both_chat_modes(self):
        self.assertIn(
            'expect_chat_restoration "non-streaming guardrails request" non-stream',
            self.script,
        )
        self.assertIn(
            'expect_chat_restoration "streaming guardrails request" stream', self.script
        )
        self.assertIn(
            'delta.content // empty] | join("") | contains($marker)', self.script
        )
        self.assertIn('test("<EMAIL_ADDRESS_[0-9]+>") | not', self.script)
        self.assertIn(
            "$(PYTHON_LOCAL) tests/test_makefile_guardrails_smoke.py", self.makefile
        )

    def test_script_checks_native_stream_text_and_tool_arguments(self):
        self.assertIn("expect_responses_stream_restoration", self.script)
        self.assertIn("response.output_text.delta", self.script)
        self.assertIn("response.function_call_arguments.delta", self.script)
        self.assertIn("delta.tool_calls[]?", self.script)
        self.assertEqual(self.script.count("for mode in non-stream stream; do"), 2)
        self.assertIn("fromjson | .email == \\$marker", self.script)

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
        self.assertIn("^data:[[:space:]]*\\[DONE\\]", self.script)
        self.assertIn("^event:[[:space:]]*error", self.script)

    def test_script_does_not_print_proxy_token(self):
        self.assertIn("Authorization: Bearer $RU_LLM_PROXY_TOKEN", self.script)
        self.assertNotIn('echo "$RU_LLM_PROXY_TOKEN', self.script)
        self.assertNotIn("printf '%s' \"$RU_LLM_PROXY_TOKEN", self.script)

    def test_makefile_and_readme_describe_broader_static_target(self):
        self.assertIn(
            "make test-routing-diagnostics — static tests для routing-smoke и guardrails-smoke Makefile targets",
            self.makefile,
        )
        self.assertIn("🧪 Makefile diagnostics static tests", self.makefile)
        self.assertIn("make test-static", self.readme)
        self.assertIn("make guardrails-smoke", self.readme)

    def test_docs_capture_local_smoke_and_update_checklist(self):
        self.assertIn("запущенного состава", self.readme)
        self.assertIn("принадлежащих проверке Redis-сопоставлений", self.monitoring)
        self.assertIn("docker compose exec -T redis", self.monitoring)
        self.assertIn("CURL_CONNECT_TIMEOUT", self.monitoring)
        checklist = self.monitoring.split(
            "Минимальный проверочный список обновления:", 1
        )[1]
        checklist = checklist.split("## Ссылки", 1)[0]
        self.assertIn("локальном окружении Docker Compose", checklist)
        self.assertIn("`make guardrails-smoke STACK=litellm-presidio`", checklist)
        self.assertLess(
            checklist.index("`make guardrails-list STACK=litellm-presidio`"),
            checklist.index("`make guardrails-smoke STACK=litellm-presidio`"),
        )
        self.assertLess(
            checklist.index("`make guardrails-smoke STACK=litellm-presidio`"),
            checklist.index("`make routing-smoke STACK=litellm-presidio`"),
        )


if __name__ == "__main__":
    unittest.main()
