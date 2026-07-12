"""Static checks for the ZCode client integration guide."""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def read_repo_file(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def test_zcode_client_doc_is_linked_from_top_level_docs():
    readme = read_repo_file("README.md")
    docs_index = read_repo_file("docs/README.md")
    examples = read_repo_file("docs/examples.md")

    assert "docs/clients/zcode.md" in readme
    assert "clients/zcode.md" in docs_index
    assert "clients/zcode.md" in examples
    assert "ZCode" in readme
    assert "ZCode" in examples


def test_zcode_doc_documents_api_key_mode_contract():
    doc = read_repo_file("docs/clients/zcode.md")

    for required in (
        "ZCode",
        "Use API Key",
        "OpenAI Base URL",
        "http://localhost:4000/v1",
        "https://<proxy-host>/v1",
        "RU_LLM_PROXY_TOKEN",
        "Пользовательский ключ LiteLLM",
        "glm-5.2",
        "glm-5.1",
        "CHAT_MODEL=glm-5.2 make client-auth-smoke",
        "CHAT_MODEL=glm-5.2 make guardrails-smoke",
    ):
        assert required in doc


def test_zcode_doc_separates_client_and_upstream_credentials():
    doc = read_repo_file("docs/clients/zcode.md")

    assert "ZAI_API_KEY" in doc
    assert "Не кладите `ZAI_API_KEY` или `LITELLM_MASTER_KEY` в настройки ZCode" in doc
    assert "ZCode получает только токен прокси" in doc
    assert "ZCode вызывает маршрут прокси `/v1`" in doc

    forbidden_client_credentials = (
        "API Key: $ZAI_API_KEY",
        "API Key: ZAI_API_KEY",
        "API Key: $LITELLM_MASTER_KEY",
        "API Key: LITELLM_MASTER_KEY",
        "Authorization: Bearer $ZAI_API_KEY",
        "Authorization: Bearer $LITELLM_MASTER_KEY",
        "RU_LLM_PROXY_TOKEN=$LITELLM_MASTER_KEY",
    )

    for pattern in forbidden_client_credentials:
        assert pattern not in doc


def test_zcode_doc_documents_account_login_boundary_and_glm_52_support():
    doc = read_repo_file("docs/clients/zcode.md")

    assert "Continue with Z.ai" in doc
    assert "не входит" in doc.lower()
    assert "glm-5.2" in doc
    assert "ZAI_API_KEY_2" in doc
    assert "litellm-config.yaml" in doc
