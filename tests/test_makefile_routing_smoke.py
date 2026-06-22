"""Static checks for the Makefile routing smoke target."""

from pathlib import Path
import re
import unittest


class RoutingSmokeMakefileTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        makefile = Path("Makefile").read_text(encoding="utf-8")
        match = re.search(r"^routing-smoke:\n(?P<body>(?:\t.*\n)+)", makefile, re.MULTILINE)
        if match is None:
            raise AssertionError("routing-smoke target not found")
        cls.recipe = match.group("body")

    def test_completion_requests_capture_and_check_http_status(self):
        self.assertIn('-w "%{http_code}"', self.recipe)
        self.assertIn('case "$$status" in', self.recipe)
        self.assertIn("2??)", self.recipe)
        self.assertIn("returned HTTP $$status", self.recipe)

    def test_completion_requests_use_versioned_chat_endpoint(self):
        self.assertIn("http://localhost:4000/v1/chat/completions", self.recipe)
        self.assertNotIn("http://localhost:4000/chat/completions", self.recipe)

    def test_failure_output_does_not_print_proxy_token(self):
        self.assertIn("Authorization: Bearer $$token", self.recipe)
        self.assertNotIn("echo \"$$token", self.recipe)
        self.assertNotIn("printf '%s' \"$$token", self.recipe)


if __name__ == "__main__":
    unittest.main()
