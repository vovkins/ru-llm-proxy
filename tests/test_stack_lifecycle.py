"""Contract tests for the explicit Compose stack lifecycle."""

from __future__ import annotations

import importlib.util
import os
import stat
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MAKEFILE = (ROOT / "Makefile").read_text(encoding="utf-8")


def _load_setup_module():
    path = ROOT / "scripts" / "setup_codex_lb.py"
    spec = importlib.util.spec_from_file_location("setup_codex_lb", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_makefile_exposes_two_explicit_runtime_stacks():
    assert "STACK_LITELLM_PRESIDIO := litellm-presidio" in MAKEFILE
    assert "STACK_CODEX_LB := litellm-presidio-codex-lb" in MAKEFILE
    assert "setup: require-stack" in MAKEFILE
    assert "up: require-stack" in MAKEFILE
    assert "health: require-stack" in MAKEFILE


def test_makefile_exposes_docker_desktop_credential_helper_to_all_recipes():
    assert (
        "DOCKER_DESKTOP_BIN := /Applications/Docker.app/Contents/Resources/bin"
        in MAKEFILE
    )
    assert (
        "$(wildcard $(DOCKER_DESKTOP_BIN)/docker-credential-desktop)" in MAKEFILE
    )
    assert "export PATH := $(DOCKER_DESKTOP_BIN):$(PATH)" in MAKEFILE


def test_build_is_common_but_up_never_builds_implicitly():
    build_section = MAKEFILE.split("# === Build ===", 1)[1].split(
        "# === Up ===", 1
    )[0]
    up_section = MAKEFILE.split("# === Up ===", 1)[1].split(
        "# === Down ===", 1
    )[0]

    for service in (
        "litellm",
        "presidio-analyzer",
        "codex-lb",
        "guardrail-tests",
        "presidio-analyzer-tests",
    ):
        assert service in build_section
    assert "--no-build" in up_section
    assert " build" not in up_section
    assert 'scripts/stack_health.sh "$(STACK)" "$(ENV_FILE)"' in up_section
    assert '"$(STACK_START_TIMEOUT)"' in up_section
    assert "sleep 5" not in up_section


def test_restart_recreates_selected_stack_and_rereads_env():
    section = MAKEFILE.split("# === Restart", 1)[1].split(
        "# === Logs ===", 1
    )[0]

    assert "--force-recreate" in section
    assert "--no-build" in section
    assert '"$(STACK_START_TIMEOUT)"' in section
    assert "docker compose restart" not in section


def test_make_rejects_runtime_command_without_stack():
    result = subprocess.run(
        ["make", "up"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
        env={key: value for key, value in os.environ.items() if key != "STACK"},
    )

    assert result.returncode == 2
    assert "Укажите STACK=litellm-presidio" in result.stdout
    assert "docker compose" not in result.stdout


def test_setup_env_keeps_codex_lb_secrets_out_of_core_stack(tmp_path):
    env_file = tmp_path / ".env"
    example_file = tmp_path / ".env.example"
    example_file.write_text(
        "LITELLM_MASTER_KEY=sk-replace-with-generated-key\n"
        "LITELLM_SALT_KEY=replace-with-generated-salt\n"
        "POSTGRES_PASSWORD=***\n"
        "LITELLM_DB_URL=postgresql://litellm:litellm@db:5432/litellm\n"
        "UI_USERNAME=admin\n"
        "UI_PASSWORD=replace-with-generated-ui-password\n",
        encoding="utf-8",
    )

    subprocess.run(
        [
            "bash",
            str(ROOT / "scripts" / "setup_env.sh"),
            str(env_file),
            str(example_file),
            "litellm-presidio",
        ],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )

    contents = env_file.read_text(encoding="utf-8")
    assert "CODEX_LB_POSTGRES_PASSWORD=" not in contents
    assert "CODEX_LB_API_KEY=" not in contents
    assert stat.S_IMODE(env_file.stat().st_mode) == 0o600


def test_codex_lb_key_is_written_atomically_without_weakening_permissions(tmp_path):
    module = _load_setup_module()
    env_file = tmp_path / ".env"
    env_file.write_text("FIRST=value\nCODEX_LB_API_KEY=***\n", encoding="utf-8")
    env_file.chmod(0o644)

    module.update_env_value(env_file, "CODEX_LB_API_KEY", "sk-clb-secret")

    assert module.read_env_value(env_file, "CODEX_LB_API_KEY") == "sk-clb-secret"
    assert stat.S_IMODE(env_file.stat().st_mode) == 0o600
    assert not list(tmp_path.glob("..env.*"))


def test_codex_lb_service_key_selection_prefers_the_key_present_in_env():
    module = _load_setup_module()
    keys = [
        {"id": "old", "name": module.SERVICE_KEY_NAME, "keyPrefix": "sk-old"},
        {"id": "current", "name": module.SERVICE_KEY_NAME, "keyPrefix": "sk-live"},
    ]

    selected = module.choose_service_key(keys, "sk-live-full-secret")

    assert selected["id"] == "current"


class _FakeClient:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def request(self, path, **kwargs):
        self.calls.append((path, kwargs))
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response


def test_codex_lb_setup_uses_stock_admin_api_and_persists_new_key(tmp_path):
    module = _load_setup_module()
    env_file = tmp_path / ".env"
    env_file.write_text("CODEX_LB_API_KEY=***\n", encoding="utf-8")
    client = _FakeClient(
        [
            {"authenticated": True},
            [{"id": "account-1"}, {"id": "account-2"}],
            {"version": 7, "apiKeyAuthEnabled": False},
            {"version": 8, "apiKeyAuthEnabled": True},
            [],
            {"id": "key-1", "key": "sk-clb-generated"},
            {"data": [{"id": "gpt-5.6-luna"}]},
        ]
    )

    account_count = module.configure(client, env_file, "admin-password")

    assert account_count == 2
    assert module.read_env_value(env_file, "CODEX_LB_API_KEY") == (
        "sk-clb-generated"
    )
    assert client.calls[0] == (
        "/api/dashboard-auth/password/login",
        {"method": "POST", "payload": {"password": "admin-password"}},
    )
    assert (
        "/api/settings",
        {
            "method": "PUT",
            "payload": {"expectedVersion": 7, "apiKeyAuthEnabled": True},
        },
    ) in client.calls
    assert (
        "/api/api-keys/",
        {"method": "POST", "payload": {"name": module.SERVICE_KEY_NAME}},
    ) in client.calls
    assert client.calls[-1] == (
        "/v1/models",
        {"bearer": "sk-clb-generated"},
    )


def test_codex_lb_setup_reuses_matching_active_key(tmp_path):
    module = _load_setup_module()
    env_file = tmp_path / ".env"
    env_file.write_text("CODEX_LB_API_KEY=sk-live-secret\n", encoding="utf-8")
    client = _FakeClient(
        [
            {"authenticated": True},
            [{"id": "account-1"}],
            {"version": 3, "apiKeyAuthEnabled": True},
            [
                {
                    "id": "key-1",
                    "name": module.SERVICE_KEY_NAME,
                    "keyPrefix": "sk-live",
                    "isActive": True,
                }
            ],
            {"data": [{"id": "gpt-5.6-luna"}]},
        ]
    )

    module.configure(client, env_file, "admin-password")

    paths = [path for path, _ in client.calls]
    assert "/api/api-keys/key-1/regenerate" not in paths
    assert paths.count("/api/api-keys/") == 1
    assert module.read_env_value(env_file, "CODEX_LB_API_KEY") == (
        "sk-live-secret"
    )
