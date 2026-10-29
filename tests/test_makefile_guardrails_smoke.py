"""Static checks for the live guardrails smoke target."""

from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
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


class GuardrailsSmokeProtocolTest(unittest.TestCase):
    """Execute the actual shell scenario; neither Docker nor an LLM is used."""

    @classmethod
    def setUpClass(cls):
        cls.root = Path(__file__).resolve().parents[1]
        for name in ("curl", "jq", "bash"):
            if shutil.which(name) is None:
                raise AssertionError(f"{name} is required for smoke scenario regressions")

    def run_smoke(self, profile="native", fault=None):
        marker = "protocol-smoke@example.test"
        requests = []
        with tempfile.TemporaryDirectory(prefix="guardrails-protocol-") as directory:
            directory = Path(directory)
            leaked = directory / "leaked"
            docker_calls = directory / "docker-calls"
            docker = directory / "docker"
            docker.write_text(
                f"#!{sys.executable}\n"
                "import os, pathlib, sys\n"
                "args = sys.argv[1:]\n"
                "with open(os.environ['MOCK_DOCKER_CALLS'], 'a') as log: log.write(repr(args) + '\\n')\n"
                "scan = ['compose', 'exec', '-T', 'redis', 'redis-cli', '--scan', '--pattern', 'pii_mapping:*']\n"
                "get = ['compose', 'exec', '-T', 'redis', 'redis-cli', 'GET', 'pii_mapping:owned-test']\n"
                "if args == scan:\n"
                "    if pathlib.Path(os.environ['MOCK_LEAK_FLAG']).exists(): print('pii_mapping:owned-test')\n"
                "elif args == get:\n"
                "    if os.environ['MOCK_FAULT'] == 'redis_read_failure': raise SystemExit(9)\n"
                "    print(os.environ['SMOKE_PII_MARKER'])\n"
                "else: raise SystemExit('Unexpected Docker call: ' + repr(args))\n",
                encoding="utf-8",
            )
            docker.chmod(0o700)

            class Handler(BaseHTTPRequestHandler):
                def log_message(self, *args):
                    pass

                def reply(self, body, status=200, stream=False):
                    data = (body.encode() if isinstance(body, str) else
                            json.dumps(body, separators=(",", ":")).encode())
                    self.send_response(status)
                    self.send_header("Content-Type", "text/event-stream" if stream else "application/json")
                    self.send_header("Content-Length", str(len(data)))
                    self.send_header("x-litellm-applied-guardrails", "ru-pii-mask-pre,ru-pii-mask-post")
                    self.end_headers()
                    self.wfile.write(data)

                def do_GET(self):
                    if self.path == "/health/liveliness":
                        self.reply({"status": "ok"})
                        return
                    if self.path != "/model/info":
                        self.reply({}, 404)
                        return
                    if fault == "model_info_denied":
                        self.reply({"error": "not authorized"}, 403)
                        return
                    if fault == "model_info_invalid_json":
                        self.reply("not-json")
                        return
                    params = {"model": "openai/private-model",
                              "use_chat_completions_api": profile != "native",
                              "api_key": "DO_NOT_PRINT_ROUTE_CREDENTIAL"}
                    if profile == "bridge_prefix":
                        params.update(model="openai/chat_completions/private-model",
                                      use_chat_completions_api=False)
                    if profile == "bridge_custom_provider":
                        params.update(model="private-model", custom_llm_provider="openai")
                    rows = [{"model_name": "private-test-alias", "litellm_params": params} for _ in range(2)]
                    if fault == "missing_model":
                        rows = []
                    elif fault == "mixed_routes":
                        rows[1] = {"model_name": "private-test-alias", "litellm_params": {
                            "model": "openai/other-model", "use_chat_completions_api": profile == "native"}}
                    elif fault == "invalid_route":
                        params["use_chat_completions_api"] = "false"
                    elif fault == "null_native_flag":
                        params["use_chat_completions_api"] = None
                    elif fault == "absent_native_flag":
                        params.pop("use_chat_completions_api")
                    elif fault == "missing_route":
                        rows[0]["litellm_params"] = None
                    self.reply({"data": {} if fault == "invalid_model_list" else rows})

                def reject(self, kind, payload):
                    if fault == "transport_failure" and kind == "named":
                        self.close_connection = True
                        return
                    if fault in {"mapping_leak", "redis_read_failure"}:
                        leaked.touch()
                    param = "tool_choice" if kind == "named" else "previous_response_id"
                    code = "responses_chat_bridge_" + ("named_tool_unsupported" if kind == "named" else "history_unsupported")
                    status = 400
                    if kind == "history" and fault != "history_400":
                        code, status = "pii_history_unavailable", 409
                        error = {"code": "409", "message": "Sensitive history is unavailable.",
                                 "provider_specific_fields": {"error": {
                                     "code": code, "type": "pii_history_error"},
                                     "guardrail_name": "ru-pii-mask-pre"}}
                    else:
                        error = {"code": "400", "param": param,
                                 "message": "litellm.BadRequestError: " + code + ": Synthetic bridge limitation."}
                    if fault == "wrong_named_code" and kind == "named":
                        error["message"] = "Unrelated error mentioning " + code
                    if fault == "wrong_named_param" and kind == "named":
                        error["param"] = "model"
                    if fault == "wrong_named_status" and kind == "named":
                        status = 503
                    if fault == "structured_named_code" and kind == "named":
                        error["code"] = code
                    if fault == "wrong_history_code" and kind == "history":
                        error["provider_specific_fields"]["error"]["code"] = "different_history_error"
                    if fault == "error_leak":
                        error["debug"] = marker
                    if fault == "history_id_leak" and kind == "history":
                        error["debug"] = payload["previous_response_id"]
                    self.reply({"error": error}, status)

                def do_POST(self):
                    payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                    requests.append((self.path, payload))
                    if self.path == "/key/generate":
                        self.reply({"key": "sk-protocol-smoke-only"})
                        return
                    tool = bool(payload.get("tools"))
                    stream = payload.get("stream", False)
                    responses = self.path == "/v1/responses"
                    named_stream = responses and tool and stream and isinstance(payload.get("tool_choice"), dict)
                    history = responses and "previous_response_id" in payload
                    if named_stream and (profile != "native" or fault == "native_named_rejected"):
                        self.reject("named", payload)
                        return
                    if history and (profile != "native" or fault == "native_history_rejected"):
                        self.reject("history", payload)
                        return
                    continued = history or isinstance(payload.get("input"), list)
                    text = ("first" if fault == "wrong_context" else "second") + " " + marker if continued else marker
                    if fault == "missing_history_marker" and continued:
                        text = "second"
                    arguments = json.dumps({"email": "wrong@example.test" if fault == "wrong_arguments" and responses and stream else marker})
                    if responses:
                        output = ([{"type": "function_call", "name": "record_contact", "arguments": arguments}] if tool else
                                  [{"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": text}]}])
                        if fault == "empty_first_answer" and isinstance(payload.get("input"), str) and "Из двух" in payload["input"]:
                            output = []
                        response = {"id": "resp_smoke_test", "status": "completed", "output": output}
                        if tool and not stream:
                            response["metadata"] = {"private": "DO_NOT_PRINT_RESPONSE_CONTENT"}
                            if fault == "invalid_tool_response":
                                self.reply("not-json DO_NOT_PRINT_RESPONSE_CONTENT")
                                return
                            if fault == "missing_tool_call":
                                response["output"] = [{"type": "message", "role": "assistant", "content": [
                                    {"type": "output_text", "text": marker}]}]
                            elif fault == "invalid_tool_arguments":
                                output[0]["arguments"] = '{"email": "DO_NOT_PRINT_RESPONSE_CONTENT'
                            elif fault == "wrong_nonstream_arguments":
                                output[0]["arguments"] = json.dumps({"email": "wrong@example.test"})
                            elif fault == "tool_argument_placeholder":
                                output[0]["arguments"] = json.dumps({"email": "<EMAIL_ADDRESS_1>"})
                            elif fault == "other_response_placeholder":
                                output.append({"type": "message", "content": [
                                    {"type": "output_text", "text": "<EMAIL_ADDRESS_2>"}]})
                            elif fault == "incomplete_tool_response":
                                response.update(status="incomplete", incomplete_details={"reason": "max_output_tokens"})
                            elif fault == "missing_tool_email":
                                output[0]["arguments"] = json.dumps({"contact": marker})
                        if stream:
                            delta = {"type": "response.function_call_arguments.delta" if tool else "response.output_text.delta",
                                     "delta": arguments if tool else text}
                            done = "response.incomplete" if fault == "incomplete_stream" else "response.completed"
                            events = [delta, {"type": done, "response": response}]
                            self.reply("".join("data: " + json.dumps(e, separators=(",", ":")) + "\n\n" for e in events), stream=True)
                        else:
                            self.reply(response)
                    else:
                        message = ({"tool_calls": [{"index": 0, "function": {"arguments": arguments}}]} if tool else {"content": text})
                        if stream:
                            event = {"choices": [{"delta": message}]}
                            self.reply("data: " + json.dumps(event, separators=(",", ":")) + "\n\ndata: [DONE]\n\n", stream=True)
                        else:
                            self.reply({"choices": [{"message": message}]})

            server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                result = subprocess.run(
                    ["bash", "tests/e2e/test_guardrails_smoke.sh"], cwd=self.root,
                    env={"PATH": str(directory) + os.pathsep + os.environ["PATH"],
                         "HOME": str(directory), "CURL_HOME": str(directory),
                         "ENV_FILE": "/dev/null", "LITELLM_MASTER_KEY": "sk-protocol-master-only",
                         "LITELLM_URL": f"http://127.0.0.1:{server.server_port}",
                         "CHAT_MODEL": "private-test-alias", "RESPONSES_MODEL": "private-test-alias",
                         "PROTOCOL_SMOKE_ENABLED": "true", "SMOKE_PII_MARKER": marker,
                         "CURL_MAX_TIME": "5", "NO_PROXY": "127.0.0.1",
                         "MOCK_LEAK_FLAG": str(leaked), "MOCK_DOCKER_CALLS": str(docker_calls),
                         "MOCK_FAULT": fault or ""}, text=True, capture_output=True, timeout=45,
                )
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)
            self.assertTrue(docker_calls.is_file(), "The real Docker command must be replaced")
            self.assertNotIn("DO_NOT_PRINT_ROUTE_CREDENTIAL", result.stdout + result.stderr)
            self.assertNotIn("sk-protocol-master-only", result.stdout + result.stderr)
            self.assertNotIn("sk-protocol-smoke-only", result.stdout + result.stderr)
            self.assertNotIn("DO_NOT_PRINT_RESPONSE_CONTENT", result.stdout + result.stderr)
            return result, requests

    def test_native_route_preserves_named_tools_and_previous_response_id(self):
        result, requests = self.run_smoke()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        responses = [p for path, p in requests if path == "/v1/responses"]
        self.assertEqual(len([p for p in responses if p.get("tools")]), 2)
        self.assertTrue(all(isinstance(p["tool_choice"], dict) for p in responses if p.get("tools")))
        self.assertEqual(len([p for p in responses if p.get("previous_response_id")]), 1)
        self.assertFalse(any(isinstance(p.get("input"), list) for p in responses))
        for payload in responses:
            if payload.get("tools"):
                self.assertEqual(payload["input"], "Call record_contact with this exact email: protocol-smoke@example.test")
                self.assertEqual(payload["max_output_tokens"], 512)

    def test_bridge_routes_check_rejections_and_supported_alternatives(self):
        for profile in ("bridge", "bridge_prefix", "bridge_custom_provider"):
            with self.subTest(profile=profile):
                result, requests = self.run_smoke(profile)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                responses = [p for path, p in requests if path == "/v1/responses"]
                required = [p for p in responses if p.get("tool_choice") == "required"]
                self.assertEqual(len(required), 1)
                self.assertTrue(required[0]["stream"])
                self.assertEqual(len(required[0]["tools"]), 1)
                history = next(p for p in responses if isinstance(p.get("input"), list))
                self.assertNotIn("previous_response_id", history)
                self.assertEqual([p["role"] for p in history["input"]], ["user", "assistant", "user"])
                self.assertIn("второй", history["input"][0]["content"])
                self.assertIn("protocol-smoke@example.test", history["input"][0]["content"])
                self.assertEqual(history["input"][1]["content"], "protocol-smoke@example.test")
                for payload in responses:
                    if payload.get("tools"):
                        self.assertIn("opaque test value", payload["input"])
                        self.assertIn("Do not validate the value", payload["input"])
                        self.assertTrue(payload["input"].endswith("protocol-smoke@example.test"))
                        self.assertEqual(payload["max_output_tokens"], 512)
                        self.assertTrue(payload["tools"][0]["strict"])
                nonstream = next(p for p in responses if p.get("tools") and not p.get("stream"))
                self.assertEqual(nonstream["tool_choice"], {"type": "function", "name": "record_contact"})
                self.assertEqual(nonstream["tools"][0]["parameters"], {
                    "type": "object", "properties": {"email": {"type": "string"}},
                    "required": ["email"], "additionalProperties": False,
                })
                chat_tools = [p for path, p in requests if path == "/v1/chat/completions" and p.get("tools")]
                self.assertTrue(all(p["messages"][0]["content"] ==
                                    "Call record_contact with this exact email: protocol-smoke@example.test"
                                    for p in chat_tools))

    def test_nonstream_tool_failures_are_distinct_and_never_accept_plain_text(self):
        faults = {
            "missing_tool_call": "did not return a function call",
            "invalid_tool_response": "returned invalid response JSON",
            "invalid_tool_arguments": "returned invalid tool argument JSON",
            "wrong_nonstream_arguments": "returned a different tool argument value",
            "tool_argument_placeholder": "left an email placeholder in the response",
            "other_response_placeholder": "left an email placeholder in the response",
            "incomplete_tool_response": "did not complete",
            "missing_tool_email": "returned a different tool argument value",
        }
        for profile in ("bridge", "native"):
            for fault, message in faults.items():
                with self.subTest(profile=profile, fault=fault):
                    result, requests = self.run_smoke(profile, fault)
                    self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
                    label = "Responses API tool request (non-stream) "
                    self.assertIn(label + message, result.stdout)
                    self.assertNotIn(label + "restored the tool argument", result.stdout)
                    tools = [p for path, p in requests if path == "/v1/responses" and p.get("tools")]
                    self.assertEqual(len(tools), 3 if profile == "bridge" else 2)
                    self.assertEqual(len([p for p in tools if not p.get("stream")]), 1,
                                     "A failed tool assertion must not trigger a retry")

    def test_bridge_accepts_only_the_documented_history_400_variant(self):
        result, _ = self.run_smoke("bridge", "history_400")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("responses_chat_bridge_history_unsupported", result.stdout)

    def test_native_route_accepts_absent_or_null_optional_flag(self):
        for fault in ("null_native_flag", "absent_native_flag"):
            with self.subTest(fault=fault):
                result, _ = self.run_smoke("native", fault)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_bridge_does_not_fabricate_an_empty_assistant_turn(self):
        result, requests = self.run_smoke("bridge", "empty_first_answer")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        history = next(p for path, p in requests if path == "/v1/responses" and isinstance(p.get("input"), list))
        self.assertEqual([p["role"] for p in history["input"]], ["user", "user"])

    def test_bridge_accepts_a_structured_named_tool_error_code(self):
        result, _ = self.run_smoke("bridge", "structured_named_code")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_full_history_still_requires_sensitive_value_restoration(self):
        result, _ = self.run_smoke("bridge", "missing_history_marker")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("full-history continuation did not restore", result.stdout)

    def test_unknown_invalid_or_mixed_route_never_silently_downgrades(self):
        for fault in ("missing_model", "mixed_routes", "invalid_route", "missing_route",
                      "model_info_denied", "model_info_invalid_json", "invalid_model_list"):
            with self.subTest(fault=fault):
                result, requests = self.run_smoke("bridge", fault)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("Cannot determine a single Responses protocol route", result.stdout)
                self.assertFalse(any(p.get("tools") for _, p in requests))

    def test_unexpected_rejections_transport_errors_and_disclosures_fail(self):
        for fault in ("wrong_named_code", "wrong_named_param", "wrong_named_status", "wrong_history_code",
                      "transport_failure", "error_leak", "history_id_leak"):
            with self.subTest(fault=fault):
                result, _ = self.run_smoke("bridge", fault)
                self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_native_route_does_not_accept_bridge_rejections(self):
        for fault in ("native_named_rejected", "native_history_rejected"):
            with self.subTest(fault=fault):
                result, requests = self.run_smoke("native", fault)
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse(any(p.get("tool_choice") == "required" for _, p in requests))
                self.assertFalse(any(isinstance(p.get("input"), list) for _, p in requests))

    def test_missing_restoration_context_or_stream_completion_fails(self):
        for profile in ("bridge", "native"):
            for fault in ("wrong_arguments", "wrong_context", "incomplete_stream"):
                with self.subTest(profile=profile, fault=fault):
                    result, _ = self.run_smoke(profile, fault)
                    self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_mapping_leak_or_redis_read_failure_after_rejection_fails(self):
        for fault in ("mapping_leak", "redis_read_failure"):
            with self.subTest(fault=fault):
                result, _ = self.run_smoke("bridge", fault)
                self.assertNotEqual(result.returncode, 0)
                self.assertRegex(result.stdout, "left a smoke-owned Redis mapping|could not read Redis mapping")


if __name__ == "__main__":
    unittest.main()
