"""Static checks for the live guardrails smoke target."""

from pathlib import Path
import re
import unittest


class GuardrailsSmokeMakefileTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.makefile = Path("Makefile").read_text(encoding="utf-8")
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
        self.assertNotIn("|| true", self.script)

    def test_script_checks_guardrail_header_and_redis_cleanup(self):
        self.assertIn("x-litellm-applied-guardrails", self.script)
        self.assertIn("ru-pii-mask-pre", self.script)
        self.assertIn("ru-pii-mask-post", self.script)
        self.assertIn("redis-cli --scan --pattern 'pii_mapping:*'", self.script)
        self.assertIn("docker compose exec -T redis", self.script)
        self.assertIn("comm -13", self.script)
        self.assertIn("Redis PII mapping set did not grow", self.script)
        self.assertNotIn('"$after_mappings" -le "$before_mappings"', self.script)

    def test_script_requires_stream_completion_marker(self):
        self.assertIn('^data:[[:space:]]*\\[DONE\\]', self.script)
        self.assertIn('^event:[[:space:]]*error', self.script)

    def test_script_does_not_print_proxy_token(self):
        self.assertIn("Authorization: Bearer $RU_LLM_PROXY_TOKEN", self.script)
        self.assertNotIn("echo \"$RU_LLM_PROXY_TOKEN", self.script)
        self.assertNotIn("printf '%s' \"$RU_LLM_PROXY_TOKEN", self.script)


if __name__ == "__main__":
    unittest.main()
