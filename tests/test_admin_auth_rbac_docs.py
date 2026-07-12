"""Static checks for production admin authentication and RBAC guidance."""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_env_compose_setup_wire_disable_admin_ui():
    env_example = (ROOT / ".env.example").read_text()
    compose = (ROOT / "docker-compose.yml").read_text()
    setup_script = (ROOT / "scripts" / "setup_env.sh").read_text()

    assert "DISABLE_ADMIN_UI=False" in env_example
    assert "DISABLE_ADMIN_UI=${DISABLE_ADMIN_UI:-False}" in compose
    assert 'ensure_key_exists "DISABLE_ADMIN_UI" "False"' in setup_script


def test_admin_access_runbook_defines_credential_boundaries_and_roles():
    doc = (ROOT / "docs" / "admin-access.md").read_text()

    for required in (
        "Границы учётных данных",
        "Правила публикации в промышленной среде",
        "OSS-база и Enterprise RBAC",
        "Модель операторских ролей",
        "Ротация и экстренный отзыв",
        "Требования к аудиту администрирования",
        "Проверочный список для промышленной среды",
        "Пользовательский ключ LiteLLM",
        "Ключи внешних провайдеров",
        "LITELLM_MASTER_KEY",
        "UI_USERNAME",
        "UI_PASSWORD",
        "DISABLE_ADMIN_UI=True",
        "SSO/OIDC/SAML",
        "VPN",
        "список разрешённых IP",
        "mTLS",
        "zero-trust proxy",
        "экстренный доступ",
        "proxy_admin_viewer",
        "org_admin",
        "team_admin",
        "premium/enterprise",
        "LiteLLM audit logs",
    ):
        assert required in doc


def test_docs_link_to_admin_access_runbook():
    docs = {
        "README.md": (ROOT / "README.md").read_text(),
        "docs/architecture.md": (ROOT / "docs" / "architecture.md").read_text(),
        "docs/monitoring.md": (ROOT / "docs" / "monitoring.md").read_text(),
        "docs/compliance.md": (ROOT / "docs" / "compliance.md").read_text(),
        "docs/examples.md": (ROOT / "docs" / "examples.md").read_text(),
        "docs/clients/jwt.md": (ROOT / "docs" / "clients" / "jwt.md").read_text(),
    }

    for path, text in docs.items():
        assert "admin-access.md" in text, path


def test_user_facing_client_docs_do_not_use_master_key_as_client_credential():
    docs = {
        "README.md": (ROOT / "README.md").read_text(),
        "docs/examples.md": (ROOT / "docs" / "examples.md").read_text(),
        "docs/clients/codex.md": (
            ROOT / "docs" / "clients" / "codex.md"
        ).read_text(),
        "docs/clients/claude-code.md": (
            ROOT / "docs" / "clients" / "claude-code.md"
        ).read_text(),
        "docs/clients/zcode.md": (
            ROOT / "docs" / "clients" / "zcode.md"
        ).read_text(),
        "docs/clients/opencode.md": (
            ROOT / "docs" / "clients" / "opencode.md"
        ).read_text(),
        "docs/clients/kilo-code.md": (
            ROOT / "docs" / "clients" / "kilo-code.md"
        ).read_text(),
    }

    disallowed_patterns = (
        "Authorization: Bearer $LITELLM_MASTER_KEY",
        "Authorization: Bearer LITELLM_MASTER_KEY",
        'env_key = "LITELLM_MASTER_KEY"',
        '"apiKey": "{env:LITELLM_MASTER_KEY}"',
        "ANTHROPIC_AUTH_TOKEN=\"$LITELLM_MASTER_KEY\"",
        "RU_LLM_PROXY_TOKEN=$LITELLM_MASTER_KEY",
    )

    for path, text in docs.items():
        for pattern in disallowed_patterns:
            assert pattern not in text, path

    assert "Authorization: Bearer $RU_LLM_PROXY_TOKEN" in docs["README.md"]
    assert "Authorization: Bearer $RU_LLM_PROXY_TOKEN" in docs["docs/examples.md"]
    assert "Не кладите `OPENAI_API_KEY` или `LITELLM_MASTER_KEY`" in docs[
        "docs/clients/codex.md"
    ]
    assert "Не кладите `ANTHROPIC_API_KEY` или `LITELLM_MASTER_KEY`" in docs[
        "docs/clients/claude-code.md"
    ]
    assert "Не кладите `ZAI_API_KEY` или `LITELLM_MASTER_KEY`" in docs[
        "docs/clients/zcode.md"
    ]


def test_admin_action_audit_is_separate_from_guardrail_audit():
    admin_doc = (ROOT / "docs" / "admin-access.md").read_text()
    monitoring = (ROOT / "docs" / "monitoring.md").read_text()
    compliance = (ROOT / "docs" / "compliance.md").read_text()

    for text in (admin_doc, monitoring, compliance):
        assert "административн" in text.lower()
        assert "действ" in text.lower()
        assert "gateway_guardrail_audit" in text

    assert "Аудит клиентских запросов и аудит административных действий — разные вещи" in (
        admin_doc
    )
    assert "admin-operator-boundary" in compliance


def test_static_suite_runs_admin_auth_rbac_regression():
    makefile = (ROOT / "Makefile").read_text()

    assert "tests/test_admin_auth_rbac_docs.py" in makefile
